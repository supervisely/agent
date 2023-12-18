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
from worker.agent import Agent, AgentRestarted


def parse_envs():
    args_req = {x: os.environ[x] for x in constants.get_required_settings()}
    args_opt = {
        x: constants.read_optional_setting(x) for x in constants.get_optional_defaults().keys()
    }
    args = {**args_opt, **args_req}
    return args


def main(args):
    sly.logger.info(
        "ENVS",
        extra={
            key: "hided" if key in constants.SENSITIVE_SETTINGS else val
            for key, val in args.items()
        },
    )

    try:
        agent = Agent()
        agent.inf_loop()
        agent.wait_all()
    except AgentRestarted:
        return


if __name__ == "__main__":
    sly.main_wrapper("agent", main, parse_envs())
