# coding: utf-8
from __future__ import annotations

import os
import requests
import tarfile
import shutil
import json
import urllib.parse
import pkg_resources
import pathlib
import copy

from docker.errors import APIError, NotFound, DockerException
from slugify import slugify
from pathlib import Path
from packaging import version
from version_parser import Version
from enum import Enum
from typing import Optional

import supervisely_lib as sly
from .task_dockerized import TaskDockerized
from supervisely_lib.io.json import dump_json_file
from supervisely_lib.io.json import flatten_json, modify_keys
from supervisely_lib.api.api import SUPERVISELY_TASK_ID
from supervisely_lib.api.api import Api
from supervisely_lib.io.fs import (
    ensure_base_path,
    silent_remove,
    get_file_name,
    remove_dir,
    get_subdirs,
    file_exists,
    mkdir,
)
from supervisely_lib.io.exception_handlers import handle_exceptions

from worker import constants
from worker.agent_utils import (
    filter_log_line,
    pip_req_satisfied_filter,
    post_get_request_filter,
)

_ISOLATE = "isolate"
_LINUX_DEFAULT_PIP_CACHE_DIR = "/root/.cache/pip"
_APP_CONTAINER_DATA_DIR = "/sly-app-data"

_MOUNT_FOLDER_IN_CONTAINER = "/mount_folder"


class GPUFlag(Enum):
    preferred: str = "preferred"
    required: str = "required"
    skipped: str = "skipped"

    @classmethod
    def from_config(cls, config: dict) -> GPUFlag:
        gpu_flag = config.get("gpu", None)
        gpu = cls.from_str(gpu_flag)

        if gpu is GPUFlag.skipped:
            need_gpu = False
            if "needGPU" in config:
                need_gpu = config["needGPU"]
            if "need_gpu" in config:
                need_gpu = config["need_gpu"]
            if need_gpu is True:
                gpu = GPUFlag.required

        return gpu

    @classmethod
    def from_str(cls, config_val: Optional[str]) -> GPUFlag:
        if config_val is None or config_val.lower() == "no":
            return GPUFlag.skipped
        elif config_val.lower() == "preferred":
            return GPUFlag.preferred
        elif config_val.lower() == "required":
            return GPUFlag.required
        raise ValueError(f"Unknown gpu flag found in config: {config_val}")


class TaskApp(TaskDockerized):
    def __init__(self, *args, **kwargs):
        self.app_config = None
        self.dir_task_src = None
        self.dir_task_container = None
        self.dir_task_src_container = None
        self.dir_apps_cache_host = None
        self.dir_apps_cache_container = None
        self._exec_id = None
        self.app_info = None
        self._path_cache_host = None
        self._need_sync_pip_cache = False
        self._requirements_path_relative = None
        self.host_data_dir = None
        self.data_dir = None
        self.agent_id = None
        self._gpu_config: Optional[GPUFlag] = None
        self._log_filters = [pip_req_satisfied_filter]  # post_get_request_filter

        super().__init__(*args, **kwargs)

    def init_logger(self, loglevel=None):
        app_loglevel = self.info["appInfo"].get("logLevel")
        super().init_logger(loglevel=app_loglevel)

    def init_task_dir(self):
        # agent container paths
        self.dir_task = os.path.join(constants.AGENT_APP_SESSIONS_DIR(), str(self.info["task_id"]))
        self.dir_task_src = os.path.join(self.dir_task, "repo")
        # host paths
        self.dir_task_host = os.path.join(
            constants.AGENT_APP_SESSIONS_DIR_HOST(), str(self.info["task_id"])
        )

        team_id = self.info.get("context", {}).get("teamId", "unknown")
        if team_id == "unknown":
            self.logger.warn("teamId not found in context")
        self.dir_apps_cache_host = os.path.join(constants.AGENT_APPS_CACHE_DIR_HOST(), str(team_id))
        self.dir_apps_cache = os.path.join(constants.AGENT_APPS_CACHE_DIR(), str(team_id))

        sly.fs.ensure_base_path(self.dir_apps_cache)

        # task container path
        # self.dir_task_container = os.path.join("/sessions", str(self.info['task_id']))
        self.dir_task_container = "/app"

        self.dir_task_src_container = os.path.join(self.dir_task_container, "repo")
        self.dir_apps_cache_container = "/apps_cache"
        self.app_info = self.info["appInfo"]

    def download_or_get_repo(self):
        def is_fixed_version(name):
            try:
                v = Version(name)
                return True
            except ValueError as e:
                return False

        git_url = self.app_info["githubUrl"]
        version = self.app_info.get("version", "master")

        already_downloaded = False
        path_cache = None
        if version != "master" and is_fixed_version(version):
            path_cache = os.path.join(
                constants.APPS_STORAGE_DIR(),
                *Path(git_url.replace(".git", "")).parts[1:],
                version,
            )
            already_downloaded = sly.fs.dir_exists(path_cache)

        if already_downloaded is False:
            self.logger.info("Git repo will be downloaded")

            api = Api(self.info["server_address"], self.info["api_token"])
            tar_path = os.path.join(self.dir_task_src, "repo.tar.gz")

            api.app.download_git_archive(
                self.app_info["moduleId"],
                self.app_info["id"],
                version,
                tar_path,
                log_progress=True,
                ext_logger=self.logger,
            )
            with tarfile.open(tar_path) as archive:
                archive.extractall(self.dir_task_src)

            subdirs = get_subdirs(self.dir_task_src)
            if len(subdirs) != 1:
                sly.fs.log_tree(self.dir_task_src, self.logger)
                raise RuntimeError(
                    "Repo is downloaded and extracted, but resulting directory not found"
                )
            silent_remove(tar_path)
            extracted_path = os.path.join(self.dir_task_src, subdirs[0])

            temp_dir = os.path.join(self.dir_task_src, sly.rand_str(5))
            sly.fs.mkdir(temp_dir)
            for filename in os.listdir(extracted_path):
                shutil.move(
                    os.path.join(extracted_path, filename),
                    os.path.join(temp_dir, filename),
                )
            remove_dir(extracted_path)

            for filename in os.listdir(temp_dir):
                shutil.move(
                    os.path.join(temp_dir, filename),
                    os.path.join(self.dir_task_src, filename),
                )
            remove_dir(temp_dir)

            # git.download(git_url, self.dir_task_src, github_token, version)
            if path_cache is not None:
                shutil.copytree(self.dir_task_src, path_cache)
        else:
            self.logger.info("Git repo already exists")
            shutil.copytree(path_cache, self.dir_task_src, dirs_exist_ok=True)

        self.logger.info("Repo directory on host", extra={"dir": self.dir_task_src})
        sly.fs.log_tree(self.dir_task_src, self.logger, level="debug")

    def init_docker_image(self):
        self.download_or_get_repo()
        api = Api(self.info["server_address"], self.info["api_token"])
        module_id = self.info["appInfo"]["moduleId"]
        version = self.app_info.get("version", "master")
        self.logger.info("App moduleId == {} [v={}] in ecosystem".format(module_id, version))
        self.app_config = api.app.get_info(module_id, version)["config"]
        self.logger.info("App config", extra={"config": self.app_config})

        gpu = GPUFlag.from_config(self.app_config)
        self.docker_runtime = "runc"

        if gpu is not GPUFlag.skipped:
            docker_info = self._docker_api.info()
            nvidia = "nvidia"
            nvidia_available = False
            runtimes = docker_info.get("Runtimes", {})
            self.logger.info("Available docker runtimes", extra={"runtimes": runtimes})

            for runtime_name, runtime_info in runtimes.items():
                if nvidia in runtime_name or nvidia in runtime_info.get("path", ""):
                    nvidia_available = True
                    break

            if nvidia_available is False:
                if gpu is GPUFlag.required:
                    raise RuntimeError(
                        f"Runtime {nvidia} required to run the application, but it's not found in docker info."
                    )
                elif gpu is GPUFlag.preferred:
                    self.logger.warn(
                        (
                            f"Runtime {nvidia} not found in docker info, GPU features will be unavailable."
                            "If this app needs GPU, please, check nvidia drivers on the computer where agent"
                            "was spawned or contact tech support."
                        )
                    )
            else:
                self.docker_runtime = nvidia

        self._gpu_config = gpu
        self.logger.info(
            "Selected docker runtime",
            extra={"runtime": self.docker_runtime, "gpu": gpu.value},
        )

        # self.app_config = sly.io.json.load_json_file(os.path.join(self.dir_task_src, 'config.json'))
        self.read_dockerimage_from_config()
        super().init_docker_image()

    def read_dockerimage_from_config(self):
        self.info["app_info"] = self.app_config
        try:
            self.info["docker_image"] = self.app_config["docker_image"]
            if constants.APP_DEBUG_DOCKER_IMAGE() is not None:
                self.logger.info(
                    "APP DEBUG MODE: docker image {!r} is replaced to {!r}".format(
                        self.info["docker_image"], constants.APP_DEBUG_DOCKER_IMAGE()
                    )
                )
                self.info["docker_image"] = constants.APP_DEBUG_DOCKER_IMAGE()
            if constants.SLY_APPS_DOCKER_REGISTRY() is not None:
                self.logger.info(
                    "NON DEFAULT DOCKER REGISTRY: docker image {!r} is replaced to {!r}".format(
                        self.info["docker_image"], f"{constants.SLY_APPS_DOCKER_REGISTRY()}/{self.info['docker_image']}"
                    )
                )
                self.info["docker_image"] = f"{constants.SLY_APPS_DOCKER_REGISTRY()}/{self.info['docker_image']}"
            
        except KeyError as e:
            requirements_path = self.get_requirements_path()
            version = "latest"
            if sly.fs.file_exists(requirements_path):
                version_in_req = self.find_sdk_version(requirements_path)
                if version_in_req is not None:
                    version = version_in_req
            self.info["docker_image"] = constants.DEFAULT_APP_DOCKER_IMAGE() + ":" + version
            self.logger.info(
                f'Dockerimage not found in config.json, so it is set to default: {self.info["docker_image"]}'
            )

    def is_isolate(self):
        if self.app_config is None:
            raise RuntimeError("App config is not initialized")
        return True  # self.app_config.get(_ISOLATE, True)

    def _get_task_volumes(self):
        res = {}
        res[self.dir_task_host] = {"bind": self.dir_task_container, "mode": "rw"}
        res[self.dir_apps_cache_host] = {
            "bind": self.dir_apps_cache_container,
            "mode": "rw",
        }

        if self._need_sync_pip_cache is True:
            res[self._path_cache_host] = {
                "bind": _LINUX_DEFAULT_PIP_CACHE_DIR,
                "mode": "rw",
            }

        if constants.REQUESTS_CA_BUNDLE() is not None:
            res[constants.MOUNTED_HOST_REQUESTS_CA_BUNDLE()] = {
                "bind": constants.REQUESTS_CA_BUNDLE_DIR_CONTAINER(),
                "mode": "ro",
            }

        if constants.SUPERVISELY_AGENT_FILES() is not None:
            relative_app_data_dir = os.path.join(
                "app_data", slugify(self.app_config["name"]), str(self.info["task_id"])
            )

            self.host_data_dir = os.path.join(
                constants.SUPERVISELY_AGENT_FILES(),  # constants.SUPERVISELY_SYNCED_APP_DATA()
                relative_app_data_dir,
            )

            self.data_dir = os.path.join(
                constants.SUPERVISELY_AGENT_FILES_CONTAINER(), relative_app_data_dir
            )
            mkdir(self.data_dir)

            self.logger.info(
                "Task host data dir",
                extra={
                    "dir": self.host_data_dir,
                    "moduleId": self.info["appInfo"]["moduleId"],
                    "app name": self.app_config["name"],
                },
            )

            res[self.host_data_dir] = {"bind": _APP_CONTAINER_DATA_DIR, "mode": "rw"}
            res[constants.SUPERVISELY_AGENT_FILES()] = {
                "bind": constants.AGENT_FILES_IN_APP_CONTAINER(),
                "mode": "rw",
            }

            api = sly.Api(self.info["server_address"], self.info["api_token"])
            api.task.update_meta(
                int(self.info["task_id"]),
                {},
                agent_storage_folder=constants.SUPERVISELY_AGENT_FILES(),
                relative_app_dir=relative_app_data_dir,
            )
        else:
            self.logger.warn(
                (
                    "SUPERVISELY_AGENT_FILES environment variable is not defined inside agent container."
                    "If this is your production agent, please contact support."
                )
            )

        mount_settings = self.info.get("folderToMount", None)
        if mount_settings is not None:
            self.logger.info(f"Mount settings for task", extra={"folderToMount": mount_settings})
            host_folder = mount_settings.get("path")
            mode = mount_settings.get("mode", "ro")
            if mode not in ["ro", "rw"]:
                self.logger.warn(f"Unknown mounting mode: {mode}. Set to 'ro'")
                mode = "ro"
            if host_folder is not None and host_folder != "":
                self.logger.info(
                    f"Agent will mount host directory: {host_folder} [mode: {mode}]-> {_MOUNT_FOLDER_IN_CONTAINER}"
                )
                res[host_folder] = {
                    "bind": _MOUNT_FOLDER_IN_CONTAINER,
                    "mode": mode,
                }

        return res

    def download_step(self):
        pass

    def find_sdk_version(self, requirements_path):
        try:
            with pathlib.Path(requirements_path).open() as requirements_txt:
                for requirement in pkg_resources.parse_requirements(requirements_txt):
                    if requirement.project_name == "supervisely":
                        if len(requirement.specs) > 0 and len(requirement.specs[0]) >= 2:
                            version = requirement.specs[0][1]
                            return version
        except Exception as e:
            print(repr(e))
        return None

    def get_requirements_path(self):
        requirements_path = os.path.join(
            self.dir_task_src, self.app_info.get("configDir"), "requirements.txt"
        )
        if file_exists(requirements_path) is False:
            requirements_path = os.path.join(self.dir_task_src, "requirements.txt")
            if file_exists(requirements_path) is True:
                self._requirements_path_relative = "requirements.txt"
        else:
            self._requirements_path_relative = os.path.join(
                self.app_info.get("configDir"), "requirements.txt"
            )

        self.logger.info(f"Relative path to requirements: {self._requirements_path_relative}")
        return requirements_path

    def sync_pip_cache(self):
        version = self.app_info.get("version", "master")
        module_id = self.app_info.get("moduleId")

        requirements_path = self.get_requirements_path()

        path_cache = os.path.join(
            constants.APPS_PIP_CACHE_DIR(), str(module_id), version
        )  # in agent container
        self._path_cache_host = constants._agent_to_host_path(os.path.join(path_cache, "pip"))

        if sly.fs.file_exists(requirements_path):
            self._need_sync_pip_cache = True

            self.logger.info("requirements.txt:")
            with open(requirements_path, "r") as f:
                self.logger.info(f.read())

            if (
                sly.fs.dir_exists(path_cache) is False or version == "master"
            ) and constants.OFFLINE_MODE() is False:
                sly.fs.mkdir(path_cache)
                archive_destination = os.path.join(path_cache, "archive.tar")
                self.spawn_container(
                    add_envs=self.main_step_envs(),
                    add_labels={
                        "pip_cache": "1",
                        "app_session_id": str(self.info["task_id"]),
                    },
                )
                self.install_pip_requirements(container_id=self._container.id)

                # @TODO: handle 404 not found
                bits, stat = self._container.get_archive(_LINUX_DEFAULT_PIP_CACHE_DIR)
                self.logger.info(
                    "Download initial pip cache from dockerimage",
                    extra={
                        "dockerimage": self.docker_image_name,
                        "module_id": module_id,
                        "version": version,
                        "save_path": path_cache,
                        "stats": stat,
                        "default_pip_cache": _LINUX_DEFAULT_PIP_CACHE_DIR,
                        "archive_destination": archive_destination,
                    },
                )

                with open(archive_destination, "wb") as archive:
                    for chunk in bits:
                        archive.write(chunk)

                with tarfile.open(archive_destination) as archive:
                    archive.extractall(path_cache)
                sly.fs.silent_remove(archive_destination)
            else:
                self.logger.info("Use existing pip cache")

    @handle_exceptions
    def find_or_run_container(self):
        add_labels = {"sly_app": "1", "app_session_id": str(self.info["task_id"])}
        sly.docker_utils.docker_pull_if_needed(
            self._docker_api,
            self.docker_image_name,
            constants.PULL_POLICY(),
            self.logger,
        )
        self.sync_pip_cache()
        if self._container is None:
            try:
                self.spawn_container(add_envs=self.main_step_envs(), add_labels=add_labels)
            except APIError as api_ex:
                msg = api_ex.args[0].response.text
                clear_msg = json.loads(msg)["message"]
                is_runtime_err = "runtime create failed" in clear_msg
                orig_runtime = self.docker_runtime

                if (
                    is_runtime_err
                    and (self.docker_runtime == "nvidia")
                    and (self._gpu_config is GPUFlag.preferred)
                ):
                    self.logger.warn("Can't start docker container. Trying to use another runtime.")
                    self.docker_runtime = "runc"

                    if self._container_name is None:
                        raise KeyError("Container name is not defined. Please, contact support.")

                    try:
                        container = self._docker_api.containers.get(self._container_name)
                    except NotFound as nf_ex:
                        self.logger.error(
                            (
                                "The created container was not found in the list of existing ones."
                                "Please, contact support."
                            )
                        )
                        raise nf_ex

                    container.remove()
                    self.spawn_container(add_envs=self.main_step_envs(), add_labels=add_labels)
                else:
                    self.logger.exception(api_ex)
                    if is_runtime_err:
                        raise DockerException(
                            (
                                "Error while trying to start the container "
                                f"with runtime={orig_runtime}. "
                                "Check your nvidia drivers, delete gpu flag from application config "
                                "or reaplace it with gpu=`preferred`. "
                                f"Docker exception message: {clear_msg}"
                            )
                        )
                    raise api_ex

            if constants.OFFLINE_MODE() is False:
                self.logger.info("Double check pip cache for old agents")
                self.install_pip_requirements(container_id=self._container.id)
                self.logger.info("pip second install for old agents is finished")

    def get_spawn_entrypoint(self):
        inf_command = "while true; do sleep 30; done;"
        self.logger.info("Infinite command", extra={"command": inf_command})
        return ["sh", "-c", inf_command]

    def _exec_command(self, command, add_envs=None, container_id=None):
        add_envs = sly.take_with_default(add_envs, {})
        self._exec_id = self._docker_api.api.exec_create(
            self._container.id if container_id is None else container_id,
            cmd=command,
            environment={
                "LOG_LEVEL": "DEBUG",
                "LANG": "C.UTF-8",
                "PYTHONUNBUFFERED": "1",
                constants._HTTP_PROXY: constants.HTTP_PROXY(),
                constants._HTTPS_PROXY: constants.HTTPS_PROXY(),
                "HOST_TASK_DIR": self.dir_task_host,
                "TASK_ID": self.info["task_id"],
                "SERVER_ADDRESS": self.info["server_address"],
                "API_TOKEN": self.info["api_token"],
                "AGENT_TOKEN": constants.TOKEN(),
                "PIP_ROOT_USER_ACTION": "ignore",
                **add_envs,
            },
        )
        self._logs_output = self._docker_api.api.exec_start(self._exec_id, stream=True)

    def exec_command(self, add_envs=None, command=None):
        add_envs = sly.take_with_default(add_envs, {})
        main_script_path = os.path.join(
            self.dir_task_src_container,
            self.app_config.get("main_script", "src/main.py"),
        )
        if command is None:
            # command = f'export PYTHONPATH="$PYTHONPATH:{self.dir_task_src_container}" && python {main_script_path}'
            command = f'bash -c "export PYTHONPATH="$PYTHONPATH:{self.dir_task_src_container}" && python {main_script_path}"'

        if "entrypoint" in self.app_config:
            command = (
                f'bash -c "cd {self.dir_task_src_container} && {self.app_config["entrypoint"]}"'
            )
        self.logger.info("command to run", extra={"command": command})
        self._exec_command(command, add_envs)

        # change pulling progress to app progress
        progress_dummy = sly.Progress("Application is started ...", 1, ext_logger=self.logger)
        progress_dummy.iter_done_report()
        self.logger.info("command is running", extra={"command": command})

    def install_pip_requirements(self, container_id=None):
        if self._need_sync_pip_cache is True:
            self.logger.info("Installing app requirements")
            progress_dummy = sly.Progress(
                "Installing app requirements...", 1, ext_logger=self.logger
            )
            progress_dummy.iter_done_report()
            # --root-user-action=ignore
            command = "pip3 install --disable-pip-version-check -r " + os.path.join(
                self.dir_task_src_container, self._requirements_path_relative
            )
            self.logger.info(f"PIP command: {command}")
            self._exec_command(command, add_envs=self.main_step_envs(), container_id=container_id)
            self.process_logs()
            self.logger.info("Requirements are installed")

    def main_step(self):
        api = Api(self.info["server_address"], self.info["api_token"])
        task_info_from_server = api.task.get_info_by_id(int(self.info["task_id"]))
        self.agent_id = task_info_from_server.get("agentId")
        self.logger.info(f"Agent ID = {self.agent_id}")

        base_url = self.info["appInfo"].get("baseUrl")
        if base_url is not None:
            # base_url.lstrip("/")
            app_url = urllib.parse.urljoin(self.info["server_address"], base_url)
            self.logger.info(f"✅ To access the app in browser, copy and paste this URL: {app_url}")
        else:
            self.logger.warn("baseUrl not found in task info")

        self.find_or_run_container()
        self.exec_command(add_envs=self.main_step_envs())
        self.process_logs()
        self.drop_container_and_check_status()
        # if exit_code != 0 next code will never execute
        if self.data_dir is not None and sly.fs.dir_exists(self.data_dir):
            parent_app_dir = Path(self.data_dir).parent
            sly.fs.remove_dir(self.data_dir)
            if sly.fs.dir_empty(parent_app_dir) and len(sly.fs.get_subdirs(parent_app_dir)) == 0:
                sly.fs.remove_dir(parent_app_dir)

    def upload_step(self):
        pass

    def main_step_envs(self):
        context = self.info.get("context", {})

        context_envs = {}
        if len(context) > 0:
            context_envs = flatten_json(context)
            context_envs = modify_keys(context_envs, prefix="context.")

        modal_state = self.info.get("state", {})
        modal_envs = {}
        if len(modal_state) > 0:
            modal_envs = flatten_json(modal_state)
            modal_envs = modify_keys(modal_envs, prefix="modal.state.")

        envs = {
            "CONTEXT": json.dumps(context),
            "MODAL_STATE": json.dumps(modal_state),
            **modal_envs,
            # session owner (sometimes labeler)
            "USER_ID": context.get("userId"),  # labeler
            "USER_LOGIN": context.get("userLogin"),  # labeler
            "API_TOKEN": context.get("apiToken"),  # labeler
            # info who spawns application (manager)
            "_SPAWN_USER_ID": context.get("spawnUserId"),  # labeler
            "_SPAWN_USER_LOGIN": context.get("spawnUserLogin"),  # manager
            "_SPAWN_API_TOKEN": context.get("spawnApiToken"),  # manager
            "TEAM_ID": context.get("teamId"),
            "CONFIG_DIR": self.info["appInfo"].get("configDir", ""),
            "BASE_URL": self.info["appInfo"].get("baseUrl", ""),
            **context_envs,
            SUPERVISELY_TASK_ID: str(self.info["task_id"]),
            "LOG_LEVEL": str(self.app_info.get("logLevel", "INFO")),
            "LOGLEVEL": str(self.app_info.get("logLevel", "INFO")),
            "PYTHONUNBUFFERED": 1,
            "SLY_APP_DATA_DIR": _APP_CONTAINER_DATA_DIR,
            constants._SUPERVISELY_AGENT_FILES: constants.SUPERVISELY_AGENT_FILES(),
            "SUPERVISELY_SYNCED_APP_DATA": constants.SUPERVISELY_SYNCED_APP_DATA(),
            "APP_MODE": "production",  # or "development"
            "ENV": "production",  # the same as "APP_MODE"
            "APP_NAME": self.app_config.get("name", "Supervisely App"),
            "icon": self.app_config.get("icon", "https://cdn.supervise.ly/favicon.ico"),
            "PIP_ROOT_USER_ACTION": "ignore",
            "AGENT_ID": self.agent_id,
        }

        if "context.workspaceId" in envs:
            envs["WORKSPACE_ID"] = envs["context.workspaceId"]

        if "modal.state.slyProjectId" in modal_envs:
            envs["context.projectId"] = modal_envs["modal.state.slyProjectId"]
            envs["PROJECT_ID"] = modal_envs["modal.state.slyProjectId"]

        if "modal.state.slyDatasetId" in modal_envs:
            envs["context.datasetId"] = modal_envs["modal.state.slyDatasetId"]
            envs["DATASET_ID"] = modal_envs["modal.state.slyDatasetId"]

        if "modal.state.slyFile" in modal_envs:
            envs["context.slyFile"] = modal_envs["modal.state.slyFile"]
            envs["FILE"] = modal_envs["modal.state.slyFile"]

        if "modal.state.slyFolder" in modal_envs:
            envs["context.slyFolder"] = modal_envs["modal.state.slyFolder"]
            envs["FOLDER"] = modal_envs["modal.state.slyFolder"]

        if constants.DOCKER_NET() is not None:
            envs["VIRTUAL_HOST"] = f'task-{self.info["task_id"]}.supervisely.local'
            envs["VIRTUAL_PORT"] = self.app_config.get("port", 8000)
        else:
            self.logger.info("⚠️ Supervisely network is not defined in ENV")

        if constants.SUPERVISELY_AGENT_FILES() is not None:
            envs["AGENT_STORAGE"] = constants.AGENT_FILES_IN_APP_CONTAINER()

        if constants.REQUESTS_CA_BUNDLE() is not None:
            envs[constants._REQUESTS_CA_BUNDLE] = constants.REQUESTS_CA_BUNDLE_CONTAINER()

        # Handle case for some dockerimages where env names with dot sumbol are not supported
        final_envs = copy.deepcopy(envs)
        for k, v in envs.items():
            if "." in k:
                new_k = k.replace(".", "_").upper()
                final_envs[new_k] = v

        return final_envs

    def process_logs(self):
        logs_found = False

        def _process_line(log_line):
            # log_line = log_line.decode("utf-8")
            msg, res_log, lvl = self.parse_log_line(log_line)
            self._process_report(msg)
            output = self.call_event_function(res_log)

            lvl_description = sly.LOGGING_LEVELS.get(lvl, None)
            if lvl_description is not None:
                lvl_int = lvl_description.int
            else:
                lvl_int = sly.LOGGING_LEVELS["INFO"].int

            lvl_int = filter_log_line(msg, lvl_int, self._log_filters)
            if lvl_int != -1:
                self.logger.log(lvl_int, msg, extra=res_log)

        # @TODO: parse multiline logs correctly (including exceptions)
        log_line = ""

        for log_line_arr in self._logs_output:
            for log_part in log_line_arr.decode("utf-8").splitlines():
                logs_found = True
                _process_line(log_part)

        if not logs_found:
            self.logger.warn("No logs obtained from container.")  # check if bug occurred

    def _stop_wait_container(self):
        if self.is_isolate():
            return super()._stop_wait_container()
        else:
            return self.exec_stop()

    def exec_stop(self):
        exec_info = self._docker_api.api.exec_inspect(self._exec_id)
        if exec_info["Running"] == True:
            pid = exec_info["Pid"]
            self._container.exec_run(cmd="kill {}".format(pid))
        else:
            return

    def _drop_container(self):
        if self.is_isolate():
            super()._drop_container()
        else:
            self.exec_stop()

    def drop_container_and_check_status(self):
        status = self._docker_api.api.exec_inspect(self._exec_id)["ExitCode"]
        if self.is_isolate():
            self._drop_container()
        self.logger.debug("Task container finished with status: {}".format(str(status)))
        if status != 0:
            if len(self._task_reports) > 0:
                last_report = self._task_reports[-1].to_dict()
                self.logger.debug("Founded error report.", extra=last_report)
                raise sly.app.exceptions.DialogWindowError(**last_report)
            raise RuntimeError(
                # self.logger.warn(
                "Task container finished with non-zero status: {}".format(str(status))
            )
