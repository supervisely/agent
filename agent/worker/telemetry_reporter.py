# coding: utf-8

import subprocess
from hurry.filesize import size as bytes_to_human
import json
import time
import supervisely_lib as sly

from worker.task_logged import TaskLogged
from worker import constants
from worker.system_info import get_directory_size_bytes, get_gpu_info


class TelemetryReporter(TaskLogged):
    NO_OUTPUT = b""

    @staticmethod
    def _get_subprocess_out_if_possible_no_shell(args, timeout=None):
        try:
            query_process_result = subprocess.run(args, stdout=subprocess.PIPE, timeout=timeout)
        except subprocess.TimeoutExpired:
            return TelemetryReporter.NO_OUTPUT
        return query_process_result.stdout

    @staticmethod
    def _get_subprocess_out_with_shell(args):
        res = subprocess.Popen(
            args, shell=True, executable="/bin/bash", stdout=subprocess.PIPE
        ).communicate()[0]
        return res

    def _get_subprocess_out_if_possible(self, proc_id, subprocess_args):
        no_output = b""
        if proc_id in self.skip_subproc:
            return no_output
        res = TelemetryReporter._get_subprocess_out_with_shell(subprocess_args)
        if len(res) <= 2:  # cr lf
            self.skip_subproc.add(proc_id)
            return no_output
        return res

    def __init__(self):
        super().__init__({"task_id": "telemetry"})
        self.skip_subproc = set()

    def init_logger(self):
        super().init_logger()
        sly.change_formatters_default_values(self.logger, "worker", "telemetry")

    def get_telemetry_str(self):
        htop_str = (
            "echo q | htop -C | "
            'aha --line-fix | html2text -width 999 | grep -v "F1Help" | grep -v "xml version="'
        )
        htop_output = self._get_subprocess_out_if_possible("htop", [htop_str])

        nvsmi_output = self.NO_OUTPUT
        try:
            nvsmi_output = self._get_subprocess_out_if_possible_no_shell(["nvidia-smi"], timeout=5)
        except OSError:
            pass

        docker_inspect_cmd = "curl -s --unix-socket /var/run/docker.sock http://localhost/containers/$(hostname)/json"
        docker_inspect_out = subprocess.Popen(
            [docker_inspect_cmd], shell=True, executable="/bin/bash", stdout=subprocess.PIPE
        ).communicate()[0]

        docker_image = (
            json.loads(docker_inspect_out)
            .get("Config", {})
            .get("Image", "Unavailable, may be in debug mode")
        )

        # img_sizeb, nn_sizeb - idk; root -- agent root
        # TODO: when or where apps use this?
        img_sizeb = get_directory_size_bytes(self.data_mgr.storage.images.storage_root_path)
        nn_sizeb = get_directory_size_bytes(self.data_mgr.storage.nns.storage_root_path)

        # some apps store weights in SUPERVISELY_SYNCED_APP_DATA_CONTAINER; root -- agent files
        # TODO: check why? 
        models_sizeb = get_directory_size_bytes(constants.SUPERVISELY_SYNCED_APP_DATA_CONTAINER())
        
        # tasks_sizeb - idk; root -- agent root
        tasks_sizeb = get_directory_size_bytes(constants.AGENT_TASKS_DIR())

        # app_sessions_sizeb - size of session file: repo (sometimes) and logs (always); root -- agent root
        app_sessions_sizeb = get_directory_size_bytes(constants.AGENT_APP_SESSIONS_DIR())

        # cache of app's git tags; root -- agent root
        git_tags_sizeb = get_directory_size_bytes(constants.APPS_STORAGE_DIR())

        # pip's cache; root -- agent root
        pip_cache_sizeb = get_directory_size_bytes(constants.APPS_PIP_CACHE_DIR())


        # TODO: full apps cache == tasks_sizeb + app_sessions_sizeb?
        full_tasks_cache = tasks_sizeb + app_sessions_sizeb
        
        total = full_tasks_cache + pip_cache_sizeb + img_sizeb + nn_sizeb

        node_storage = [
            {"Images": bytes_to_human(img_sizeb)},
            {"NN weights": bytes_to_human(nn_sizeb)},
            {"Tasks": bytes_to_human(full_tasks_cache)},
            {"Total": bytes_to_human(total)},
        ]

        server_info = {
            "htop": htop_output.decode("utf-8"),
            "nvsmi": nvsmi_output.decode("utf-8"),
            "node_storage": node_storage,
            "docker_image": docker_image,
            "gpu_info": get_gpu_info(self.logger),
        }

        info_str = json.dumps(server_info)
        return info_str

    def task_main_func(self):
        try:
            self.logger.info("TELEMETRY_REPORTER_INITIALIZED")
            # self.logger.debug(f"TELEMETRY REPORT: {self.get_telemetry_str()}")
            for _ in self.api.get_endless_stream(
                "GetTelemetryTask", sly.api_proto.Task, sly.api_proto.Empty()
            ):
                self.api.simple_request(
                    "UpdateTelemetry",
                    sly.api_proto.Empty,
                    sly.api_proto.AgentInfo(info=self.get_telemetry_str()),
                )

        except Exception as e:
            self.logger.critical(
                "TELEMETRY_REPORTER_CRASHED",
                exc_info=True,
                extra={
                    "event_type": sly.EventType.TASK_CRASHED,
                    "exc_str": str(e),
                },
            )


# class TelemetryAutoUpdater(TelemetryReporter):
#     def task_main_func(self):
#         try:
#             self.logger.info("TELEMETRY_AUTO_UPDATER_60SEC_INITIALIZED")
#             while True:
#                 self.api.simple_request(
#                     "UpdateTelemetry",
#                     sly.api_proto.Empty,
#                     sly.api_proto.AgentInfo(info=self.get_telemetry_str()),
#                 )
#                 time.sleep(60)
#         except Exception as e:
#             self.logger.warning(
#                 "TELEMETRY_AUTO_UPDATER_60SEC_CRASHED",
#                 exc_info=True,
#                 extra={
#                     "event_type": sly.EventType.TASK_CRASHED,
#                     "exc_str": str(e),
#                 },
#             )
