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

        docker_inspect_cmd = "curl -s --unix-socket /var/run/docker.sock http://localhost/containers/$(hostname)/json"
        docker_img_info = subprocess.Popen(
            [docker_inspect_cmd], shell=True, executable="/bin/bash", stdout=subprocess.PIPE
        ).communicate()[0]
        docker_img_info = json.loads(docker_img_info)

        use_options = False

        try:
            agent_utils.check_instance_version()
            envs, volumes, ca_cert = agent_utils.updated_agent_options()
            _, _, new_ca_cert_path = agent_utils.get_options_changes(envs, volumes, ca_cert)
            if new_ca_cert_path and constants.SLY_EXTRA_CA_CERTS() != new_ca_cert_path:
                envs[constants._SLY_EXTRA_CA_CERTS] = new_ca_cert_path

            use_options = True
        except agent_utils.AgentOptionsNotAvailable:
            envs = agent_utils.envs_list_to_dict(docker_img_info["Config"]["Env"])
            volumes = agent_utils.binds_to_volumes_dict(docker_img_info["HostConfig"]["Binds"])

        cur_container_id = docker_img_info["Config"]["Hostname"]

        if (
            docker_img_info["Config"]["Labels"].get("com.docker.compose.project", None)
            == "supervisely"
        ):
            raise RuntimeError(
                "Docker container was started from docker-compose. Please, use docker-compose to upgrade."
            )
            return

        envs[constants._REMOVE_OLD_AGENT] = cur_container_id

        image = docker_img_info["Config"]["Image"]
        if self.info.get("docker_image", None):
            image = self.info["docker_image"]
        if constants._DOCKER_IMAGE in envs:
            image = envs[constants._DOCKER_IMAGE]
        if str(envs.get(constants._PULL_POLICY)) != str(sly.docker_utils.PullPolicy.NEVER):
            sly.docker_utils._docker_pull_progress(self._docker_api, image, self.logger)

        # Pull net-client if needed
        net_container_name = constants.NET_CLIENT_CONTAINER_NAME()
        try:
            sly_net_container = self._docker_api.containers.get(net_container_name)

            if envs.get(constants._PULL_POLICY) != str(sly.docker_utils.PullPolicy.NEVER):
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

        # add cross agent volume
        try:
            self._docker_api.volumes.create(constants.CROSS_AGENT_VOLUME_NAME(), driver="local")
        except:
            pass
        volumes[constants.CROSS_AGENT_VOLUME_NAME()] = {
            "bind": constants.CROSS_AGENT_DATA_DIR(),
            "mode": "rw",
        }

        runtime = docker_img_info["HostConfig"]["Runtime"]
        envs = agent_utils.envs_dict_to_list(envs)

        # start new agent
        container = self._docker_api.containers.run(
            image,
            runtime=runtime,
            detach=True,
            name="{}-{}".format(constants.CONTAINER_NAME(), sly.rand_str(5)),
            remove=False,
            restart_policy={"Name": "unless-stopped"},
            volumes=volumes,
            environment=envs,
            stdin_open=False,
            tty=False,
        )
        container.reload()
        self.logger.debug("After spawning. Container status: {}".format(str(container.status)))
        self.logger.info(
            "Docker container is spawned",
            extra={"container_id": container.id, "container_name": container.name},
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
        sly.docker_utils._docker_pull_progress(dc, sly_net_client_image_name, logger)
        return True
