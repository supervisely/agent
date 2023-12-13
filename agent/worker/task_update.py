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

        cur_container_id = docker_img_info["Config"]["Hostname"]
        cur_volumes = docker_img_info["HostConfig"]["Binds"]
        cur_envs = docker_img_info["Config"]["Env"]

        if (
            docker_img_info["Config"]["Labels"].get("com.docker.compose.project", None)
            == "supervisely"
        ):
            raise RuntimeError(
                "Docker container was started from docker-compose. Please, use docker-compose to upgrade."
            )
            return

        sly.docker_utils._docker_pull_progress(
            self._docker_api, self.info["docker_image"], self.logger
        )

        new_volumes = {}
        for vol in cur_volumes:
            parts = vol.split(":")
            src = parts[0]
            dst = parts[1]
            new_volumes[src] = {"bind": dst, "mode": "rw"}

        new_envs = []
        for val in cur_envs:
            if not val.startswith("REMOVE_OLD_AGENT"):
                new_envs.append(val)
        cur_envs = new_envs
        cur_envs.append("REMOVE_OLD_AGENT={}".format(cur_container_id))

        # Pull net-client if needed
        net_container_name = "supervisely-net-client-{}".format(constants.TOKEN())
        sly_net_container = None

        for container in self._docker_api.containers.list():
            if container.name == net_container_name:
                sly_net_container: Container = container
                break

        if sly_net_container is None:
            self.logger.warn(
                "Something goes wrong: can't find sly-net-client attached to this agent"
            )
        else:
            need_update = check_and_pull_sly_net_if_needed(
                self._docker_api, sly_net_container, self.logger
            )
            if need_update is True:
                cur_envs.append("UPDATE_SLY_NET_AFTER_RESTART=1")
            else:
                cur_envs.append("UPDATE_SLY_NET_AFTER_RESTART=0")

        # start new agent
        container = self._docker_api.containers.run(
            self.info["docker_image"],
            runtime=self.info["config"]["docker_runtime"],
            detach=True,
            name="supervisely-agent-{}-{}".format(constants.TOKEN(), sly.rand_str(5)),
            remove=False,
            restart_policy={"Name": "unless-stopped"},
            volumes=new_volumes,
            environment=cur_envs,
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
    sly_net_client_image_name = None,
) -> bool:
    ic = ImageCollection(dc)

    if sly_net_client_image_name is None:
        sly_net_client_image_name = cur_container.image.tags[0]

    docker_hub_image_info = ic.get_registry_data(sly_net_client_image_name)
    name_with_digest: str = cur_container.image.attrs.get("RepoDigests", [""])[0]

    if name_with_digest.endswith(docker_hub_image_info.id):
        logger.info("sly-net-client is already updated")
        return False
    else:
        logger.info("Found new version of sly-net-client. Pulling...")
        sly.docker_utils._docker_pull_progress(dc, sly_net_client_image_name, logger)
        return True
