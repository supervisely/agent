# coding: utf-8

import os
import os.path as osp
from typing import List, Union

import supervisely as sly
from datetime import datetime, timedelta
from pathlib import Path

from worker.task_sly import TaskSly
from worker.agent_utils import TaskDirCleaner
from worker import constants


class TaskCleanNode(TaskSly):
    def remove_objects(self, storage, spaths):
        removed = []
        for st_path in spaths:
            ret_status = None
            if type(st_path) is str:
                ret_status = storage.remove_object(st_path)
            else:
                ret_status = storage.remove_object(*st_path)
            removed.append(ret_status)
        removed = list(filter(None, removed))
        return removed

    def remove_images(self, storage, proj_structure):
        for key, value in proj_structure.items():
            self.logger.info(
                "Clean dataset",
                extra={
                    "proj_id": value["proj_id"],
                    "proj_title": value["proj_title"],
                    "ds_id": value["ds_id"],
                    "ds_title": value["ds_title"],
                },
            )
            # spaths = [spath_ext[0] for spath_ext in value['spaths']]
            removed = self.remove_objects(storage, value["spaths"])
            self.logger.info(
                "Images are removed.",
                extra={"need_remove_cnt": len(value["spaths"]), "removed_cnt": len(removed)},
            )

    def remove_weights(self, storage, paths):
        removed = self.remove_objects(storage, paths)
        self.logger.info(
            "Weights are removed.",
            extra={"need_remove_cnt": len(paths), "removed_cnt": len(removed)},
        )

    def get_dataset_images_hashes(self, dataset_id):
        image_array = self.api.simple_request(
            "GetDatasetImages", sly.api_proto.ImageArray, sly.api_proto.Id(id=dataset_id)
        )
        img_hashes = []

        for batch_img_ids in sly.batched(
            list(image_array.images), constants.BATCH_SIZE_GET_IMAGES_INFO()
        ):
            images_info_proto = self.api.simple_request(
                "GetImagesInfo",
                sly.api_proto.ImagesInfo,
                sly.api_proto.ImageArray(images=batch_img_ids),
            )
            img_hashes.extend([(info.hash, info.ext) for info in images_info_proto.infos])
        return img_hashes

    def list_weights_to_remove(self, storage, action, input_weights_hashes):
        if action == "delete_selected":
            return [storage.get_storage_path(hash) for hash in input_weights_hashes]

        if action == "delete_all_except_selected":
            selected_paths = set([storage.get_storage_path(hash) for hash in input_weights_hashes])
            all_paths = set([path_and_suffix[0] for path_and_suffix in storage.list_objects()])
            paths_to_remove = list(
                all_paths.difference(selected_paths)
            )  # all_paths - selected_paths
            return paths_to_remove

        raise ValueError("Unknown cleanup action", extra={"action": action})

    def list_images_to_remove(self, storage, action, projects):
        img_spaths = {}
        for project in projects:
            for dataset in project["datasets"]:
                ds_id = dataset["id"]
                img_spaths[ds_id] = {
                    "proj_id": project["id"],
                    "proj_title": project["title"],
                    "ds_id": ds_id,
                    "ds_title": dataset["title"],
                    "spaths": [],
                }
                temp_spaths = [
                    storage.get_storage_path(hash_ext[0], hash_ext[1])
                    for hash_ext in self.get_dataset_images_hashes(ds_id)
                ]
                img_spaths[ds_id]["spaths"] = temp_spaths

        if action == "delete_selected":
            return img_spaths

        if action == "delete_all_except_selected":
            selected_paths = []
            for key, value in img_spaths.items():
                selected_paths.extend(value["spaths"])
            all_paths = set(storage.list_objects())
            paths_to_remove = all_paths.difference(
                set(selected_paths)
            )  # all_paths - selected_paths

            result = {}
            result[0] = {
                "proj_id": -1,
                "proj_title": "all cache images",
                "ds_id": -1,
                "ds_title": "all cache images",
            }
            result[0]["spaths"] = paths_to_remove
            return result

        raise ValueError("Unknown cleanup action", extra={"action": action})

    def clean_tasks_dir(self):
        self.logger.info("Will remove temporary tasks data.")
        task_dir = constants.AGENT_TASKS_DIR()
        task_names = os.listdir(task_dir)
        for subdir_n in task_names:
            dir_task = osp.join(task_dir, subdir_n)
            TaskDirCleaner(dir_task).clean_forced()
        self.logger.info("Temporary tasks data has been removed.")

    def clean_agent_logs(self):
        self.logger.info("Auto remove: clean old agent logs.")
        root_path = Path(constants.AGENT_LOG_DIR())

        old_logs = self._get_old_files_or_paths(root_path)
        for log_path in old_logs:
            sly.fs.silent_remove(log_path)

        self.logger.info(f"Removed agent logs: {log_path}")

    def clean_app_sessions(self, auto=True):
        root_path = Path(constants.AGENT_APP_SESSIONS_DIR())
        removed_ids = []
        now = datetime.now()
        if auto is True:
            old_apps = self._get_old_files_or_paths(root_path)
        else:
            # get all files and folders
            old_apps = self._get_old_files_or_paths(age_limit=timedelta(0))

        for app in old_apps:
            app_path = Path(app)
            app_id = app_path.name

            if not os.path.exists(app_path / "__do_not_clean.marker"):
                removed_ids.append(app_id)
                sly.fs.silent_remove(app)
                continue

            if self._check_task_id_finished_or_crashed(app_id):
                removed_ids.append(app_id)
                sly.fs.silent_remove(app)

    def task_main_func(self):
        self.logger.info("CLEAN_NODE_START")

        if self.info["action"] == "remove_tasks_data":
            self.clean_tasks_dir()
        else:
            if "projects" in self.info:
                img_storage = self.data_mgr.storage.images
                proj_structure = self.list_images_to_remove(
                    img_storage, self.info["action"], self.info["projects"]
                )
                self.remove_images(img_storage, proj_structure)

            if "weights" in self.info:
                nns_storage = self.data_mgr.storage.nns
                weights_to_rm = self.list_weights_to_remove(
                    nns_storage, self.info["action"], self.info["weights"]
                )
                self.remove_weights(nns_storage, weights_to_rm)

        self.logger.info("CLEAN_NODE_FINISH")

    def _get_log_datetime(self, log_name) -> datetime:
        return datetime.strptime(log_name, "log_%Y-%m-%d_%H:%M:%S.txt")

    def _get_file_or_path_datetime(self, path: Union[Path, str]) -> datetime:
        time_sec = max(os.path.getmtime(path), os.path.getatime(path))
        return datetime.fromtimestamp(time_sec)

    def _get_old_files_or_paths(
        self,
        parent_path: Union[Path, str],
        age_limit: timedelta = constants.AUTO_CLEAN_TIMEDELTA_DAYS,
    ) -> List[str]:
        now = datetime.now()
        ppath = Path(parent_path)
        old_path_files = []
        for file_or_path in os.listdir(parent_path):
            full_path = ppath / file_or_path
            file_datetime = self._get_file_or_path_datetime(full_path)
            if (now - file_datetime) > age_limit:
                old_path_files.append(str(full_path))

        return old_path_files

    def _check_task_id_finished_or_crashed(self, task_id: Union[str, int]) -> bool:
        try:
            task_id = int(task_id)
        except ValueError as exc:
            self.logger.exception(exc)

        return True
