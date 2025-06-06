# coding: utf-8

import json
import os
import os.path as osp
import queue
import re
import time
import requests
import docker
import shutil
import tarfile
import io

import supervisely_lib as sly
from supervisely_lib._utils import (  # pylint: disable=import-error, no-name-in-module
    get_certificates_list,
)

from logging import Logger
from typing import Callable, List, Optional, Tuple, Union, Container
from datetime import datetime, timedelta
from pathlib import Path
from docker import DockerClient
from docker.errors import APIError, ImageNotFound
from filelock import FileLock

from worker import constants
from worker.system_info import get_container_info, _get_self_container_idx
from worker import docker_utils


class AgentOptionsNotAvailable(RuntimeError):
    pass


class AgentOptionsJsonFields:
    AGENT_OPTIONS = "agentOptions"
    AGENT_HOST_DIR = "agentDataHostDir"
    DELETE_TASK_DIR_ON_FAILURE = "deleteTaskDirOnFailure"
    DELETE_TASK_DIR_ON_FINISH = "deleteTaskDirOnFinish"
    DOCKER_CREDS = "dockerCreds"
    DOCKER_LOGIN = "login"
    DOCKER_PASSWORD = "password"
    DOCKER_REGISTRY = "registry"
    SERVER_ADDRESS = "serverAddress"
    SERVER_ADDRESS_INTERNAL = "internalServerAddress"
    SERVER_ADDRESS_EXTERNAL = "externalServerAddress"
    OFFLINE_MODE = "offlineMode"
    PULL_POLICY = "pullPolicy"
    SUPERVISELY_AGENT_FILES = "slyAppsDataHostDir"
    SLY_APPS_DOCKER_REGISTRY = "slyAppsDockerRegistry"
    NO_PROXY = "noProxy"
    CPU_LIMIT = "cpuLimit"
    MEM_LIMIT = "memLimit"
    HTTP_PROXY = "httpProxy"
    SECURITY_OPT = "securityOpts"
    NET_OPTIONS = "netClientOptions"
    NET_CLIENT_DOCKER_IMAGE = "dockerImage"
    NET_SERVER_ADDRESS = "netServerAddress"
    NET_SERVER_PORT = "netServerPort"
    DOCKER_IMAGE = "dockerImage"
    FORCE_CPU_ONLY = "forceCPUOnly"
    LOG_LEVEL = "logLevel"
    IMAGES_CLEAN_INTERVAL = "imagesCleanInterval"


def create_img_meta_str(img_size_bytes, width, height):
    img_meta = {"size": img_size_bytes, "width": width, "height": height}
    res = json.dumps(img_meta)
    return res


def ann_special_fields():
    return "img_hash", "img_ext", "img_size_bytes"


# multithreading
class LogQueue:
    def __init__(self):
        self.q = queue.Queue()  # no limit

    def put_nowait(self, log_line):
        self.q.put_nowait(log_line)

    def _get_batch_nowait(self, batch_limit):
        log_lines = []
        for _ in range(batch_limit):
            try:
                log_line = self.q.get_nowait()
            except queue.Empty:
                break
            log_lines.append(log_line)
        return log_lines

    def get_log_batch_nowait(self):
        res = self._get_batch_nowait(constants.BATCH_SIZE_LOG())
        return res

    def get_log_batch_blocking(self):
        first_log_line = self.q.get(block=True)
        rest_lines = self._get_batch_nowait(constants.BATCH_SIZE_LOG() - 1)
        log_lines = [first_log_line] + rest_lines
        return log_lines


class TaskDirCleaner:
    def __init__(self, dir_task):
        self.dir_task = dir_task
        self.marker_fpath = osp.join(self.dir_task, "__do_not_clean.marker")

    def _clean(self):
        for elem in filter(lambda x: "logs" not in x, os.listdir(self.dir_task)):
            del_path = osp.join(self.dir_task, elem)
            if osp.isfile(del_path):
                os.remove(del_path)
            else:
                shutil.rmtree(del_path)

    def forbid_dir_cleaning(self):
        with open(self.marker_fpath, "a"):
            os.utime(self.marker_fpath, None)  # touch file

    def allow_cleaning(self):
        if osp.isfile(self.marker_fpath):
            os.remove(self.marker_fpath)

    def clean(self):
        if constants.DELETE_TASK_DIR_ON_FINISH() is False:
            return
        if constants.DELETE_TASK_DIR_ON_FAILURE() is False and osp.isfile(self.marker_fpath):
            return
        self._clean()

    def clean_forced(self):
        self._clean()


class AppDirCleaner:
    def __init__(self, logger: Logger) -> None:
        self.logger = logger

    def clean_agent_logs(self):
        root_path = Path(constants.AGENT_LOG_DIR())
        removed = []

        old_logs = self._get_old_files_or_folders(root_path, only_files=True)
        for log_path in old_logs:
            sly.fs.silent_remove(log_path)
            removed.append(log_path)

        self.logger.debug(f"Removed agent logs: {removed}")

    def clean_app_sessions(
        self,
        auto=False,
        working_apps: Optional[Container[int]] = None,
    ) -> List[str]:
        """Delete sessions logs and repository clones of finished/crashed apps"""
        root_path = Path(constants.AGENT_APP_SESSIONS_DIR())
        cleaned_sessions = []

        if auto is True:
            old_apps = self._get_old_files_or_folders(root_path, only_folders=True)
        else:
            # get all files and folders
            old_apps = self._get_old_files_or_folders(
                root_path, only_folders=True, age_limit=timedelta(days=0)
            )

        for app in old_apps:
            app_path = Path(app)
            app_id = app_path.name

            if not os.path.exists(app_path / "__do_not_clean.marker"):
                cleaned_sessions.append(app_id)
                sly.fs.remove_dir(app)
                continue

            if self._check_task_id_finished_or_crashed(app_id, working_apps):
                cleaned_sessions.append(app_id)
                sly.fs.remove_dir(app)

        self.logger.debug(f"Removed sessions: {cleaned_sessions}")

        return cleaned_sessions

    def clean_app_files(self, cleaned_sessions: List[str]):
        """Delete files, artifacts used in finished/crashed apps"""
        if constants.SUPERVISELY_SYNCED_APP_DATA_CONTAINER() is not None:
            root_path = Path(constants.SUPERVISELY_SYNCED_APP_DATA_CONTAINER())
            known_sessions = os.listdir(constants.AGENT_APP_SESSIONS_DIR())
        else:
            return

        for app_name in os.listdir(root_path):
            app_path = root_path / app_name
            if os.path.isdir(app_path):
                for task_id in os.listdir(app_path):
                    task_path = app_path / task_id

                    to_del = False
                    if task_id in cleaned_sessions:
                        to_del = True
                    elif task_id not in known_sessions:
                        to_del = True

                    if to_del and os.path.isdir(task_path):
                        sly.fs.remove_dir(task_path)

                if sly.fs.dir_empty(app_path):
                    sly.fs.remove_dir(app_path)

    def clean_pip_cache(self, auto=False, pip_cache_dir: str = None):
        if pip_cache_dir is None:
            pip_cache_dir = constants.APPS_PIP_CACHE_DIR()
        root_path = Path(pip_cache_dir)
        removed = []

        for module_id in os.listdir(root_path):
            module_caches_path = root_path / module_id

            if auto is True:
                old_cache = self._get_old_files_or_folders(module_caches_path, only_folders=True)
            else:
                old_cache = self._get_old_files_or_folders(
                    module_caches_path, only_folders=True, age_limit=timedelta(days=0)
                )

            for ver_path in old_cache:
                removed.append(ver_path)
                sly.fs.remove_dir(ver_path)

            if sly.fs.dir_empty(module_caches_path):
                sly.fs.remove_dir(module_caches_path)

        self.logger.debug(f"Removed PIP cache: {removed}")

    def clean_git_tags(self):
        # TODO: add conditions?
        root_path = Path(constants.APPS_STORAGE_DIR())
        sly.fs.remove_dir(str(root_path / "github.com"))

    def auto_clean(self, working_apps: Container[int]):
        self.logger.info("Auto cleaning task started.")
        self._allow_manual_cleaning_if_not_launched(working_apps)
        self._apps_cleaner(working_apps, auto=True)
        self.clean_agent_logs()

    def clean_all_app_data(self, working_apps: Optional[Container[int]] = None):
        self.logger.info("Cleaning apps data.")
        self._apps_cleaner(working_apps, auto=False, clean_pip=False, clean_apps_cache=True)
        self.clean_git_tags()

    def clean_apps_cache(self, apps_cache_dir: str = None):
        if apps_cache_dir is None:
            apps_cache_dir = constants.AGENT_APPS_CACHE_DIR()
        cleaned_space = 0
        for p in Path(apps_cache_dir).iterdir():
            if p.is_dir():
                cleaned_space += sly.fs.get_directory_size(p.as_posix())
                sly.fs.remove_dir(p)
            else:
                cleaned_space += sly.fs.get_file_size(p.as_posix())
                p.unlink()
        self.logger.debug("Apps cache cleaned. Space freed: %s bytes", cleaned_space)

    def _apps_cleaner(
        self,
        working_apps: Optional[Container[int]],
        auto: bool = False,
        clean_pip: bool = True,
        clean_apps_cache: bool = False,
    ):
        cleaned_sessions = self.clean_app_sessions(auto=auto, working_apps=working_apps)
        if auto is False:
            self.clean_app_files(cleaned_sessions)
        if clean_pip is True:
            self.clean_pip_cache(auto=auto)
        if clean_apps_cache is True:
            self.clean_apps_cache()

    def _get_log_datetime(self, log_name) -> datetime:
        return datetime.strptime(log_name, "log_%Y-%m-%d_%H:%M:%S.txt")

    def _get_file_or_path_datetime(self, path: Union[Path, str]) -> datetime:
        time_sec = max(os.path.getmtime(path), os.path.getatime(path))
        return datetime.fromtimestamp(time_sec)

    def _get_old_files_or_folders(
        self,
        parent_path: Union[Path, str],
        only_files: bool = False,
        only_folders: bool = False,
        age_limit: Union[timedelta, int] = constants.AUTO_CLEAN_TIMEDELTA_DAYS(),
    ) -> List[str]:
        """Return abs path for folders/files which last modification/access
        datetime is greater than constants.AUTO_CLEAN_TIMEDELTA_DAYS (default: 7);
        use `AUTO_CLEAN_INT_RANGE_DAYS` env var to setup.

        :param parent_path: path to serach
        :type parent_path: Union[Path, str]
        :param only_files: return will content only files paths, defaults to False
        :type only_files: bool, optional
        :param only_folders: return will content only folders paths, defaults to False
        :type only_folders: bool, optional
        :param age_limit: max age of file or folder.
            If `type(age_limit)` is int, will convert to `timedelta(day=age_limit)`;
            defaults to constants.AUTO_CLEAN_TIMEDELTA_DAYS
        :type age_limit: timedelta, optional
        :raises ValueError: `only_files` and `only_folders` can't be True simultaneously
        :return: list of absolute paths
        :rtype: List[str]
        """
        if (only_files and only_folders) is True:
            raise ValueError("only_files and only_folders can't be True simultaneously.")

        if isinstance(age_limit, int):
            age_limit = timedelta(days=age_limit)

        now = datetime.now()
        ppath = Path(parent_path)
        old_path_files = []
        for file_or_path in os.listdir(parent_path):
            full_path = ppath / file_or_path

            if only_files and os.path.isdir(full_path):
                continue
            elif only_folders and os.path.isfile(full_path):
                continue

            file_datetime = self._get_file_or_path_datetime(full_path)
            if (now - file_datetime) > age_limit:
                old_path_files.append(str(full_path))

        return old_path_files

    def _check_task_id_finished_or_crashed(
        self,
        task_id: Union[str, int],
        working_apps: Optional[Container[int]],
    ) -> bool:
        try:
            task_id = int(task_id)
        except ValueError as exc:
            self.logger.exception(exc)
            return False

        if working_apps is None:
            return False

        return task_id not in working_apps

    def _allow_manual_cleaning_if_not_launched(self, working_apps: Container[int]):
        root_path = Path(constants.AGENT_APP_SESSIONS_DIR())
        allow_for_cleaner = []
        for task_id in os.listdir(root_path):
            allow = False
            try:
                if int(task_id) not in working_apps:
                    allow = True
            except ValueError:
                pass

            dir_task = str(root_path / task_id)
            if allow and os.path.exists(dir_task):
                allow_for_cleaner.append(task_id)
                cleaner = TaskDirCleaner(dir_task)
                cleaner.allow_cleaning()
                cleaner.clean()

        if len(allow_for_cleaner) > 0:
            self.logger.info(f"Files for this session can be manually removed: {allow_for_cleaner}")


class DockerImagesCleaner:
    def __init__(self, docker_api: DockerClient, logger: Logger) -> None:
        self._days_before_delete = timedelta(days=constants.REMOVE_IDLE_DOCKER_IMAGE_AFTER_X_DAYS())
        self.logger = logger
        self.path_to_history = constants.CROSS_AGENT_DATA_DIR()
        self.docker_api = docker_api

    def remove_idle_images(self):
        if self.path_to_history is None:
            self.logger.debug(
                f"{constants._CROSS_AGENT_DATA_DIR} has not been set; the process of removing unused Docker Images will not be executed"
            )
            return

        if constants.OFFLINE_MODE() is True:
            self.logger.debug(
                f"Offline mode is enabled; the process of removing unused Docker Images will not be executed"
            )
            return

        all_hists = sly.fs.list_files(
            self.path_to_history, filter_fn=self._is_history, valid_extensions=".json"
        )
        lock_file = os.path.join(self.path_to_history, "docker-images-lock.txt")

        if sly.fs.file_exists(lock_file):
            if os.path.getctime(lock_file) < datetime.now().timestamp() - 60 * 60 * 24:
                self.logger.info(
                    "Skip DockerImagesCleaner task: lock file is too old. Will try to remove it."
                )
                sly.fs.silent_remove(lock_file)
            else:
                self.logger.info(
                    "Skip DockerImagesCleaner task: another agent is already working on this task"
                )
                return

        self.logger.info("DockerImagesCleaner started: old images will be removed.")
        sly.fs.touch(lock_file)

        try:
            # check all
            to_remove = self._parse_all_hists(all_hists)
            for image in to_remove:
                try:
                    self.docker_api.api.remove_image(image)
                    self.logger.info(f"Image {image} has been successfully removed.")
                except (APIError, ImageNotFound) as exc:
                    reason = exc.response.json().get("message")
                    self.logger.info(f"Skip {image}: {reason}")
            self.docker_api.api.prune_images({"dangling": True})
        finally:
            sly.fs.silent_remove(lock_file)
            self.logger.info("DockerImagesCleaner finished.")

    def _is_history(self, filename: str) -> bool:
        return "docker-images-history-" in filename

    def _parse_all_hists(self, hist_paths: List[str]) -> List[str]:
        to_remove = {}
        for hist in hist_paths:
            to_remove = self._parse_and_update_history(hist, to_remove)
        return list(to_remove.keys())

    def _is_base_image(self, image_name: str):
        for base_image_name in constants.BASE_IMAGES():
            if image_name.endswith(base_image_name):
                return True
        return False

    def _parse_and_update_history(self, hist_path: str, to_remove: dict):
        hist_lock = FileLock(f"{hist_path}.lock")

        with hist_lock:
            with open(hist_path, "r") as json_file:
                images_data: dict = json.load(json_file)

            rest_images = {}
            for image, last_date in images_data.items():
                image: str
                if not self._is_base_image(image) and self._is_unused(last_date):
                    to_remove[image] = last_date
                else:
                    rest_images[image] = last_date
                    if image in to_remove:
                        del to_remove[image]

            with open(hist_path, "w") as json_file:
                json.dump(rest_images, json_file, indent=4)

        return to_remove

    def _is_unused(self, last_date: Union[str, datetime]) -> bool:
        last_date_ts = last_date
        if isinstance(last_date, str):
            last_date_ts = datetime.strptime(last_date, "%Y-%m-%dT%H:%M")
        return datetime.now() - last_date_ts > self._days_before_delete


# @TODO: remove this method or refactor it in future (dict_name - WTF??)
def get_single_item_or_die(src_dict, key, dict_name):
    results = src_dict.get(key, None)
    if results is None:
        raise ValueError(
            "No values were found for {} in {}. A list with exactly one item is required.".format(
                key, dict_name
            )
        )
    if len(results) != 1:
        raise ValueError(
            "Multiple values ({}) were found for {} in {}. A list with exactly one item is required.".format(
                len(results), key, dict_name
            )
        )
    return results[0]


def add_creds_to_git_url(git_url):
    old_str = None
    if "https://" in git_url:
        old_str = "https://"
    elif "http://" in git_url:
        old_str = "http://"
    res = git_url
    if constants.GIT_LOGIN() is not None and constants.GIT_PASSWORD() is not None:
        res = git_url.replace(
            old_str, "{}{}:{}@".format(old_str, constants.GIT_LOGIN(), constants.GIT_PASSWORD())
        )
        return res
    else:
        return git_url


def filter_log_line(
    msg: str,
    cur_log_level: int,
    line_filters: Optional[List[Callable[[str, int], int]]] = None,
) -> int:
    """Change log level using providet filters.

    :param msg: log message
    :type msg: str
    :param cur_log_level: current log level code (sly.LOGGING_LEVELS)
    :type cur_log_level: int
    :param line_filters: function receiving a message and current logging level
        as input and returns a new logging level for current msg;
        if `-1` is returned, the line will be skipped; defaults to None
    :type line_filters: Optional[List[Callable[[str], int]]], optional
    :return: new logging level
    :rtype: int
    """
    if line_filters is None:
        return cur_log_level

    new_level = cur_log_level
    for line_filter in line_filters:
        new_level = line_filter(msg, new_level)
        if cur_log_level != new_level:
            return new_level

    return cur_log_level


def pip_req_satisfied_filter(msg: str, cur_level: int) -> int:
    if "Requirement already satisfied" in msg:
        return sly.LOGGING_LEVELS["DEBUG"].int
    return cur_level


def post_get_request_filter(msg: str, cur_level: int) -> int:
    pattern = r".*?(GET|POST).*?(200 OK)"
    if re.match(pattern, msg) is not None:
        return sly.LOGGING_LEVELS["DEBUG"].int
    return cur_level


def value_to_str(value):
    if isinstance(value, list):
        return ",".join(value)
    if value is None:
        return ""
    if isinstance(value, bool):
        return str(value).lower()
    return str(value)


def str_to_value(value_str):
    if value_str == "":
        return None
    if value_str == "true":
        return True
    if value_str == "false":
        return False
    if "," in value_str:
        return value_str.split(",")
    return value_str


def envs_list_to_dict(envs_list: List[str]) -> dict:
    envs_dict = {}
    for env in envs_list:
        env_split_arr = env.split("=", maxsplit=1)

        if len(env_split_arr) == 1:
            env_name = env_split_arr[0]
            env_value = ""
        else:
            env_name, env_value = env_split_arr

        envs_dict[env_name] = str_to_value(env_value)
    return envs_dict


def envs_dict_to_list(envs_dict: dict) -> List[str]:
    envs_list = []
    for env_name, env_value in envs_dict.items():
        envs_list.append(f"{env_name}={value_to_str(env_value)}")
    return envs_list


def binds_to_volumes_dict(binds: List[str]) -> dict:
    volumes = {}
    for bind in binds:
        split = bind.split(":")
        src = split[0]
        dst = split[1]
        mode = split[2] if len(split) == 3 else "rw"
        volumes[src] = {"bind": dst, "mode": mode}
    return volumes


def volumes_dict_to_binds(volumes: dict) -> List[str]:
    binds = []
    for src, dst in volumes.items():
        bind = f"{src}:{dst['bind']}:{dst['mode']}" if "mode" in dst else f"{src}:{dst['bind']}"
        binds.append(bind)
    return binds


def get_agent_options(server_address=None, token=None, timeout=60) -> dict:
    if server_address is None:
        server_address = constants.SERVER_ADDRESS()
    if token is None:
        token = constants.TOKEN()

    api = sly.Api(server_address=server_address)
    method = "agents.options.info"

    resp = api.post(
        method,
        data={"token": token},
    )
    if resp is None or resp.status_code != requests.codes.ok:  # pylint: disable=no-member
        try:
            text = resp.text
        except:
            text = None
        msg = f"Can't get agent options from server {server_address}."
        if text is not None:
            msg += f" Response: {text}"
        raise AgentOptionsNotAvailable(msg)
    return resp.json()


def get_instance_version(server_address=None, timeout=60):
    if server_address is None:
        server_address = constants.SERVER_ADDRESS()

    api = sly.Api(server_address=server_address)
    resp = api.get("instance.version", {})
    if resp is None or resp.status_code != requests.codes.ok:  # pylint: disable=no-member
        if resp is not None and resp.status_code in (400, 401, 403, 404):
            return None
        try:
            text = resp.text
        except:
            text = None
        msg = f"Can't get instance version from server {server_address}."
        if text is not None:
            msg += f" Response: {text}"
        raise RuntimeError(msg)
    return resp.json()["version"]


def updated_agent_options() -> Tuple[dict, dict, str]:
    env = {}

    def update_env_param(name, value, default=None):
        if value is None or value == "":
            if name in constants.get_required_settings():
                raise RuntimeError(f'Required option "{name}" is empty.')
            value = default
        env[name] = value

    params = get_agent_options()
    options: dict = params[AgentOptionsJsonFields.AGENT_OPTIONS]
    net_options: dict = params[AgentOptionsJsonFields.NET_OPTIONS]
    ca_cert = params["caCert"]
    http_proxy = params.get("httpProxy", None)
    no_proxy = params.get("noProxy", constants.NO_PROXY())

    optional_defaults = constants.get_optional_defaults()

    update_env_param(constants._ACCESS_TOKEN, constants.TOKEN())
    update_env_param(
        constants._SUPERVISELY_AGENT_FILES,
        options.get(AgentOptionsJsonFields.SUPERVISELY_AGENT_FILES, None),
        optional_defaults[constants._SUPERVISELY_AGENT_FILES],
    )
    update_env_param(
        constants._SLY_APPS_DOCKER_REGISTRY,
        options.get(AgentOptionsJsonFields.SLY_APPS_DOCKER_REGISTRY, None),
        optional_defaults[constants._SLY_APPS_DOCKER_REGISTRY],
    )
    update_env_param(
        constants._DELETE_TASK_DIR_ON_FAILURE,
        str(options.get(AgentOptionsJsonFields.DELETE_TASK_DIR_ON_FAILURE, "")).lower(),
        optional_defaults[constants._DELETE_TASK_DIR_ON_FAILURE],
    )
    update_env_param(
        constants._DELETE_TASK_DIR_ON_FINISH,
        str(options.get(AgentOptionsJsonFields.DELETE_TASK_DIR_ON_FINISH, "")).lower(),
        optional_defaults[constants._DELETE_TASK_DIR_ON_FINISH],
    )

    docker_cr = options.get(AgentOptionsJsonFields.DOCKER_CREDS, [])
    docker_login = ",".join([cr[AgentOptionsJsonFields.DOCKER_LOGIN] for cr in docker_cr])
    docker_pass = ",".join([cr[AgentOptionsJsonFields.DOCKER_PASSWORD] for cr in docker_cr])
    docker_reg = ",".join([cr[AgentOptionsJsonFields.DOCKER_REGISTRY] for cr in docker_cr])
    os.environ["DOCKER_LOGIN"] = ""
    os.environ["DOCKER_PASSWORD"] = ""
    os.environ["DOCKER_REGISTRY"] = ""
    constants._VALUES[constants._DOCKER_LOGIN] = docker_login
    constants._VALUES[constants._DOCKER_PASSWORD] = docker_pass
    constants._VALUES[constants._DOCKER_REGISTRY] = docker_reg
    auth_log = docker_utils.hidden_auth()
    sly.logger.debug("Updated Docker credentials:", extra={"auth": auth_log})

    # TODO: save all server addresses
    server_address = options.get(AgentOptionsJsonFields.SERVER_ADDRESS, None)
    if server_address is None or server_address == "":
        server_address = options.get(AgentOptionsJsonFields.SERVER_ADDRESS_INTERNAL, "")
        try:
            get_agent_options(server_address=server_address, timeout=4)
        except Exception as e:
            server_address = options.get(AgentOptionsJsonFields.SERVER_ADDRESS_EXTERNAL, None)
    update_env_param(constants._SERVER_ADDRESS, server_address, constants.SERVER_ADDRESS())

    update_env_param(
        constants._OFFLINE_MODE,
        options.get(AgentOptionsJsonFields.OFFLINE_MODE, None),
        optional_defaults[constants._OFFLINE_MODE],
    )
    update_env_param(
        constants._PULL_POLICY,
        options.get(AgentOptionsJsonFields.PULL_POLICY, None),
        optional_defaults[constants._PULL_POLICY],
    )
    update_env_param(
        constants._CPU_LIMIT,
        options.get(AgentOptionsJsonFields.CPU_LIMIT, None),
        optional_defaults[constants._CPU_LIMIT],
    )
    update_env_param(
        constants._MEM_LIMIT,
        options.get(AgentOptionsJsonFields.MEM_LIMIT, None),
        optional_defaults[constants._MEM_LIMIT],
    )
    update_env_param(
        constants._SECURITY_OPT,
        options.get(AgentOptionsJsonFields.SECURITY_OPT, None),
        optional_defaults[constants._SECURITY_OPT],
    )
    update_env_param(
        constants._HTTP_PROXY,
        http_proxy,
        optional_defaults[constants._HTTP_PROXY],
    )
    update_env_param(constants._HTTPS_PROXY, http_proxy, optional_defaults[constants._HTTPS_PROXY])
    update_env_param(constants._NO_PROXY, no_proxy, optional_defaults[constants._NO_PROXY])
    update_env_param(
        constants._NET_CLIENT_DOCKER_IMAGE,
        net_options.get(AgentOptionsJsonFields.NET_CLIENT_DOCKER_IMAGE, None),
        optional_defaults[constants._NET_CLIENT_DOCKER_IMAGE],
    )
    update_env_param(
        constants._NET_SERVER_ADDRESS,
        net_options.get(AgentOptionsJsonFields.NET_SERVER_ADDRESS, None),
        optional_defaults[constants._NET_SERVER_ADDRESS],
    )
    update_env_param(
        constants._NET_SERVER_PORT,
        net_options.get(AgentOptionsJsonFields.NET_SERVER_PORT, None),
        optional_defaults[constants._NET_SERVER_PORT],
    )
    update_env_param(
        constants._DOCKER_IMAGE, options.get(AgentOptionsJsonFields.DOCKER_IMAGE, None)
    )
    update_env_param(
        constants._FORCE_CPU_ONLY,
        str(options.get(AgentOptionsJsonFields.FORCE_CPU_ONLY, False)).lower(),
        optional_defaults[constants._FORCE_CPU_ONLY],
    )
    update_env_param(
        constants._LOG_LEVEL,
        options.get(AgentOptionsJsonFields.LOG_LEVEL, None),
        optional_defaults[constants._LOG_LEVEL],
    )

    agent_host_dir = options.get(AgentOptionsJsonFields.AGENT_HOST_DIR, "").strip()
    if agent_host_dir == "":
        agent_host_dir = optional_defaults[constants._AGENT_HOST_DIR]

    update_env_param(constants._AGENT_HOST_DIR, agent_host_dir)

    update_env_param(
        constants._REMOVE_IDLE_DOCKER_IMAGE_AFTER_X_DAYS,
        options.get(AgentOptionsJsonFields.IMAGES_CLEAN_INTERVAL, None),
        optional_defaults[constants._REMOVE_IDLE_DOCKER_IMAGE_AFTER_X_DAYS],
    )

    volumes = {}

    def add_volume(src: str, dst: str) -> dict:
        volumes[src] = {"bind": dst, "mode": "rw"}

    add_volume(agent_host_dir, constants.AGENT_ROOT_DIR())
    add_volume("/var/run/docker.sock", "/var/run/docker.sock")
    add_volume(
        env[constants._SUPERVISELY_AGENT_FILES], constants.SUPERVISELY_AGENT_FILES_CONTAINER()
    )

    return env, volumes, ca_cert


def compare_semver(first, second):
    if first == second:
        return 0
    first = first.split(".")
    second = second.split(".")
    if len(first) != len(second):
        first = [*first, *["0"] * max(0, len(second) - len(first))]
        second = [*second, *["0"] * max(0, len(first) - len(second))]
    for i in range(3):
        if int(first[i]) > int(second[i]):
            return 1
        elif int(first[i]) < int(second[i]):
            return -1
    return 0


def check_instance_version():
    MIN_INSTANCE_VERSION = "6.8.68"
    instance_version = get_instance_version()
    if instance_version is None or compare_semver(instance_version, MIN_INSTANCE_VERSION) < 0:
        raise AgentOptionsNotAvailable(
            f"Instance version {instance_version} is too old. Required {MIN_INSTANCE_VERSION}"
        )


def _envs_changes(envs: dict) -> dict:
    changes = {}
    for key, value in envs.items():
        cur_value = os.environ.get(key, None)
        if cur_value is None or cur_value != value_to_str(value):
            changes[key] = value
    return changes


def _volumes_changes(volumes) -> dict:
    container_info = get_container_info()
    container_volumes = container_info.get("HostConfig", {}).get("Binds", [])
    container_volumes = binds_to_volumes_dict(container_volumes)
    changes = {}
    for key, value in volumes.items():
        if key not in container_volumes:
            changes[key] = value
        elif container_volumes[key]["bind"] != value["bind"]:
            changes[key] = value
    return changes


def _is_bind_attached(container_info, bind_path):
    vols = binds_to_volumes_dict(container_info.get("HostConfig", {}).get("Binds", []))

    for vol in vols.values():
        if vol["bind"] == bind_path:
            return True

    return False


def _copy_file_to_container(container, src, dst_dir: str):
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w|") as tar, open(src, "rb") as f:
        info = tar.gettarinfo(fileobj=f)
        info.name = os.path.basename(src)
        tar.addfile(info, f)

    container.put_archive(dst_dir, stream.getvalue())


def _write_new_ca_bundle(ca_cert: str):
    bundle_path = constants.SLY_EXTRA_CA_CERTS_BUNDLE_FILEPATH()
    certs = get_certificates_list()
    certs.insert(0, ca_cert)

    with open(bundle_path, "w", encoding="ascii") as f:
        f.write("\n".join(certs))


def _ca_cert_changed(ca_cert) -> str:
    cert_path = constants.SLY_EXTRA_CA_CERTS_FILEPATH()
    bundle_path = constants.SLY_EXTRA_CA_CERTS_BUNDLE_FILEPATH()

    if ca_cert is None:
        if os.path.exists(cert_path):
            os.remove(cert_path)

        if os.path.exists(bundle_path):
            os.remove(bundle_path)

        return None

    ca_cert = ca_cert.replace("\r\n", "\n")

    cur_path = constants.SLY_EXTRA_CA_CERTS()
    if cert_path == cur_path and os.path.exists(cert_path):
        with open(cert_path, "r", encoding="utf-8") as f:
            ca_file_contents = f.read().replace("\r\n", "\n")
            sly.logger.debug(f"Checking if existing certificates on disk need to be updated")
            if ca_file_contents == ca_cert:
                if not os.path.exists(bundle_path):
                    _write_new_ca_bundle(ca_cert)

                sly.logger.debug(f"Certificates are equal, skipping the update")
                return None
            else:
                sly.logger.debug(f"Certificates are not equal, updating")

    docker_api = docker.from_env()
    container_info = get_container_info()

    Path(cert_path).parent.mkdir(parents=True, exist_ok=True)
    with open(cert_path, "w", encoding="ascii") as f:
        f.write(ca_cert)

    _write_new_ca_bundle(ca_cert)

    # initialize certs volume and copy the new certificate
    if not _is_bind_attached(get_container_info(), constants.SLY_EXTRA_CA_CERTS_DIR()):
        agent_image = container_info["Config"]["Image"]

        tmp_container = docker_api.containers.create(
            agent_image,
            "",
            volumes={
                constants.SLY_EXTRA_CA_CERTS_VOLUME_NAME(): {
                    "bind": constants.SLY_EXTRA_CA_CERTS_DIR(),
                    "mode": "rw",
                }
            },
        )

        _copy_file_to_container(tmp_container, cert_path, constants.SLY_EXTRA_CA_CERTS_DIR())
        _copy_file_to_container(tmp_container, bundle_path, constants.SLY_EXTRA_CA_CERTS_DIR())
        tmp_container.remove(force=True)

    return cert_path


def is_agent_container_ready_to_continue():
    container_info = get_container_info()
    volumes = binds_to_volumes_dict(container_info.get("HostConfig", {}).get("Binds", []))

    # should contain at least 3 volumes:
    # docker socket
    # agent data files
    # apps data files
    if len(volumes) < 3:
        return False

    return True


def get_options_changes(envs: dict, volumes: dict, ca_cert: str) -> Tuple[dict, dict, str]:
    return _envs_changes(envs), _volumes_changes(volumes), _ca_cert_changed(ca_cert)


def check_and_remove_agent_with_old_name(dc: DockerClient):
    agent_same_token = []
    agent_name_start = constants.CONTAINER_NAME()
    cur_agent_cont = dc.containers.get(_get_self_container_idx())

    agent_old_name = f"supervisely-agent-{constants.TOKEN()}"
    cur_agent_contains_old_name = cur_agent_cont.name.startswith(agent_old_name)

    for cont in dc.containers.list(sparse=False, ignore_removed=True):
        if cont.name.startswith(agent_name_start):
            agent_same_token.append(cont)

    if len(agent_same_token) > 1:
        for cont in agent_same_token:
            # we don't want to remove current container
            if cur_agent_cont.name == cont.name:
                continue
            # we want to remove any other container with the same token that got stuck during the upgrade - the ones that have - after their name
            elif cont.name.startswith(f"{agent_name_start}-"):
                cont.remove(force=True)
            # we want to remove containers with a new name in case the current container contains an old one, this happens when the agent is deployed on an older Supervisely instance
            elif cur_agent_contains_old_name and cont.name.startswith(agent_name_start):
                cont.remove(force=True)


def is_named_docker_volume(volume_name: str):
    return "/" not in volume_name


def restart_agent(
    image: str = None,
    envs: dict = None,
    volumes: dict = None,
    runtime: str = None,
    ca_cert_path: str = None,
    docker_api=None,
):
    """Restart agent with new image, envs, volumes, runtime.
    If any of the arguments is None, it will be taken from the current agent.
    image: str - new image name
    envs: dict - new environment variables
    volumes: dict - new volumes
    runtime: str - new runtime
    ca_cert_path: str - new path to ca certificate
    """
    if docker_api is None:
        docker_api = docker.from_env()
    container_info = get_container_info()
    if image is None:
        image = container_info["Config"]["Image"]
    if envs is None:
        envs = container_info.get("Config", {}).get("Env", [])
        envs = envs_list_to_dict(envs)
    if volumes is None:
        volumes = binds_to_volumes_dict(container_info.get("HostConfig", {}).get("Binds", []))
    if runtime is None:
        runtime = container_info["HostConfig"]["Runtime"]

    # remove agent with old name if exists
    check_and_remove_agent_with_old_name(docker_api)

    # add SLY_EXTRA_CA_CERTS env
    if ca_cert_path is None:
        envs[constants._SLY_EXTRA_CA_CERTS] = constants.SLY_EXTRA_CA_CERTS()
    else:
        envs[constants._SLY_EXTRA_CA_CERTS] = ca_cert_path

    if envs[constants._SLY_EXTRA_CA_CERTS] is not None:
        # add SLY_EXTRA_CA_CERTS volume
        volumes[constants.SLY_EXTRA_CA_CERTS_VOLUME_NAME()] = {
            "bind": constants.SLY_EXTRA_CA_CERTS_DIR(),
            "mode": "rw",
        }

    # add REMOVE_OLD_AGENT env if needed (in case of update)
    remove_old_agent = constants.REMOVE_OLD_AGENT()
    remove_old_agent = envs.get(constants._REMOVE_OLD_AGENT, remove_old_agent)
    if remove_old_agent is not None:
        envs[constants._REMOVE_OLD_AGENT] = remove_old_agent

    # add cross agent volume
    volumes[constants.CROSS_AGENT_VOLUME_NAME()] = {
        "bind": constants.CROSS_AGENT_DATA_DIR(),
        "mode": "rw",
    }

    # create named volumes if necessary
    for src in volumes.keys():
        if is_named_docker_volume(src):
            try:
                docker_api.volumes.get(src)
            except docker.errors.NotFound:
                docker_api.volumes.create(src)

    envs = envs_dict_to_list(envs)
    container: Container = docker_api.containers.run(
        image,
        runtime=runtime,
        detach=True,
        name=f"{constants.CONTAINER_NAME()}-{sly.rand_str(5)}",
        remove=False,
        restart_policy={"Name": "unless-stopped"},
        volumes=volumes,
        environment=envs,
        stdin_open=False,
        tty=False,
    )
    container.reload()
    sly.logger.debug(f"After spawning. Container status: {str(container.status)}")
    sly.logger.info(
        "Docker container is spawned",
        extra={"container_id": container.id, "container_name": container.name},
    )


def nvidia_runtime_is_available():
    docker_api = docker.from_env()
    container_info = get_container_info()
    image = container_info.get("Image")
    try:
        docker_api.containers.run(
            image,
            entrypoint="",
            command="nvidia-smi",
            runtime="nvidia",
            remove=True,
        )
        return True
    except Exception as e:
        sly.logger.warning(f"Failed to run nvidia-smi:", exc_info=True)
        return False


def maybe_update_runtime():
    container_info = get_container_info()
    runtime = container_info["HostConfig"]["Runtime"]
    if runtime == "nvidia":
        return runtime
    sly.logger.debug("NVIDIA runtime is not enabled. Checking if it can be enabled...")
    is_available = nvidia_runtime_is_available()
    if is_available:
        sly.logger.debug("NVIDIA runtime is available. Will restart Agent with NVIDIA runtime.")
        return "nvidia"
    else:
        sly.logger.debug("NVIDIA runtime is not available.")
        return runtime


def convert_millicores_to_cpu_quota(millicores, cpu_period=100000):
    cpu_quota = (millicores / 1000) * cpu_period
    return int(cpu_quota)
