from logging import Logger
from typing import Dict, Generator, List, Literal, Optional

import docker
import supervisely as sly
from docker.errors import APIError, DockerException, NotFound
from docker.models.containers import Container
from worker import constants, docker_utils
from worker.agent_utils import convert_millicores_to_cpu_quota
from worker.container_runner.container_runner import (
    BaseContainer,
    BaseContainerExec,
    BaseContainerRunner,
)
from worker.task_dockerized import ErrorReport


class LocalContainerExec(BaseContainerExec):
    def __init__(self, docker_client: docker.DockerClient, exec_id: str):
        self._docker_client = docker_client
        self._exec_id = exec_id

    def stream_logs(self) -> Generator[str, None, None]:
        for log_line in self._docker_client.api.exec_start(self._exec_id, stream=True):
            yield log_line.decode("utf-8")

    def get_exit_code(self) -> int:
        exec_info = self._docker_client.api.exec_inspect(self._exec_id)
        exit_code = exec_info["ExitCode"]
        return exit_code


class LocalContainer(BaseContainer):
    def __init__(self, container: Container, docker_client: docker.DockerClient):
        self._container = container
        self._docker_client = docker_client

    def stop(self, *, timeout: Optional[float] = None):
        self._container.stop(timeout=timeout)

    def wait(
        self,
        *,
        timeout: Optional[float] = None,
        condition: Literal["not-running", "next-exit", "removed"] = None,
    ) -> Dict:
        result = self._container.wait(timeout=timeout, condition=condition)
        return result

    def remove(self, *, v: bool = False, link: bool = False, force: bool = False):
        return self._container.remove(v=v, link=link, force=force)

    def is_running(self) -> bool:
        if self._container is None:
            return False
        try:
            self._container.reload()
            return self._container.status == "running"
        except NotFound:
            return False

    def is_alive(self):
        return self.is_running()

    def exec(self, command) -> LocalContainerExec:
        exec_id = self._docker_client.api.exec_create(
            self._container.id,
            cmd=command,
        )
        return LocalContainerExec(self._docker_client, exec_id)

    def exec_kill(self, exec_id: str):
        exec_info = self._docker_client.api.exec_inspect(exec_id)
        if exec_info["Running"] == True:
            pid = exec_info["Pid"]
            self._container.exec_run(cmd="kill {}".format(pid))
        else:
            return

    def get_exit_code(self):
        self._container.reload()
        return self._container.attrs["State"]["ExitCode"]


class LocalContainerRunner(BaseContainerRunner):
    def __init__(self, docker_client: docker.DockerClient, logger: Logger):
        self.docker_client = docker_client
        self.logger = logger

        self._container: LocalContainer = None

    def prepare_image(self, image):
        docker_utils.docker_pull_if_needed(
            self.docker_client,
            image,
            constants.PULL_POLICY(),
            self.logger,
        )

        # self.sync_pip_cache()

    def spawn_container(
        self,
        image,
        *,
        runtime: str = None,
        entrypoint: List = None,
        detach: bool = True,
        name: str = None,
        remove: bool = False,
        volumes: Dict = None,
        environment: Dict = None,
        labels: Dict = None,
        shm_size: int = None,
        stdin_open: bool = False,
        tty: bool = False,
        cpu_limit: int = None,
        mem_limit: int = None,
        memswap_limit: int = None,
        network: str = None,
        ipc_mode: str = None,
        security_opt: List[str] = None,
    ) -> LocalContainer:
        print("Spawning container with image: {}".format(image))
        if cpu_limit is None:
            cpu_limit = constants.CPU_LIMIT()
        if cpu_limit is not None:
            cpu_quota = convert_millicores_to_cpu_quota(cpu_limit)
        else:
            cpu_quota = None
        container = self.docker_client.containers.run(
            image,
            runtime=runtime,
            entrypoint=entrypoint,
            detach=detach,
            name=name,
            remove=remove,  # TODO: check autoremove
            volumes=volumes,
            environment=environment,
            labels=labels,
            shm_size=shm_size,
            stdin_open=stdin_open,
            tty=tty,
            cpu_quota=cpu_quota,
            mem_limit=mem_limit,
            memswap_limit=memswap_limit,
            network=network,
            ipc_mode=ipc_mode,
            security_opt=security_opt,
        )
        container.reload()
        self._container = LocalContainer(container, self.docker_client)
        self.logger.debug(
            "After spawning. Container status: {}".format(str(container.status))
        )
        self.logger.info(
            "Docker container is spawned",
            extra={
                "container_id": container.id,
                "container_name": container.name,
            },
        )
        return self._container

    def exec(self, command, environment=None):
        self._exec_id = self.docker_client.api.exec_create(
            self._container._container.id,
            cmd=command,
            environment=environment,
        )

    def stream_logs(self) -> Generator[str, None, None]:
        for log_line in self._container._container.logs(stream=True):
            yield log_line.decode("utf-8")
