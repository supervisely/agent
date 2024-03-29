# coding: utf-8

import supervisely_lib as sly

from supervisely_lib._utils import _remove_sensitive_information # pylint: disable=import-error, no-name-in-module
from worker.task_logged import TaskLogged
from worker import constants
import logging


# a task that should be shown as a 'task' in web
class TaskSly(TaskLogged):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    def init_logger(self, loglevel=None):
        super().init_logger(loglevel=loglevel)
        sly.change_formatters_default_values(self.logger, 'service_type', sly.ServiceType.TASK)
        sly.change_formatters_default_values(self.logger, 'task_id', self.info['task_id'])
        self.logger.info(f"TASK LOG LEVEL: {logging.getLevelName(self.logger.level)} ({self.logger.level})")

    def init_api(self):
        super().init_api()
        self.api.add_to_metadata('x-task-id', str(self.info['task_id']))

    def report_start(self):
        self.logger.info('TASK_START', extra={'event_type': sly.EventType.TASK_STARTED})
        to_log = _remove_sensitive_information(self.info)
        if "agent_info" in to_log:
            if "environ" in to_log["agent_info"]:
                if "DOCKER_NET" in to_log["agent_info"]["environ"]:
                    value = to_log["agent_info"]["environ"]["DOCKER_NET"]
                    if value is not None:
                        value = value.replace(constants.TOKEN(), '***')
                        to_log["agent_info"]["environ"]["DOCKER_NET"] = value
        self.logger.info('TASK_MSG', extra=to_log)

    def task_main_func(self):
        raise NotImplementedError()
