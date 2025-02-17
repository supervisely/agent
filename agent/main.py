# coding: utf-8

import os
import sys
import docker
from urllib.parse import urljoin
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
from worker import docker_utils


def parse_envs():
    args_req = {
        x: constants._VALUES[x] if x in constants._VALUES else os.environ.get(x, None)
        for x in constants.get_required_settings()
    }
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
    net_container_name = constants.NET_CLIENT_CONTAINER_NAME()

    try:
        network = constants.NET_CLIENT_NETWORK()
        image = constants.NET_CLIENT_DOCKER_IMAGE()
        net_server_port = constants.NET_SERVER_PORT()
        if net_server_port is None:
            raise RuntimeError(f"{constants._NET_SERVER_PORT} is not defined")

        command = [
            constants.TOKEN(),
            urljoin(constants.SERVER_ADDRESS(), "net/"),
            constants.NET_SERVER_ADDRESS(),
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

        volumes = [
            "/var/run/docker.sock:/tmp/docker.sock:ro",
            f"{constants.HOST_DIR()}:{constants.AGENT_ROOT_DIR()}",
            f"{constants.SUPERVISELY_AGENT_FILES()}:{constants.SUPERVISELY_AGENT_FILES_CONTAINER()}",
        ]

        if constants.SLY_EXTRA_CA_CERTS() and os.path.exists(constants.SLY_EXTRA_CA_CERTS()):
            envs.append(
                f"{constants._SLY_EXTRA_CA_CERTS}={constants.SLY_EXTRA_CA_CERTS_FILEPATH()}"
            )
            volumes.append(
                f"{constants.SLY_EXTRA_CA_CERTS_VOLUME_NAME()}:{constants.SLY_EXTRA_CA_CERTS_DIR()}"
            )

        log_config = LogConfig(
            type="local", config={"max-size": "1m", "max-file": "1", "compress": "false"}
        )

        try:
            docker_api.networks.get(network)
        except:
            docker_api.networks.create(network)

        if constants.OFFLINE_MODE() is False:
            img_exists = docker_utils._docker_image_exists(docker_api, image)
            if not img_exists:
                docker_utils._docker_pull(docker_api, image, logger=sly.logger, raise_exception=True)

        sly.logger.info("Starting sly-net-client...")
        net_container = docker_api.containers.run(
            image=image,
            name=f"{net_container_name}_{sly.rand_str(5)}",
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
        for container in docker_api.containers.list(all=True, sparse=False, ignore_removed=True):
            if container.name.startswith(net_container_name) and container.id != net_container.id:
                container.remove(force=True)
        net_container.rename(net_container_name)
        sly.logger.info("Sly-net-client is started")
    except:
        for container in docker_api.containers.list(sparse=False, ignore_removed=True):
            if (
                container.name.startswith(net_container_name)
                and not container.name == net_container_name
            ):
                try:
                    docker_api.containers.get(net_container_name)
                    container.remove(force=True)
                except docker.errors.NotFound:
                    container.rename(net_container_name)

        try:
            net_container = docker_api.containers.get(net_container_name)
        except:
            net_container = None
        if net_container is None:
            sly.logger.fatal("Sly-net-client is not running", exc_info=True)
            sly.logger.warn("Something went wrong: couldn't start sly-net-client")
            sly.logger.warn(
                (
                    "You should probably update Supervisely to the latest version or restart the agent manually using the instructions here:"
                    "https://developer.supervisely.com/getting-started/connect-your-computer"
                )
            )
        else:
            try:
                net_client_networks_dict = net_container.attrs.get("NetworkSettings").get(
                    "Networks"
                )
                net_client_network_name = list(net_client_networks_dict.keys())[0]

                if net_client_network_name != constants.NET_CLIENT_NETWORK():
                    os.environ[constants._NET_CLIENT_NETWORK] = net_client_network_name
            except Exception as e:
                sly.logger.fatal(
                    "Couldn't fetch network name from the net-client to reuse the same network",
                    exc_info=True,
                )
                raise e


def _is_runtime_changed(new_runtime):
    container_info = get_container_info()
    return container_info["HostConfig"]["Runtime"] != new_runtime


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
    except Exception as e:
        if not agent_utils.is_agent_container_ready_to_continue():
            sly.logger.error(
                "Agent options are not available. Agent will be stopped. Please, check the connection to the server"
            )
            raise

        if isinstance(e, agent_utils.AgentOptionsNotAvailable):
            sly.logger.debug("Can not update agent options", exc_info=True)
            sly.logger.warning(
                "Can not update agent options. Agent will be started with current options"
            )
            return

        raise

    if new_envs.get(constants._FORCE_CPU_ONLY, "false") == "true":
        runtime = "runc"
        runtime_changed = _is_runtime_changed(runtime)
    else:
        runtime = agent_utils.maybe_update_runtime()
        runtime_changed = _is_runtime_changed(runtime)
    envs_changes, volumes_changes, new_ca_cert_path = agent_utils.get_options_changes(
        new_envs, new_volumes, ca_cert
    )
    if (
        len(envs_changes) > 0
        or len(volumes_changes) > 0
        or runtime_changed
        or new_ca_cert_path is not None
    ):
        docker_api = docker.from_env()
        container_info = get_container_info()

        # TODO: only set true if some NET_CLIENT envs changed
        new_envs[constants._UPDATE_SLY_NET_AFTER_RESTART] = "true"

        sly.logger.info(
            "Agent is restarting due to options change",
            extra={
                "envs_changes": {
                    k: "hidden" if k in constants.SENSITIVE_SETTINGS else v
                    for k, v in envs_changes.items()
                },
                "volumes_changes": volumes_changes,
                "runtime_changes": (
                    {container_info["HostConfig"]["Runtime"]: runtime} if runtime_changed else {}
                ),
                "ca_cert_changed": bool(new_ca_cert_path),
            },
        )

        # recursion warning
        restart_n = constants.AGENT_RESTART_COUNT()
        if restart_n >= constants.MAX_AGENT_RESTARTS():
            sly.logger.warning(
                (
                    "Agent restarted multiple times, indicating a potential error. "
                    "Reapply options and contact support if issues persist."
                )
            )
            return
        new_envs[constants._AGENT_RESTART_COUNT] = restart_n + 1

        agent_utils.restart_agent(
            envs=new_envs,
            volumes=new_volumes,
            runtime=runtime,
            ca_cert_path=new_ca_cert_path,
            docker_api=docker_api,
        )

        sly.logger.info("Agent is restarted. This container will be removed")
        docker_api.containers.get(container_info["Id"]).remove(force=True)


if __name__ == "__main__":
    # set updated envs and restart if needed
    init_envs()
    constants.init_constants()  # Set up the directories.
    sly.add_default_logging_into_file(sly.logger, constants.AGENT_LOG_DIR())
    sly.main_wrapper("agent", main, parse_envs())
