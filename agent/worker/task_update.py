# coding: utf-8

from logging import Logger
import os
import json
import supervisely_lib as sly

from .task_dockerized import TaskSly
import subprocess
import docker
from docker.models.containers import Container
from docker.models.images import ImageCollection
from docker.errors import DockerException, ImageNotFound
from worker import constants
from worker import agent_utils
from worker import docker_utils
from worker.system_info import get_container_info


class TaskUpdate(TaskSly):
    @property
    def docker_api(self):
        return self._docker_api

    @docker_api.setter
    def docker_api(self, val: docker.DockerClient):
        self._docker_api = val

    def task_main_func(self):
        if constants.TOKEN() != self.info["config"]["access_token"]:
            raise RuntimeError("Current token != new token")

        container_info = get_container_info()

        if (
            container_info["Config"]["Labels"].get("com.docker.compose.project", None)
            == "supervisely"
        ):
            raise RuntimeError(
                "Docker container was started from docker-compose. Please, use docker-compose to upgrade."
            )

        use_options = False
        ca_cert_path = None
        try:
            agent_utils.check_instance_version()
            envs, volumes, ca_cert = agent_utils.updated_agent_options()
            _, _, ca_cert_path = agent_utils.get_options_changes(envs, volumes, ca_cert)
            use_options = True
        except agent_utils.AgentOptionsNotAvailable:
            envs = agent_utils.envs_list_to_dict(container_info["Config"]["Env"])
            volumes = agent_utils.binds_to_volumes_dict(container_info["HostConfig"]["Binds"])

        optional_defaults = constants.get_optional_defaults()
        envs[constants._SHOULD_CLEAN_TASKS_DATA] = self.info["config"].get(
            "should_clean_tasks_data", optional_defaults[constants._SHOULD_CLEAN_TASKS_DATA]
        )
        envs[constants._SHOULD_CLEAN_PIP_CACHE] = self.info["config"].get(
            "should_clean_pip_cache", optional_defaults[constants._SHOULD_CLEAN_PIP_CACHE]
        )
        envs[constants._SHOULD_CLEAN_APPS_DATA] = self.info["config"].get(
            "should_clean_apps_data", optional_defaults[constants._SHOULD_CLEAN_APPS_DATA]
        )

        image = container_info["Config"]["Image"]
        if self.info.get("docker_image", None):
            image = self.info["docker_image"]
        if constants._DOCKER_IMAGE in envs:
            image = envs[constants._DOCKER_IMAGE]

        # Pull new image if needed
        if envs.get(constants._PULL_POLICY) != str(docker_utils.PullPolicy.NEVER):
            docker_utils._docker_pull_progress(self._docker_api, image, self.logger)

        # Pull net-client if needed
        net_container_name = constants.NET_CLIENT_CONTAINER_NAME()
        try:
            sly_net_container = self._docker_api.containers.get(net_container_name)

            if envs.get(constants._PULL_POLICY) != str(docker_utils.PullPolicy.NEVER):
                sly_net_client_image_name = None

                if use_options:
                    sly_net_client_image_name = envs.get(constants._NET_CLIENT_DOCKER_IMAGE)

                need_update = check_and_pull_sly_net_if_needed(
                    self._docker_api, sly_net_container, self.logger, sly_net_client_image_name
                )
                envs[constants._UPDATE_SLY_NET_AFTER_RESTART] = "true" if need_update else "false"
        except docker.errors.NotFound:
            self.logger.warn(
                "Couldn't find sly-net-client attached to this agent. We'll try to deploy it during the agent restart"
            )

        if envs.get(constants._FORCE_CPU_ONLY, "false") == "true":
            runtime = "runc"
        else:
            runtime = agent_utils.maybe_update_runtime()

        # Stop current container
        cur_container_id = container_info["Config"]["Hostname"]
        envs[constants._REMOVE_OLD_AGENT] = cur_container_id

        agent_utils.restart_agent(
            image=image,
            envs=envs,
            volumes=volumes,
            runtime=runtime,
            ca_cert_path=ca_cert_path,
            docker_api=self._docker_api,
        )


def run_shell_command(cmd, print_output=False):
    pipe = subprocess.Popen(cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    stdout = pipe.communicate()[0]
    if pipe.returncode != 0:
        raise RuntimeError(stdout.decode("utf-8"))
    res = []
    for line in stdout.decode("utf-8").splitlines():
        clean_line = line.strip()
        res.append(clean_line)
        if print_output:
            print(clean_line)
    return res


def check_and_pull_sly_net_if_needed(
    dc: docker.DockerClient,
    cur_container: Container,
    logger: Logger,
    sly_net_client_image_name=None,
) -> bool:
    ic = ImageCollection(dc)

    if sly_net_client_image_name is None:
        sly_net_client_image_name = cur_container.attrs["Config"]["Image"]

    docker_hub_image_info = ic.get_registry_data(sly_net_client_image_name)
    name_with_digest: str = cur_container.image.attrs.get("RepoDigests", [""])[0]

    if name_with_digest.endswith(docker_hub_image_info.id):
        logger.info("sly-net-client is already updated")
        return False
    else:
        logger.info("Found new version of sly-net-client. Pulling...")
        docker_utils._docker_pull_progress(dc, sly_net_client_image_name, logger)
        return True
