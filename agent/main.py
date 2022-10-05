# coding: utf-8

import os
import sys
from dotenv import load_dotenv


gettrace = getattr(sys, "gettrace", None)
if gettrace is None:
    print("No sys.gettrace")
elif gettrace():
    print("Hmm, Debugg is in progress")
    import faulthandler

    faulthandler.enable()
    # only for convenient debug, has no effect in production
    load_dotenv(os.path.expanduser("~/debug-agent.env"))
else:
    print("Debugger is disabled")


import supervisely_lib as sly

from worker import constants
from worker.agent import Agent


def parse_envs():
    args_req = {x: os.environ[x] for x in constants.get_required_settings()}
    args_opt = {
        x: constants.read_optional_setting(x)
        for x in constants.get_optional_defaults().keys()
    }
    args = {**args_opt, **args_req}
    return args

def remove_empty_folders(path):
    if path is None:
        return
    if not os.path.isdir(path):
        return
    # if os.path.normpath(path) == os.path.normpath(constants.SUPERVISELY_SYNCED_APP_DATA()):
    #     return

    # remove empty subfolders
    files = os.listdir(path)
    if len(files):
        for f in files:
            fullpath = os.path.join(path, f)
            if os.path.isdir(fullpath):
                remove_empty_folders(fullpath)

    # if folder empty, delete it
    files = os.listdir(path)
    if len(files) == 0 and os.path.normpath(path) != os.path.normpath(constants.SUPERVISELY_SYNCED_APP_DATA()):
        sly.logger.info(f"Removing empty folder: {path}")
        os.rmdir(path)

def main(args):
    sly.logger.info("ENVS", extra={**args, constants._DOCKER_PASSWORD: "hidden"})
    
    sly.logger.info("Remove empty directories in agent storage")
    remove_empty_folders(constants.SUPERVISELY_AGENT_FILES())
    
    agent = Agent()
    agent.inf_loop()
    agent.wait_all()


if __name__ == "__main__":
    constants.init_constants()  # Set up the directories.
    sly.add_default_logging_into_file(sly.logger, constants.AGENT_LOG_DIR())
    sly.main_wrapper("agent", main, parse_envs())
