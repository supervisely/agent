# coding: utf-8

import copy
import json
import os
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from threading import Lock
from typing import Callable, List, Optional

import docker
from filelock import FileLock
import supervisely_lib as sly
from worker import constants
from worker.agent_utils import (
    TaskDirCleaner,
    filter_log_line,
    pip_req_satisfied_filter,
    post_get_request_filter,
    convert_millicores_to_cpu_quota,
)
from worker.task_sly import TaskSly
from docker.models.containers import Container

class TaskStep(Enum):
    NOTHING = 0
    DOWNLOAD = 1
    MAIN = 2
    UPLOAD = 3


@dataclass
class ErrorReport(object):
    title: Optional[str] = None
    description: Optional[str] = None

    def to_dict(self) -> dict:
        return {"title": self.title, "description": self.description}


# task with main work in separate container and with sequential steps
class TaskDockerized(TaskSly):
    step_name_mapping = {
        "DOWNLOAD": TaskStep.DOWNLOAD,
        "MAIN": TaskStep.MAIN,
        "UPLOAD": TaskStep.UPLOAD,
    }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.docker_runtime = "runc"  # or 'nvidia'
        self.entrypoint = "/workdir/src/main.py"
        self.action_map = {}

        self.completed_step = TaskStep.NOTHING
        self.task_dir_cleaner = TaskDirCleaner(self.dir_task)

        self._docker_api: docker.DockerClient = None  # must be set by someone

        self._container: Container = None
        self._container_lock = Lock()  # to drop container from different threads

        self.docker_image_name = None

        self.docker_pulled = False  # in task
        self._container_name = None
        self._task_reports: List[ErrorReport] = []
        self._log_filters = [pip_req_satisfied_filter]  # post_get_request_filter

        self._history_file = None
        if constants.CROSS_AGENT_DATA_DIR() is not None:
            self._history_file = os.path.join(
                constants.CROSS_AGENT_DATA_DIR(),
                f"docker-images-history-{constants.TOKEN()[:8]}.json",
            )
            self._history_file_lock = FileLock(f"{self._history_file}.lock")

    def init_docker_image(self):
        self.docker_image_name = self.info.get("docker_image", None)
        if self.docker_image_name is not None and ":" not in self.docker_image_name:
            self.docker_image_name += ":latest"

        self.update_image_history(self.docker_image_name)

    @property
    def docker_api(self):
        return self._docker_api

    @docker_api.setter
    def docker_api(self, val):
        self._docker_api = val

    def report_step_done(self, curr_step):
        if self.completed_step.value < curr_step.value:
            self.logger.info(
                "STEP_DONE",
                extra={
                    "step": curr_step.name,
                    "event_type": sly.EventType.STEP_COMPLETE,
                },
            )
            self.completed_step = curr_step

    def task_main_func(self):
        self.init_docker_image()
        self.task_dir_cleaner.forbid_dir_cleaning()

        last_step_str = self.info.get("last_complete_step")
        self.logger.info("LAST_COMPLETE_STEP", extra={"step": last_step_str})
        self.completed_step = self.step_name_mapping.get(last_step_str, TaskStep.NOTHING)

        for curr_step, curr_method in [
            (TaskStep.DOWNLOAD, self.download_step),
            (TaskStep.MAIN, self.main_step),
            (TaskStep.UPLOAD, self.upload_step),
        ]:
            if self.completed_step.value < curr_step.value:
                self.logger.info("BEFORE_STEP", extra={"step": curr_step.name})
                curr_method()

        self.task_dir_cleaner.allow_cleaning()

    # don't forget to report_step_done
    def download_step(self):
        raise NotImplementedError()

    # don't forget to report_step_done
    def upload_step(self):
        raise NotImplementedError()

    def before_main_step(self):
        raise NotImplementedError()

    def main_step_envs(self):
        envs = {}
        for env_key in (
            sly.api.SUPERVISELY_PUBLIC_API_RETRIES,
            sly.api.SUPERVISELY_PUBLIC_API_RETRY_SLEEP_SEC,
        ):
            env_val = os.getenv(env_key)
            if env_val is not None:
                envs[env_key] = env_val

        if constants.REQUESTS_CA_BUNDLE() is not None:
            envs[constants._REQUESTS_CA_BUNDLE] = constants.REQUESTS_CA_BUNDLE_CONTAINER()

        return envs

    def main_step(self):
        self.before_main_step()
        sly.fs.log_tree(self.dir_task, self.logger)
        self.spawn_container(add_envs=self.main_step_envs())
        self.process_logs()
        self.drop_container_and_check_status()
        self.report_step_done(TaskStep.MAIN)

    def run(self):
        try:
            super().run()
        finally:
            if self._stop_event.is_set():
                self.task_dir_cleaner.allow_cleaning()
            self._drop_container()  # if something occurred

    def clean_task_dir(self):
        self.task_dir_cleaner.clean()

    def _get_task_volumes(self):
        volumes = {
            self.dir_task_host: {"bind": "/sly_task_data", "mode": "rw"},
        }
        if constants.REQUESTS_CA_BUNDLE() is not None:
            volumes[constants.MOUNTED_HOST_REQUESTS_CA_BUNDLE()] = {
                "bind": constants.REQUESTS_CA_BUNDLE_DIR_CONTAINER(),
                "mode": "ro",
            }

        return volumes

    def get_spawn_entrypoint(self):
        return ["sh", "-c", "python -u {}".format(self.entrypoint)]

    def spawn_container(self, add_envs=None, add_labels=None, entrypoint_func=None):
        add_envs = sly.take_with_default(add_envs, {})
        add_labels = sly.take_with_default(add_labels, {})
        if entrypoint_func is None:
            entrypoint_func = self.get_spawn_entrypoint

        self._container_lock.acquire()
        volumes = self._get_task_volumes()
        volumes_dupl = copy.deepcopy(volumes)
        volumes_to_log = {}
        for host_path, binding in volumes_dupl.items():
            new_binding = copy.deepcopy(binding)
            if "bind" in new_binding:
                new_binding["bind"] = new_binding["bind"].replace(constants.TOKEN(), "***")
            volumes_to_log[host_path.replace(constants.TOKEN(), "***")] = new_binding

        self.logger.info("Docker container volumes", extra={"volumes": volumes_to_log})

        try:
            all_environments = {
                "LOG_LEVEL": "DEBUG",
                "LANG": "C.UTF-8",
                "PYTHONUNBUFFERED": "1",
                constants._HTTP_PROXY: constants.HTTP_PROXY(),
                constants._HTTPS_PROXY: constants.HTTPS_PROXY(),
                "HOST_TASK_DIR": self.dir_task_host,
                constants._NO_PROXY: constants.NO_PROXY(),
                constants._HTTP_PROXY.lower(): constants.HTTP_PROXY(),
                constants._HTTPS_PROXY.lower(): constants.HTTPS_PROXY(),
                constants._NO_PROXY.lower(): constants.NO_PROXY(),
                "PIP_ROOT_USER_ACTION": "ignore",
                **add_envs,
            }
            if constants.SSL_CERT_FILE() is not None:
                all_environments[constants._SSL_CERT_FILE] = constants.SSL_CERT_FILE()

            ipc_mode = ""
            if self.docker_runtime == "nvidia":
                ipc_mode = "host"
                self.logger.info(f"NVidia runtime, IPC mode is set to {ipc_mode}")

            self._container_name = "sly_task_{}_{}".format(
                self.info["task_id"], constants.TASKS_DOCKER_LABEL()
            )

            cpu_quota = self.info.get("limits", {}).get("cpu", None)
            if cpu_quota is None:
                cpu_quota = constants.CPU_LIMIT()
            if cpu_quota is not None:
                cpu_quota = convert_millicores_to_cpu_quota(cpu_quota)

            mem_limit = self.info.get("limits", {}).get("memory", None)
            if mem_limit is None:
                mem_limit = constants.MEM_LIMIT()

            self._container = self._docker_api.containers.run(
                self.docker_image_name,
                runtime=self.docker_runtime,
                entrypoint=entrypoint_func(),
                detach=True,
                name=self._container_name,
                remove=False,  # TODO: check autoremove
                volumes=volumes,
                environment=all_environments,
                labels={
                    "ecosystem": "supervisely",
                    "ecosystem_token": constants.TASKS_DOCKER_LABEL(),
                    "task_id": str(self.info["task_id"]),
                    **add_labels,
                },
                shm_size=constants.SHM_SIZE(),
                stdin_open=False,
                tty=False,
                cpu_quota=cpu_quota,
                mem_limit=mem_limit,
                memswap_limit=mem_limit,
                network=constants.DOCKER_NET(),
                ipc_mode=ipc_mode,
                security_opt=constants.SECURITY_OPT(),
            )
            self._container.reload()
            self.logger.debug(
                "After spawning. Container status: {}".format(str(self._container.status))
            )
            self.logger.info(
                "Docker container is spawned",
                extra={
                    "container_id": self._container.id,
                    "container_name": self._container.name,
                },
            )
        finally:
            self._container_lock.release()

    def _stop_wait_container(self):
        status = {}
        container = self._container  # don't lock, fail if someone will remove container
        if container is not None:
            container.stop(timeout=2)
            status = container.wait()
        return status

    def _drop_container(self):
        self._container_lock.acquire()
        try:
            if self._container is not None:
                self._container.remove(force=True)
                self._container = None
        finally:
            self._container_lock.release()

    def drop_container_and_check_status(self):
        status = self._stop_wait_container()
        if (len(status) > 0) and (status["StatusCode"] not in [0]):  # StatusCode must exist
            # if len(self._task_reports) > 0:
            raise RuntimeError(
                "Task container finished with non-zero status: {}".format(str(status))
            )
        self.logger.debug("Task container finished with status: {}".format(str(status)))
        self._drop_container()
        return status

    @classmethod
    def parse_log_line(cls, log_line):
        msg = ""
        lvl = "INFO"
        try:
            jlog = json.loads(log_line)
            msg = jlog["message"]
            del jlog["message"]
            lvl = jlog["level"].upper()
            del jlog["level"]
        except (KeyError, ValueError, TypeError):
            msg = str(log_line)
            jlog = {}

        if "event_type" not in jlog:
            jlog["event_type"] = sly.EventType.LOGJ

        return msg, jlog, lvl

    def call_event_function(self, jlog):
        et = jlog["event_type"]
        if et in self.action_map:
            return self.action_map[et](jlog)
        return {}

    def process_logs(self):
        logs_found = False
        for log_line in self._container.logs(stream=True):
            logs_found = True
            log_line = log_line.decode("utf-8")
            msg, res_log, lvl = self.parse_log_line(log_line)
            output = self.call_event_function(res_log)
            self._process_report(msg)

            lvl_description = sly.LOGGING_LEVELS.get(lvl, None)

            if lvl_description is not None:
                lvl_int = lvl_description.int
            else:
                lvl_int = sly.LOGGING_LEVELS["INFO"].int

            lvl_int = filter_log_line(msg, lvl_int, self._log_filters)

            # skip msg
            if lvl_int == -1:
                continue

            self.logger.log(lvl_int, msg, extra={**res_log, **output})

        if not logs_found:
            self.logger.warn("No logs obtained from container.")  # check if bug occurred

    def _process_report(self, log_msg: str):
        if log_msg is None:
            self.logger.warn("Received empty (none) message in process task report")
            return

        err_title, err_desc = None, None
        splits = log_msg.split(":")

        if splits[0].endswith("Error title"):
            err_title = splits[-1].strip()
        if splits[0].endswith("Error message"):
            err_desc = splits[-1].strip()

        if err_title is not None:
            self._task_reports.append(ErrorReport(title=err_title))
        if err_desc is not None:
            try:
                last_report = self._task_reports[-1]
                if last_report.description is None:
                    last_report.description = err_desc
                else:
                    self.logger.warn("Last DialogWindowError report was suspicious.")
                    self.logger.warn("Found message without title.")
            except IndexError:
                pass

    def update_image_history(self, image_name):
        if self._history_file is None:
            self.logger.debug(
                f"{constants._CROSS_AGENT_DATA_DIR} has not been set; the process of removing unused Docker will not be executed"
            )
            return

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

            images_stat[image_name] = cur_date

            with open(self._history_file, "w") as json_file:
                json.dump(images_stat, json_file, indent=4)
