# coding: utf-8

import os
import os.path as osp
import shutil
import queue
import json
import re
import requests

import supervisely_lib as sly

from logging import Logger
from typing import Callable, List, Optional, Tuple, Union, Container
from datetime import datetime, timedelta
from pathlib import Path
from worker import constants


class AgentOptionsJsonFields:
    AGENT_HOST_DIR = "agentDataHostDir"
    DELETE_TASK_DIR_ON_FAILURE = "deleteTaskDirOnFailure"
    DELETE_TASK_DIR_ON_FINISH = "deleteTaskDirOnFinish"
    DOCKER_CREDS = "dockerCreds"
    DOCKER_LOGIN = "login"
    DOCKER_PASSWORD = "password"
    DOCKER_REGISTRY = "registry"
    SERVER_ADDRESS = "serverAddress"
    SERVER_ADDRESS_INTERNAL = "serverAddressInternal"
    SERVER_ADDRESS_EXTERNAL = "externalServerAddress"
    OFFLINE_MODE = "offlineMode"
    PULL_POLICY = "pullPolicy"
    SUPERVISELY_AGENT_FILES = "slyAppsDataHostDir"
    NO_PROXY = "noProxy"
    MEM_LIMIT = "memLimit"
    HTTP_PROXY = "httpProxy"
    SECURITY_OPT = "securityOpts"


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

        self.logger.info(f"Removed agent logs: {removed}")

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

        self.logger.info(f"Removed sessions: {cleaned_sessions}")

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

    def clean_pip_cache(self, auto=False):
        root_path = Path(constants.APPS_PIP_CACHE_DIR())
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

        self.logger.info(f"Removed PIP cache: {removed}")

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
        self._apps_cleaner(working_apps, auto=False, clean_pip=False)
        self.clean_git_tags()

    def _apps_cleaner(
        self,
        working_apps: Optional[Container[int]],
        auto: bool = False,
        clean_pip: bool = True,
    ):
        cleaned_sessions = self.clean_app_sessions(auto=auto, working_apps=working_apps)
        if auto is False:
            self.clean_app_files(cleaned_sessions)
        if clean_pip is True:
            self.clean_pip_cache(auto=auto)

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


def envs_list_to_dict(envs_list: List[str]) -> dict:
    envs_dict = {}
    for env in envs_list:
        env_name, env_value = env.split("=", maxsplit=1)
        envs_dict[env_name] = env_value
    return envs_dict


def envs_dict_to_list(envs_dict: dict) -> List[str]:
    envs_list = []
    for env_name, env_value in envs_dict.items():
        envs_list.append(f"{env_name}={env_value}")
    return envs_list


def binds_to_volumes_dict(binds: List[str]) -> dict:
    volumes = {}
    for bind in binds:
        src, dst = bind.split(":")
        volumes[src] = {"bind": dst, "mode": "rw"}
    return volumes


def volumes_dict_to_binds(volumes: dict) -> List[str]:
    binds = []
    for src, dst in volumes.items():
        binds.append(f"{src}:{dst['bind']}")
    return binds


def get_agent_options(server_address=None, token=None, timeout=None) -> dict:
    if server_address is None:
        server_address = constants.SERVER_ADDRESS()
    if token is None:
        token = constants.TOKEN()
    api_method = "agents.options.info"
    url = os.path.join(server_address, "public", "api", "v3", api_method)
    resp = requests.post(url=url, json={"token": token}, timeout=timeout)
    if resp.status_code != requests.codes.ok:
        try:
            text = resp.text
        except:
            text = None
        msg = f"Can't get agent options from server {server_address}."
        if text is not None:
            msg += f" Response: {text}"
        raise RuntimeError(msg)
    return resp.json()


def remove_empty_folders(path):
    if path is None:
        return
    if not os.path.isdir(path):
        return

    # remove empty subfolders
    files = os.listdir(path)
    if len(files):
        for f in files:
            fullpath = os.path.join(path, f)
            if os.path.isdir(fullpath):
                remove_empty_folders(fullpath)

    # if folder empty, delete it
    files = os.listdir(path)
    if len(files) == 0 and os.path.normpath(path) != os.path.normpath(
        constants.SUPERVISELY_SYNCED_APP_DATA_CONTAINER()
    ):
        sly.logger.info(f"Removing empty folder: {path}")
        os.rmdir(path)


def updated_agent_options() -> Tuple[dict, int]:
    env = {}

    def maybe_update_env_param(name, value):
        if not (value is None or value == ""):
            env[name] = value

    params = get_agent_options()
    options: dict = params["options"]
    ca_cert = params["caCert"]

    maybe_update_env_param(
        constants._AGENT_HOST_DIR, options.get(AgentOptionsJsonFields.AGENT_HOST_DIR, None)
    )
    maybe_update_env_param(
        constants._DELETE_TASK_DIR_ON_FAILURE,
        options.get(AgentOptionsJsonFields.DELETE_TASK_DIR_ON_FAILURE, None),
    )
    maybe_update_env_param(
        constants._DELETE_TASK_DIR_ON_FINISH,
        options.get(AgentOptionsJsonFields.DELETE_TASK_DIR_ON_FINISH, None),
    )

    docker_cr = options.get(AgentOptionsJsonFields.DOCKER_CREDS, [])
    docker_login = ",".join([cr[AgentOptionsJsonFields.DOCKER_LOGIN] for cr in docker_cr])
    docker_pass = ",".join([cr[AgentOptionsJsonFields.DOCKER_PASSWORD] for cr in docker_cr])
    docker_reg = ",".join([cr[AgentOptionsJsonFields.DOCKER_REGISTRY] for cr in docker_cr])
    maybe_update_env_param(
        constants._DOCKER_LOGIN,
        docker_login,
    )
    maybe_update_env_param(constants._DOCKER_PASSWORD, docker_pass)
    maybe_update_env_param(constants._DOCKER_REGISTRY, docker_reg)

    # TODO: save all server addresses
    server_address = options.get(AgentOptionsJsonFields.SERVER_ADDRESS, None)
    if server_address is None or server_address == "":
        server_address = options.get(AgentOptionsJsonFields.SERVER_ADDRESS_INTERNAL, None)
        try:
            get_agent_options(server_address=server_address, timeout=4)
        except:
            server_address = options.get(AgentOptionsJsonFields.SERVER_ADDRESS_EXTERNAL, None)
    maybe_update_env_param(constants._SERVER_ADDRESS, server_address)

    maybe_update_env_param(
        constants._OFFLINE_MODE, options.get(AgentOptionsJsonFields.OFFLINE_MODE, None)
    )
    maybe_update_env_param(
        constants._PULL_POLICY, options.get(AgentOptionsJsonFields.PULL_POLICY, None)
    )
    maybe_update_env_param(constants._NO_PROXY, options.get(AgentOptionsJsonFields.NO_PROXY, None))
    maybe_update_env_param(
        constants._MEM_LIMIT, options.get(AgentOptionsJsonFields.MEM_LIMIT, None)
    )
    maybe_update_env_param(
        constants._HTTP_PROXY, options.get(AgentOptionsJsonFields.HTTP_PROXY, None)
    )
    maybe_update_env_param(
        constants._SECURITY_OPT, options.get(AgentOptionsJsonFields.SECURITY_OPT, None)
    )
    # DOCKER_IMAGE
    # maybe_update_env_param(constants._DOCKER_IMAGE, options.get(AgentOptionsJsonFields.DOCKER_IMAGE, None))

    volumes = {}

    def add_volume(src: str, dst: str) -> dict:
        volumes[src] = {"bind": dst, "mode": "rw"}

    add_volume("/var/run/docker.sock", "/var/run/docker.sock")
    agent_host_dir = options.get(AgentOptionsJsonFields.AGENT_HOST_DIR, None)
    if agent_host_dir is not None and agent_host_dir != "":
        add_volume(agent_host_dir, constants.AGENT_ROOT_DIR())
    agent_files = options.get(AgentOptionsJsonFields.SUPERVISELY_AGENT_FILES, None)
    if agent_files is not None and agent_files != "":
        add_volume(agent_files, constants.SUPERVISELY_AGENT_FILES_CONTAINER())

    return env, volumes, ca_cert
