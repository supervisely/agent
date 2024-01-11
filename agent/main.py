# coding: utf-8

import os
import sys
import docker
from docker.models.containers import Container
from docker.types import LogConfig
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

from worker import agent_utils
from worker import constants
from worker.system_info import get_container_info
from worker.agent import Agent


def parse_envs():
    args_req = {x: os.environ[x] for x in constants.get_required_settings()}
    args_opt = {
        x: constants.read_optional_setting(x) for x in constants.get_optional_defaults().keys()
    }
    args = {**args_opt, **args_req}
    return args


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


def _start_net_client(docker_api=None):
    if docker_api is None:
        docker_api = docker.from_env()
    net_container_name = "supervisely-net-client-{}".format(constants.TOKEN())
    sly_net_container = None

    for container in docker_api.containers.list():
        if container.name == net_container_name:
            sly_net_container: Container = container
            break
    if sly_net_container is None:
        try:
            sly.logger.info("Starting sly-net-client...")
            network = "supervisely-net-{}".format(constants.TOKEN())
            image = constants.NET_CLIENT_DOCKER_IMAGE()
            net_server_port = constants.NET_SERVER_PORT()
            if net_server_port is None:
                raise RuntimeError(f"{constants._NET_SERVER_PORT} is not defined")
            command = [
                constants.TOKEN(),
                os.path.join(constants.SERVER_ADDRESS(), "net/"),
                f"{constants.SERVER_ADDRESS().rstrip('/').lstrip('https://').lstrip('http://')}:{net_server_port}",
            ]
            envs = [
                f"{constants._SLY_NET_CLIENT_PING_INTERVAL}={constants.SLY_NET_CLIENT_PING_INTERVAL()}",
                f"{constants._TRUST_DOWNSTREAM_PROXY}={constants.TRUST_DOWNSTREAM_PROXY()}",
            ]
            if constants.HTTP_PROXY():
                envs.append(f"{constants._HTTP_PROXY}={constants.HTTP_PROXY()}")
            if constants.HTTPS_PROXY():
                envs.append(f"{constants._HTTPS_PROXY}={constants.HTTPS_PROXY()}")
            if constants.NO_PROXY():
                envs.append(f"{constants._NO_PROXY}={constants.NO_PROXY()}")
            if constants.SLY_EXTRA_CA_CERTS():
                envs.append(f"{constants._SLY_EXTRA_CA_CERTS}={constants.SLY_EXTRA_CA_CERTS()}")
            volumes = [
                "/var/run/docker.sock:/tmp/docker.sock:ro",
                f"{constants.HOST_DIR()}:{constants.AGENT_ROOT_DIR()}",
                f"{constants.SUPERVISELY_AGENT_FILES()}:{constants.SUPERVISELY_AGENT_FILES_CONTAINER()}",
            ]
            log_config = LogConfig(
                type="local", config={"max-size": "1m", "max-file": "1", "compress": "false"}
            )

            if len(docker_api.networks.list(names=[network])) == 0:
                docker_api.networks.create(network)
            docker_api.containers.run(
                image=image,
                name=net_container_name,
                command=command,
                network=network,
                cap_add="NET_ADMIN",
                volumes=volumes,
                privileged=True,
                restart_policy={"Name": "always", "MaximumRetryCount": 0},
                environment=envs,
                log_config=log_config,
                devices=["/dev/net/tun:/dev/net/tun"],
                detach=True,
            )
            sly.logger.info("Sly-net-client is started")
        except:
            sly.logger.debug("Sly-net-client is not started", exc_info=True)
            sly.logger.warn("Something goes wrong: can not start sly-net-client")
            sly.logger.warn(
                (
                    "Probably you should restart agent manually using instructions:"
                    "https://developer.supervisely.com/getting-started/connect-your-computer"
                )
            )


def _nvidia_runtime_check():
    container_info = get_container_info()
    runtime = container_info["HostConfig"]["Runtime"]
    if runtime == "nvidia":
        return False
    sly.logger.info("NVIDIA runtime is not enabled. Checking if it can be enabled...")
    docker_api = docker.from_env()
    image = constants.DEFAULT_APP_DOCKER_IMAGE()
    try:
        docker_api.containers.run(
            image,
            command="nvidia-smi",
            runtime="nvidia",
            remove=True,
        )
        sly.logger.info("NVIDIA runtime is available. Will restart Agent with NVIDIA runtime.")
        return True
    except Exception as e:
        sly.logger.info("NVIDIA runtime is not available.")
        return False


def main(args):
    sly.logger.info(
        "ENVS",
        extra={
            key: "hidden" if key in constants.SENSITIVE_SETTINGS else value
            for key, value in args.items()
        },
    )

    sly.logger.info(f"Agent storage [host]: {constants.SUPERVISELY_AGENT_FILES()}")
    sly.logger.info(f"Agent storage [container]: {constants.SUPERVISELY_AGENT_FILES_CONTAINER()}")
    sly.logger.info(f"Agent storage app data [host]: {constants.SUPERVISELY_SYNCED_APP_DATA()}")
    sly.logger.info(
        f"Agent storage app data [container]: {constants.SUPERVISELY_SYNCED_APP_DATA_CONTAINER()}"
    )

    sly.logger.info("Remove empty directories in agent storage...")
    remove_empty_folders(constants.SUPERVISELY_AGENT_FILES_CONTAINER())

    _start_net_client()
    agent = Agent()
    agent.inf_loop()
    agent.wait_all()


def init_envs():
    try:
        agent_utils.check_instance_version()
        new_envs, new_volumes, ca_cert = agent_utils.updated_agent_options()
    except:
        sly.logger.debug("Can not update agent options", exc_info=True)
        sly.logger.warn("Can not update agent options. Agent will be started with current options")
        return
    restart_with_nvidia_runtime = _nvidia_runtime_check()
    envs_changes, volumes_changes, new_ca_cert_path = agent_utils.get_options_changes(
        new_envs, new_volumes, ca_cert
    )
    if envs_changes or volumes_changes or restart_with_nvidia_runtime or new_ca_cert_path:
        docker_api = docker.from_env()
        container_info = get_container_info()
        if (
            new_ca_cert_path
            and os.environ.get(constants.SLY_EXTRA_CA_CERTS(), None) != new_ca_cert_path
        ):
            new_envs[constants.SLY_EXTRA_CA_CERTS()] = new_ca_cert_path
        runtime = (
            "nvidia" if restart_with_nvidia_runtime else container_info["HostConfig"]["Runtime"]
        )

        # add remove old agent env if needed (in case of update)
        remove_old_agent = constants.REMOVE_OLD_AGENT()
        if remove_old_agent is not None:
            new_envs[constants._REMOVE_OLD_AGENT] = remove_old_agent

        # add update net client env if needed (in case of update)
        new_envs[constants._UPDATE_SLY_NET_AFTER_RESTART] = constants.UPDATE_SLY_NET_AFTER_RESTART()

        envs = agent_utils.envs_dict_to_list(new_envs)

        # add cross agent volume
        try:
            docker_api.volumes.create(constants.CROSS_AGENT_VOLUME_NAME(), driver="local")
        except:
            pass
        new_volumes[constants.CROSS_AGENT_VOLUME_NAME()] = {
            "bind": constants.CROSS_AGENT_DATA_DIR(),
            "mode": "rw",
        }

        sly.logger.info(
            "Agent is restarting due to options change",
            extra={
                "envs_changes": {
                    k: "hidden" if k in constants.SENSITIVE_SETTINGS else v
                    for k, v in envs_changes.items()
                },
                "volumes_changes": volumes_changes,
                "runtime_changes": {container_info["HostConfig"]["Runtime"]: runtime},
                "ca_cert_changed": bool(new_ca_cert_path),
            },
        )
        Agent._restart(envs, new_volumes, runtime)
        sly.logger.info("Agent is restarted. This container will be removed")
        docker_api.containers.get(container_info["Id"]).remove(force=True)


if __name__ == "__main__":
    # set updated envs and restart if needed
    init_envs()
    constants.init_constants()  # Set up the directories.
    sly.add_default_logging_into_file(sly.logger, constants.AGENT_LOG_DIR())
    sly.main_wrapper("agent", main, parse_envs())
