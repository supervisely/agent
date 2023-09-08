# coding: utf-8

import os
import os.path as osp
import shutil
import queue
import json

import supervisely_lib as sly

from logging import Logger
from typing import List, Optional, Union, Container
from datetime import datetime, timedelta
from pathlib import Path
from worker import constants


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
        """Delete files, used in finished/crashed apps"""
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
        datetime is greater than constants.AUTO_CLEAN_TIMEDELTA_DAYS (default: 14)

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
