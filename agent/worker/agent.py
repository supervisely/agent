# coding: utf-8

import re
import shutil
import time
import docker
import json
import threading
import subprocess
import os
import uuid
import warnings
from docker.models.containers import Container
from docker.models.images import ImageCollection
from docker.types import LogConfig
from concurrent.futures import ThreadPoolExecutor, wait
from typing import Dict, List
from pathlib import Path
from filelock import FileLock
from datetime import datetime
from docker.errors import DockerException

import supervisely_lib as sly

from worker.task_dockerized import TaskDockerized
from worker.agent_utils import TaskDirCleaner, AppDirCleaner, DockerImagesCleaner
from worker.task_update import check_and_pull_sly_net_if_needed

warnings.filterwarnings(action="ignore", category=UserWarning)

from worker import constants
from worker import agent_utils
from worker import docker_utils
from worker.task_sly import TaskSly
from worker.task_factory import create_task, is_task_type
from worker.logs_to_rpc import add_task_handler
from worker.agent_utils import LogQueue
from worker.system_info import (
    get_hw_info,
    get_self_docker_image_digest,
    get_gpu_info,
    get_container_info,
)
from worker.app_file_streamer import AppFileStreamer
from worker.telemetry_reporter import TelemetryReporter
from worker.task_dockerized import TaskDockerized

# pylint: disable=import-error, no-name-in-module
from supervisely_lib._utils import (
    _remove_sensitive_information,
)


class Agent:
    def __init__(self):
        self.logger = sly.get_task_logger("agent")
        sly.change_formatters_default_values(self.logger, "service_type", sly.ServiceType.AGENT)
        sly.change_formatters_default_values(self.logger, "event_type", sly.EventType.LOGJ)
        self.log_queue = LogQueue()
        add_task_handler(self.logger, self.log_queue)
        sly.add_default_logging_into_file(self.logger, constants.AGENT_LOG_DIR())

        self._stop_log_event = threading.Event()
        self.executor_log = ThreadPoolExecutor(max_workers=1)
        self.future_log = None

        self.logger.info("Agent comes back...")

        self.task_pool_lock = threading.Lock()
        self.task_pool: Dict[int, TaskSly] = {}  # task_id -> task_manager (process_id)

        self.thread_pool = ThreadPoolExecutor(max_workers=10)
        self.thread_list = []
        self.daemons_list = []

        self._remove_old_agent()
        self._validate_duplicated_agents()

        sly.fs.clean_dir(constants.AGENT_TMP_DIR())
        self._stop_missed_containers(constants.TASKS_DOCKER_LABEL())
        # for compatibility with old plugins
        self._stop_missed_containers(constants.TASKS_DOCKER_LABEL_LEGACY())

        self.docker_api = docker.from_env(
            version="auto", timeout=constants.DOCKER_API_CALL_TIMEOUT()
        )

        self.logger.info("Agent is ready to get tasks.")
        self.api = sly.AgentAPI(
            constants.TOKEN(),
            constants.SERVER_ADDRESS(),
            self.logger,
            constants.TIMEOUT_CONFIG_PATH(),
        )
        self.agent_connect_initially()
        self.logger.info("Agent connected to server.")

        self._history_file = None
        if constants.CROSS_AGENT_DATA_DIR() is not None:
            self._history_file = os.path.join(
                constants.CROSS_AGENT_DATA_DIR(),
                f"docker-images-history-{constants.TOKEN()[:8]}.json",
            )
            self._history_file_lock = FileLock(f"{self._history_file}.lock")

        cur_date = datetime.utcnow().strftime("%Y-%m-%dT%H:%M")

        with self._history_file_lock:
            images_stat = {}
            if sly.fs.file_exists(self._history_file):
                try:
                    with open(self._history_file, "r") as json_file:
                        images_stat = json.load(json_file)
                except json.JSONDecodeError:
                    self.logger.warning(
                        f"Corrupted JSON in {self._history_file}. Resetting images_stat."
                    )

            images_stat[self.agent_info["agent_image"]] = cur_date

            with open(self._history_file, "w") as json_file:
                json.dump(images_stat, json_file, indent=4)

    def _remove_old_agent(self):
        container_id = os.getenv("REMOVE_OLD_AGENT", None)
        dc = docker.from_env()
        if container_id is not None:
            try:
                old_agent: Container = dc.containers.get(container_id)
                old_agent.remove(force=True)
            except docker.errors.NotFound:
                pass

        self._update_net_client(dc)

        agent_same_token = []
        agent_name_start = constants.CONTAINER_NAME()

        agent_utils.check_and_remove_agent_with_old_name(dc)

        for cont in dc.containers.list(sparse=False, ignore_removed=True):
            if cont.name.startswith(agent_name_start):
                agent_same_token.append(cont)

        if len(agent_same_token) > 1:
            raise RuntimeError(
                "Several agents with the same token are running. Please, kill them or contact support."
            )

        if len(agent_same_token) == 1 and agent_same_token[0].name != agent_name_start:
            agent_same_token[0].rename(agent_name_start)

    def _update_net_client(self, dc: docker.DockerClient):
        need_update_env = constants.UPDATE_SLY_NET_AFTER_RESTART()
        if not need_update_env:
            return

        net_container_name = constants.NET_CLIENT_CONTAINER_NAME()
        sly_net_client_image_name = constants.NET_CLIENT_DOCKER_IMAGE()
        sly_net_container = None

        for container in dc.containers.list(sparse=False, ignore_removed=True):
            if container.name == net_container_name:
                sly_net_container: Container = container
                break

        if sly_net_container is None:
            self.logger.warn(
                "Something went wrong: can't find sly-net-client attached to this agent"
            )
            self.logger.warn(
                (
                    "Probably you should restart agent manually using instructions:"
                    "https://developer.supervisely.com/getting-started/connect-your-computer"
                )
            )
            return
        else:
            # pull if update too old agent
            if need_update_env is None:
                need_update_env = check_and_pull_sly_net_if_needed(
                    dc, sly_net_container, self.logger, sly_net_client_image_name
                )

        if need_update_env is False:
            return

        network = constants.NET_CLIENT_NETWORK()
        command = sly_net_container.attrs.get("Args")
        volumes = sly_net_container.attrs["HostConfig"]["Binds"]
        cap_add = sly_net_container.attrs["HostConfig"]["CapAdd"]
        privileged = sly_net_container.attrs["HostConfig"]["Privileged"]
        restart_policy = sly_net_container.attrs["HostConfig"]["RestartPolicy"]
        envs = sly_net_container.attrs["Config"]["Env"]

        log_config_dct = sly_net_container.attrs["HostConfig"]["LogConfig"]
        log_config = LogConfig(type=log_config_dct["Type"], config=log_config_dct["Config"])

        devices = []
        for dev in sly_net_container.attrs["HostConfig"]["Devices"]:
            host = dev.get("PathOnHost", None)
            cont = dev.get("PathInContainer", None)
            perm = dev.get("CgroupPermissions", "rwm")
            if host is not None and perm is not None:
                devices.append(f"{host}:{cont}:{perm}")

        # recreate network if necessary
        try:
            dc.networks.get(network)
        except:
            dc.networks.create(network)

        sly_net_container.remove(force=True)
        dc.containers.run(
            image=sly_net_client_image_name,
            name=net_container_name,
            command=command,
            network=network,
            cap_add=cap_add,
            volumes=volumes,
            privileged=privileged,
            restart_policy=restart_policy,
            environment=envs,
            log_config=log_config,
            devices=devices,
            detach=True,
        )

    def _validate_duplicated_agents(self):
        dc = docker.from_env()
        agent_same_token = []
        for cont in dc.containers.list(sparse=False, ignore_removed=True):
            if constants.CONTAINER_NAME() in cont.name:
                agent_same_token.append(cont)
        if len(agent_same_token) > 1:
            raise RuntimeError("Agent with the same token already exists.")

    def agent_connect_initially(self):
        try:
            hw_info = get_hw_info()
        except Exception:
            hw_info = {}
            self.logger.debug("Hardware information can not be obtained")

        docker_inspect_cmd = "curl -s --unix-socket /var/run/docker.sock http://localhost/containers/$(hostname)/json"
        docker_img_info = subprocess.Popen(
            [docker_inspect_cmd], shell=True, executable="/bin/bash", stdout=subprocess.PIPE
        ).communicate()[0]

        config = json.loads(docker_img_info).get("Config")
        if config is None:
            config = {}
            self.logger.warn("Docker container info unavailable, agent is running in debug VENV")

        gpu_info = get_gpu_info(self.logger)
        hw_info["gpuinfo"] = gpu_info

        self.agent_info = {
            "agent_id_from_storage_path": constants.AGENT_ID(),
            "hardware_info": hw_info,
            "agent_image": config.get(
                "Image", "docker.enterprise.supervise.ly/agent:6.999.0"
            ),  # for debug
            "agent_version": config.get("Labels", {}).get("VERSION", "agent:6.999.0"),  # for debug
            "agent_image_digest": get_self_docker_image_digest(),
            "server_address": constants.SERVER_ADDRESS(),
            "environ": {
                constants._SUPERVISELY_AGENT_FILES: constants.SUPERVISELY_AGENT_FILES(),
                constants._DOCKER_NET: constants.DOCKER_NET(),
            },
        }
        # @todo: add GPU device info
        resp = self.api.simple_request(
            "AgentConnected",
            sly.api_proto.ServerInfo,
            sly.api_proto.AgentInfo(info=json.dumps(self.agent_info)),
        )

    def send_connect_info(self):
        while True:
            time.sleep(2)
            self.api.simple_request("AgentPing", sly.api_proto.Empty, sly.api_proto.Empty())

    def get_new_task(self):
        for task in self.api.get_endless_stream(
            "GetNewTask",
            sly.api_proto.Task,
            sly.api_proto.Empty(),
            server_fail_limit=200,
            wait_server_sec=5,
        ):
            task_msg = json.loads(task.data)
            task_msg["agent_info"] = self.agent_info
            self.logger.info("GET_NEW_TASK", extra={"received_task_id": task_msg["task_id"]})
            to_log = _remove_sensitive_information(task_msg)
            self.logger.debug("FULL_TASK_MESSAGE", extra={"task_msg": to_log})
            self.start_task(task_msg)

    def get_stop_task(self):
        for task in self.api.get_endless_stream(
            "GetStopTask",
            sly.api_proto.Id,
            sly.api_proto.Empty(),
            server_fail_limit=200,
            wait_server_sec=5,
        ):
            stop_task_id = task.id
            self.logger.info("GET_STOP_TASK", extra={"task_id": stop_task_id})
            self.stop_task(stop_task_id)

    def stop_task(self, task_id):
        self.task_pool_lock.acquire()
        try:
            if task_id in self.task_pool:
                task = self.task_pool[task_id]
                task.join(timeout=20)
                task.terminate()
                if isinstance(task, TaskDockerized):
                    if task._container is not None:
                        try:
                            task._container.remove(force=True)
                        except docker.errors.NotFound:
                            pass
                        except:
                            self.logger.error("Unable to stop the container", exc_info=True)

                task_extra = {
                    "task_id": task_id,
                    "exit_status": task,
                    "exit_code": task.exitcode,
                }

                self.logger.info("REMOVE_TASK_TEMP_DATA IF NECESSARY", extra=task_extra)
                task.clean_task_dir()

                self.logger.info("TASK_STOPPED", extra=task_extra)
                del self.task_pool[task_id]

            else:
                self.logger.warning(
                    "Task could not be stopped. Not found", extra={"task_id": task_id}
                )

                dir_task = str(Path(constants.AGENT_APP_SESSIONS_DIR()) / str(task_id))
                if os.path.exists(dir_task):
                    cleaner = TaskDirCleaner(dir_task)
                    cleaner.allow_cleaning()
                    cleaner.clean()

                self.logger.info(
                    "TASK_MISSED",
                    extra={
                        "service_type": sly.ServiceType.TASK,
                        "event_type": sly.EventType.TASK_STOPPED,
                        "task_id": task_id,
                    },
                )

        finally:
            self.task_pool_lock.release()

    def start_task(self, task):
        self.task_pool_lock.acquire()
        try:
            if task["task_id"] in self.task_pool:
                # @TODO: remove - ?? only app will receive its messages (skip app button's messages)
                if task["task_type"] != "app":
                    self.logger.warning(
                        "TASK_ID_ALREADY_STARTED", extra={"task_id": task["task_id"]}
                    )
                else:
                    # request to application is duplicated to agent for debug purposes
                    pass
            else:
                task_id = task["task_id"]
                task["agent_version"] = self.agent_info["agent_version"]
                try:
                    # check existing upgrade task to avoid agent duplication
                    need_skip = False
                    if task["task_type"] == "update_agent":
                        for temp_id, temp_task in self.task_pool.items():
                            if is_task_type(temp_task, "update_agent"):
                                need_skip = True
                                break

                    if need_skip is False:
                        self.task_pool[task_id] = create_task(task, self.docker_api)
                        self.task_pool[task_id].start()
                    else:
                        self.logger.warning(
                            "Agent Update is running, current task is skipped due to duplication",
                            extra={"task_id": task_id},
                        )
                except Exception as e:
                    self.logger.critical(
                        "Unexpected exception in task start.",
                        exc_info=True,
                        extra={
                            "event_type": sly.EventType.TASK_CRASHED,
                            "exc_str": str(e),
                        },
                    )

        finally:
            self.task_pool_lock.release()

    def tasks_health_check(self):
        while True:
            time.sleep(3)
            self.task_pool_lock.acquire()
            try:
                all_tasks = list(self.task_pool.keys())
                for task_id in all_tasks:
                    val = self.task_pool[task_id]
                    if not val.is_alive():
                        self._forget_task(task_id)
            finally:
                self.task_pool_lock.release()

    # used only in healthcheck
    def _forget_task(self, task_id):
        task_extra = {
            "event_type": sly.EventType.TASK_REMOVED,
            "task_id": task_id,
            "exit_status": self.task_pool[task_id],
            "exit_code": self.task_pool[task_id].exitcode,
            "service_type": sly.ServiceType.TASK,
        }

        self.logger.info("REMOVE_TASK_TEMP_DATA IF NECESSARY", extra=task_extra)
        self.task_pool[task_id].clean_task_dir()

        del self.task_pool[task_id]
        self.logger.info("TASK_REMOVED", extra=task_extra)

    @staticmethod
    def _remove_containers(label_filter):
        dc = docker.from_env()
        stop_list = dc.containers.list(
            all=True, filters=label_filter, sparse=False, ignore_removed=True
        )
        for cont in stop_list:
            cont.remove(force=True)
        return stop_list

    def _stop_missed_containers(self, ecosystem_token):
        self.logger.info("Searching for missed containers...")
        label_filter = {"label": "ecosystem_token={}".format(ecosystem_token)}

        stopped_list = Agent._remove_containers(label_filter=label_filter)

        if len(stopped_list) == 0:
            self.logger.info("There are no missed containers.")

        for cont in stopped_list:
            self.logger.info("Container stopped", extra={"cont_id": cont.id, "labels": cont.labels})
            self.logger.info(
                "TASK_MISSED",
                extra={
                    "service_type": sly.ServiceType.TASK,
                    "event_type": sly.EventType.MISSED_TASK_FOUND,
                    "task_id": int(cont.labels["task_id"]),
                },
            )

    def submit_log(self):
        while True:
            log_lines = self.log_queue.get_log_batch_nowait()
            if len(log_lines) > 0:
                self.api.simple_request(
                    "Log", sly.api_proto.Empty, sly.api_proto.LogLines(data=log_lines)
                )
            else:
                if self._stop_log_event.isSet():
                    return True
                else:
                    time.sleep(1)

    def follow_daemon(self, process_cls, name, sleep_sec=5):
        proc = process_cls()
        self.daemons_list.append(proc)
        GPU_FREQ = 60
        last_gpu_message = 0
        try:
            proc.start()
            while True:
                if not proc.is_alive():
                    err_msg = "{}_CRASHED".format(name)
                    self.logger.error("Agent process is dead.", extra={"exc_str": err_msg})
                    time.sleep(1)  # an opportunity to send log
                    raise RuntimeError(err_msg)
                time.sleep(sleep_sec)
                last_gpu_message -= sleep_sec
                if last_gpu_message <= 0:
                    gpu_info = get_gpu_info(self.logger)
                    self.logger.debug(f"GPU state: {gpu_info}")
                    self.api.simple_request(
                        "UpdateTelemetry",
                        sly.api_proto.Empty,
                        sly.api_proto.AgentInfo(info=json.dumps({"gpu_info": gpu_info})),
                    )
                    last_gpu_message = GPU_FREQ

        except Exception as e:
            proc.terminate()
            proc.join(timeout=2)
            raise e

    def inf_loop(self):
        self.future_log = self.executor_log.submit(
            sly.function_wrapper_external_logger, self.submit_log, self.logger
        )
        self.thread_list.append(self.future_log)
        self.thread_list.append(
            self.thread_pool.submit(
                sly.function_wrapper_external_logger, self.tasks_health_check, self.logger
            )
        )
        self.thread_list.append(
            self.thread_pool.submit(
                sly.function_wrapper_external_logger, self.get_new_task, self.logger
            )
        )
        self.thread_list.append(
            self.thread_pool.submit(
                sly.function_wrapper_external_logger, self.get_stop_task, self.logger
            )
        )
        self.thread_list.append(
            self.thread_pool.submit(
                sly.function_wrapper_external_logger, self.send_connect_info, self.logger
            )
        )
        self.thread_list.append(
            self.thread_pool.submit(
                sly.function_wrapper_external_logger, self.task_clear_old_data, self.logger
            )
        )
        self.thread_list.append(
            self.thread_pool.submit(
                sly.function_wrapper_external_logger, self.task_stream_net_client_logs, self.logger
            )
        )
        self.thread_list.append(
            self.thread_pool.submit(
                sly.function_wrapper_external_logger, self.update_base_layers, self.logger
            )
        )
        self.thread_list.append(
            self.thread_pool.submit(
                sly.function_wrapper_external_logger, self.task_clear_tasks_dir, self.logger
            )
        )
        self.thread_list.append(
            self.thread_pool.submit(
                sly.function_wrapper_external_logger, self.task_clear_pip_cache, self.logger
            )
        )
        self.thread_list.append(
            self.thread_pool.submit(
                sly.function_wrapper_external_logger, self.task_clear_apps_data, self.logger
            )
        )
        # Removed due to bug
        # self.thread_list.append(
        #     self.thread_pool.submit(
        #         sly.function_wrapper_external_logger, self.monitor_stopped_tasks_containers, self.logger
        #     )
        # )

        if constants.DISABLE_TELEMETRY() is None:
            self.thread_list.append(
                self.thread_pool.submit(
                    sly.function_wrapper_external_logger,
                    self.follow_daemon,
                    self.logger,
                    TelemetryReporter,
                    "TELEMETRY_REPORTER",
                )
            )
        else:
            self.logger.warn("Telemetry is disabled in ENV")
        # self.thread_list.append(
        #     self.thread_pool.submit(sly.function_wrapper_external_logger, self.follow_daemon,  self.logger, AppFileStreamer, 'APP_FILE_STREAMER'))

    def wait_all(self):
        def terminate_all_deamons():
            for process in self.daemons_list:
                process.terminate()
                process.join(timeout=2)

        futures_statuses = wait(self.thread_list, return_when="FIRST_EXCEPTION")
        for future in self.thread_list:
            if future.done():
                try:
                    future.result()
                except Exception:
                    terminate_all_deamons()
                    break

        if not self.future_log.done():
            self.logger.info("WAIT_FOR_TASK_LOG: Agent is almost dead")
            time.sleep(
                1
            )  # Additional sleep to give other threads the final chance to get their logs in.
            self._stop_log_event.set()
            self.executor_log.shutdown(wait=True)

        if len(futures_statuses.not_done) != 0:
            raise RuntimeError("AGENT: EXCEPTION IN BASE FUTURE !!!")

    def task_clear_old_data(self):
        day = 60 * 60 * 24
        cleaner = AppDirCleaner(self.logger)
        image_cleaner = DockerImagesCleaner(self.docker_api, self.logger)

        while True:
            with self.task_pool_lock:
                all_tasks = set(self.task_pool.keys())

            try:
                cleaner.auto_clean(all_tasks)
            except Exception as e:
                self.logger.exception(e)
                # raise or not?
                # raise e
            image_cleaner.remove_idle_images()
            time.sleep(day)

    def task_stream_net_client_logs(self):
        net_container_name = constants.NET_CLIENT_CONTAINER_NAME()
        sly_net_container = None

        for container in self.docker_api.containers.list(sparse=False, ignore_removed=True):
            if container.name == net_container_name:
                sly_net_container: Container = container
                break

        if sly_net_container is None:
            return

        self.net_logger = sly.get_task_logger("net_client")
        sly.change_formatters_default_values(self.net_logger, "service_type", "NET_CLIENT")
        sly.change_formatters_default_values(self.net_logger, "event_type", sly.EventType.LOGJ)

        add_task_handler(self.net_logger, self.log_queue)
        sly.add_default_logging_into_file(self.net_logger, constants.AGENT_LOG_DIR())

        log_buffer = ""
        ansi_escape = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
        for chunk in sly_net_container.logs(stdout=True, stderr=True, follow=True, stream=True):
            decoded_chunk = chunk.decode("utf-8")
            log_buffer += decoded_chunk
            if log_buffer.endswith("\n"):
                log_buffer = ansi_escape.sub("", log_buffer)
                self.net_logger.info(log_buffer.strip())
                log_buffer = ""

    def update_base_layers(self):
        self.logger.info("Start background task: pulling base images")
        pulled = []
        for image in constants.BASE_IMAGES():
            try:
                if constants.SLY_APPS_DOCKER_REGISTRY() is not None and image.startswith(
                    "supervisely/"
                ):
                    self.logger.info(
                        "NON DEFAULT DOCKER REGISTRY: docker image {!r} is replaced with {!r}".format(
                            image,
                            f"{constants.SLY_APPS_DOCKER_REGISTRY()}/{image}",
                        )
                    )
                    image = f"{constants.SLY_APPS_DOCKER_REGISTRY()}/{image}"

                docker_utils.docker_pull_if_needed(
                    self.docker_api,
                    image,
                    policy=docker_utils.PullPolicy.ALWAYS,
                    logger=self.logger,
                    progress=False,
                )

                self.logger.info(f"Docker image '{image}' has been pulled successfully")
                pulled.append(image)
            except:
                self.logger.warn(f"Failed to pull docker image '{image}'", exc_info=True)
        self.logger.info(
            f"Background task finished: base images have been pulled. Images: [{', '.join([image for image in pulled])}]"
        )

    def task_clear_tasks_dir(self):
        tmp_dir = constants.AGENT_TASKS_DIR() + "_to_remove"
        if os.path.exists(tmp_dir):
            self.logger.info(
                "Start background task: Clearing Tasks data [_to_remove directory detected]"
            )
            try:
                shutil.rmtree(tmp_dir)
            except:
                self.logger.warn("Background task error: Failed to clear tasks data", exc_info=True)
            else:
                self.logger.info(
                    "Background task finished: Tasks data has been cleared successfully"
                )
        elif constants.SHOULD_CLEAN_TASKS_DATA():
            self.logger.info("Start background task: Clearing Tasks data")
            try:
                task_dir = constants.AGENT_TASKS_DIR()
                if os.path.exists(task_dir):
                    shutil.move(task_dir, tmp_dir)
                    os.makedirs(task_dir)
                    shutil.rmtree(tmp_dir)
            except:
                self.logger.warn("Background task error: Failed to clear tasks data", exc_info=True)
            else:
                self.logger.info(
                    "Background task finished: Tasks data has been cleared successfully"
                )

    def task_clear_pip_cache(self):
        tmp_dir = constants.APPS_PIP_CACHE_DIR() + "_to_remove"
        if os.path.exists(tmp_dir):
            self.logger.info(
                "Start background task: Clearing pip cache [_to_remove directory detected]"
            )
            try:
                shutil.rmtree(tmp_dir)
            except:
                self.logger.warn("Background task error: Failed to clear pip cache", exc_info=True)
            else:
                self.logger.info(
                    "Background task finished: Pip cache has been cleared successfully"
                )
        elif constants.SHOULD_CLEAN_PIP_CACHE():
            self.logger.info("Start background task: Clearing pip cache")
            try:
                if os.path.exists(constants.APPS_PIP_CACHE_DIR()):
                    shutil.move(constants.APPS_PIP_CACHE_DIR(), tmp_dir)
                    os.makedirs(constants.APPS_PIP_CACHE_DIR())
                    shutil.rmtree(tmp_dir)
            except:
                self.logger.warn("Background task error: Failed to clear pip cache", exc_info=True)
            else:
                self.logger.info(
                    "Background task finished: Pip cache has been cleared successfully"
                )

    def task_clear_apps_data(self):
        tmp_dir = constants.AGENT_APPS_CACHE_DIR() + "_to_remove"
        if os.path.exists(tmp_dir):
            self.logger.info(
                "Start background task: Clearing apps data [_to_remove directory detected]"
            )
            try:
                shutil.rmtree(tmp_dir)
            except:
                self.logger.warn("Background task error: Failed to clear apps data", exc_info=True)
            else:
                self.logger.info(
                    "Background task finished: Apps data has been cleared successfully"
                )
        elif constants.SHOULD_CLEAN_APPS_DATA():
            self.logger.info("Start background task: Clearing apps data")
            try:
                if os.path.exists(constants.AGENT_APPS_CACHE_DIR()):
                    shutil.move(constants.AGENT_APPS_CACHE_DIR(), tmp_dir)
                    os.makedirs(constants.AGENT_APPS_CACHE_DIR())
                    shutil.rmtree(tmp_dir)
            except:
                self.logger.warn("Background task error: Failed to clear apps data", exc_info=True)
            else:
                self.logger.info(
                    "Background task finished: Agent data has been cleared successfully"
                )
        tmp_dir = constants.SUPERVISELY_SYNCED_APP_DATA_CONTAINER() + "_to_remove"
        if os.path.exists(tmp_dir):
            self.logger.info(
                "Start background task: Clearing apps data [_to_remove directory detected]"
            )
            try:
                shutil.rmtree(tmp_dir)
            except:
                self.logger.warn("Background task error: Failed to clear apps data", exc_info=True)
            else:
                self.logger.info(
                    "Background task finished: Apps data has been cleared successfully"
                )
        elif constants.SHOULD_CLEAN_APPS_DATA():
            self.logger.info("Start background task: Clearing apps data")
            try:
                if os.path.exists(constants.SUPERVISELY_SYNCED_APP_DATA_CONTAINER()):
                    shutil.move(constants.SUPERVISELY_SYNCED_APP_DATA_CONTAINER(), tmp_dir)
                    os.makedirs(constants.SUPERVISELY_SYNCED_APP_DATA_CONTAINER())
                    shutil.rmtree(tmp_dir)
            except:
                self.logger.warn("Background task error: Failed to clear apps data", exc_info=True)
            else:
                self.logger.info(
                    "Background task finished: Agent data has been cleared successfully"
                )

    def monitor_stopped_tasks_containers(self):
        """
        This method is used to monitor stopped tasks containers.
        It will remove containers that are not needed anymore.
        """
        error_log_delay = 60 * 30
        last_error_log = time.monotonic() - error_log_delay
        self.logger.info("Start background task: Monitoring stopped tasks containers")
        while True:
            time.sleep(60*3)
            try:
                ecosystem_token = constants.TASKS_DOCKER_LABEL()
                label_filter = {"label": "ecosystem_token={}".format(ecosystem_token)}
                # legacy
                ecosystem_token = constants.TASKS_DOCKER_LABEL_LEGACY()
                legacy_label_filter = {"label": "ecosystem_token={}".format(ecosystem_token)}

                for label_filter in [label_filter, legacy_label_filter]:
                    dc = docker.from_env()
                    containers = dc.containers.list(
                        all=True, filters=label_filter, sparse=False, ignore_removed=True
                    )
                    for container in containers:
                        with self.task_pool_lock:
                            task_found = False
                            task_ids = list(self.task_pool.keys())
                            for task_id in task_ids:
                                task = self.task_pool[task_id]
                                if not isinstance(task, TaskDockerized):
                                    continue
                                if task._container is None:
                                    continue
                                if task._container.id != container.id:
                                    continue

                                task_found = True
                                break
                            
                            log_extra = {
                                "container_id": container.id,
                                "container_name": container.name,
                            }
                            if not task_found:
                                task_status = "not found"
                            else:
                                if self.task_pool[task_id].is_alive():
                                    task_status = "running"
                                else:
                                    task_status = "terminated"
                                
                                log_extra["task_id"] = task_id

                        try:
                            container.reload()
                        except docker.errors.NotFound:
                            container_status = "not found"
                        except docker.errors.APIError as e:
                            container_status = "error during container reload"
                            self.logger.debug("Error during container reload", extra=log_extra)
                            continue
                        else:
                            container_status = container.status

                        if container_status == "running" and task_status == "running":
                            continue

                        if container_status == "running":
                            self.logger.info(f"Task is {task_status}, but container is still running", extra=log_extra)
                            self.logger.info("Removing container", extra=log_extra)
                            try:
                                container.stop(timeout=30)
                            except docker.errors.NotFound:
                                self.logger.info("Container not found during removal", extra=log_extra)
                                continue
                            except DockerException:
                                pass
                            try:
                                container.remove(force=True)
                            except docker.errors.NotFound:
                                self.logger.info("Container not found during removal", extra=log_extra)
                            except DockerException as e:
                                self.logger.error("Failed to remove container", exc_info=True, extra=log_extra)
                            else:
                                self.logger.info("Container removed successfully", extra=log_extra)
                        elif task_status == "running":
                            self.logger.info(f"Task is {task_status}, but container is {container_status}", extra=log_extra)
                            self.logger.info("Stopping the task", extra={"task_id": task_id})
                            self.stop_task(task_id)
                            # TODO: handle other statuses like "paused", "restarting"
                            
            except Exception as e:
                if time.monotonic() - last_error_log > error_log_delay:
                    self.logger.error("Error during monitoring stopped tasks containers", exc_info=True)
                    last_error_log = time.monotonic()

