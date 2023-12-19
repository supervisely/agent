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
                raise RuntimeError("NET_SERVER_PORT is not defined")
            command = [
                constants.TOKEN(),
                os.path.join(constants.SERVER_ADDRESS(), "net/"),
                f"{constants.SERVER_ADDRESS().rstrip('/').lstrip('https://').lstrip('http://')}:{net_server_port}",
            ]
            envs = ["SLY_NET_CLIENT_PING_INTERVAL=60", "TRUST_DOWNSTREAM_PROXY=true"]
            volumes = [
                "/var/run/docker.sock:/tmp/docker.sock:ro",
                f"{constants.HOST_DIR()}:{constants.AGENT_ROOT_DIR()}",
                f"{constants.SUPERVISELY_AGENT_FILES()}:{constants.SUPERVISELY_AGENT_FILES_CONTAINER()}",
            ]
            log_config = LogConfig(
                type="local", config={"max-size": "1m", "max-file": "1", "compress": "false"}
            )

            if len(docker_api.networks.list(names=[network])):
                try:
                    docker_api.networks.get(network).remove()
                except:
                    sly.logger.debug("Can not remove network", exc_info=True)
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


def _envs_changes(envs: dict) -> dict:
    changes = {}
    for key, value in envs.items():
        cur_value = os.environ.get(key, None)
        if cur_value is None or cur_value != str(value):
            changes[key] = value
    return changes


def _volumes_changes(volumes) -> list:
    container_info = get_container_info()
    container_volumes = container_info.get("HostConfig", {}).get("Binds", [])
    container_volumes = agent_utils.binds_to_volumes_dict(container_volumes)
    changes = {}
    for key, value in volumes.items():
        if key not in container_volumes:
            changes[key] = value
        elif container_volumes[key]["bind"] != value["bind"]:
            changes[key] = value
    return changes


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


def _ca_cert_changed(ca_cert):
    if ca_cert is None:
        return False
    cert_path = os.path.join(constants.HOST_DIR(), "certs", "instance_ca_chain.crt")
    with open(cert_path, "r") as f:
        cur_cert = f.read()
        if cur_cert == ca_cert:
            return False
    with open(cert_path, "w") as f:
        f.write(ca_cert)
    os.environ["SLY_EXTRA_CA_CERTS"] = cert_path
    return True


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
        new_envs, new_volumes, ca_cert = agent_utils.updated_agent_options()
    except:
        sly.logger.debug("Can not update agent options", exc_info=True)
        sly.logger.warn("Can not update agent options. Agent will be started with current options")
        return
    restart_with_nvidia_runtime = _nvidia_runtime_check()
    ca_cert_changed = _ca_cert_changed(ca_cert)
    envs_changes = _envs_changes(new_envs)
    volumes_changes = _volumes_changes(new_volumes)
    if envs_changes or volumes_changes or restart_with_nvidia_runtime or ca_cert_changed:
        container_info = get_container_info()
        envs = container_info.get("Config", {}).get("Env", [])
        envs = agent_utils.envs_list_to_dict(envs)
        envs.update(envs_changes)
        envs = agent_utils.envs_dict_to_list(envs)
        volumes = agent_utils.volumes_dict_to_binds(new_volumes)
        runtime = (
            "nvidia" if restart_with_nvidia_runtime else container_info["HostConfig"]["Runtime"]
        )

        sly.logger.info(
            "Agent is restarting due to options change",
            extra={
                "envs_changes": {
                    k: "hidden" if k in constants.SENSITIVE_SETTINGS else v
                    for k, v in envs_changes.items()
                },
                "volumes_changes": volumes_changes,
                "runtime_changes": {container_info["HostConfig"]["Runtime"]: runtime},
                "ca_cert_changed": ca_cert_changed,
            },
        )
        Agent._restart(envs, volumes, runtime)
    for key, val in envs_changes.items():
        os.environ[key] = str(val)


if __name__ == "__main__":
    # set updated envs and restart if needed
    init_envs()
    constants.init_constants()  # Set up the directories.
    sly.add_default_logging_into_file(sly.logger, constants.AGENT_LOG_DIR())
    sly.main_wrapper("agent", main, parse_envs())
