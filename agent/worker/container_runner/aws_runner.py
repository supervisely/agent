import json
from pathlib import Path
import shlex
import time
from worker.container_runner.container_runner import (
    BaseContainer,
    BaseContainerRunner,
)
import os
from typing import Dict, Generator, List, Literal, Optional, Union

from worker.container_runner.aws_utils import (
    mirror_image_to_ecr,
    run_container_ec2,
    get_boto3_client,
    stream_task_logs,
    ECSConfig,
)


import json
import uuid

import construct as c
import websocket


def parse_mem_limit_to_bytes(mem_limit) -> int:
    if isinstance(mem_limit, (int, float)):
        return int(mem_limit)

    if isinstance(mem_limit, str):
        if mem_limit == "":
            return None
        mem_limit = mem_limit.strip().lower()
        units = {"b": 1, "k": 1024, "m": 1024**2, "g": 1024**3}
        if mem_limit[-1] in units:
            return int(float(mem_limit[:-1]) * units[mem_limit[-1]])
        return int(mem_limit)  # no unit, assume bytes

    if mem_limit is None:
        return None

    raise ValueError(f"Unsupported mem_limit type: {type(mem_limit)}")
class AWSContainerExec:
    def __init__(self, session: dict, exec_id: str):
        self._session = session
        self._exec_id = exec_id
        self._exit_code = None
        self._connection = self._init_connection()

    def _init_connection(self):
        if self._session is None:
            raise RuntimeError("No active exec session found")

        connection = websocket.create_connection(self._session["streamUrl"])
        init_payload = {
            "MessageSchemaVersion": "1.0",
            "RequestId": str(uuid.uuid4()),
            "TokenValue": self._session["tokenValue"],
        }
        connection.send(json.dumps(init_payload))
        return connection

    def stream_logs(self) -> Generator[str, None, None]:
        AgentMessageHeader = c.Struct(
            "HeaderLength" / c.Int32ub,
            "MessageType" / c.PaddedString(32, "ascii"),
        )
        AgentMessagePayload = c.Struct(
            "PayloadLength" / c.Int32ub,
            "Payload" / c.PaddedString(c.this.PayloadLength, "ascii"),
        )

        try:
            while True:
                response = self._connection.recv()
                message = AgentMessageHeader.parse(response)
                message_type = message.MessageType.strip()

                if "channel_closed" in message_type:
                    break

                if "output_stream_data" in message_type:
                    payload_message = AgentMessagePayload.parse(
                        response[message.HeaderLength :]
                    )
                    for line in payload_message.Payload.splitlines():
                        yield line

                if "exit_code" in message_type:
                    payload_message = AgentMessagePayload.parse(
                        response[message.HeaderLength :]
                    )
                    self._exit_code = int(payload_message.Payload.strip())

        finally:
            self.close()

    def get_exit_code(self) -> Optional[int]:
        return self._exit_code

    def close(self):
        if self._connection is not None:
            try:
                self._connection.close()
            except Exception:
                pass
            self._connection = None

    def __del__(self):
        self.close()


class AWSContainer(BaseContainer):
    def __init__(
        self,
        task_arn: str,
        container_name: str,
        task_definition_arn: str,
        ecs_config: ECSConfig,
    ):
        self._task_arn = task_arn
        self._container_name = container_name
        self._task_definition_arn = task_definition_arn
        self._ecs_config = ecs_config
        self._ecs_client = get_boto3_client("ecs", self._ecs_config.region)
        self._logs_token = None
        self._session = None

    def _describe_task(self) -> dict:
        response = self._ecs_client.describe_tasks(
            cluster=self._ecs_config.cluster, tasks=[self._task_arn]
        )
        if not response["tasks"]:
            raise RuntimeError(f"Task {self._task_arn} not found")
        return response["tasks"][0]

    def _get_status(self) -> str:
        """Returns ECS last status: PROVISIONING, PENDING, RUNNING, DEPROVISIONING, STOPPED."""
        return self._describe_task()["lastStatus"]

    def stop(self, *, timeout: Optional[float] = None):
        self._ecs_client.stop_task(
            cluster=self._ecs_config.cluster,
            task=self._task_arn,
            reason="Stopped by AWSContainer.stop()",
        )

    def wait(
        self,
        *,
        timeout: Optional[float] = None,
        condition: Literal["not-running", "next-exit", "removed"] = None,
    ) -> Dict:
        start = time.time()
        poll_interval = 1

        while True:
            task = self._describe_task()
            status = task["lastStatus"]

            is_done = (
                status == "STOPPED"
                if condition in (None, "not-running", "next-exit", "removed")
                else False
            )

            if is_done:
                containers = task.get("containers", [])
                exit_code = containers[0].get("exitCode", 0) if containers else 0
                return {"StatusCode": exit_code}

            if timeout is not None and (time.time() - start) > timeout:
                raise TimeoutError(
                    f"Task {self._task_arn} did not stop within {timeout}s"
                )

            time.sleep(poll_interval)

    def wait_for_execute(self, timeout: float = 300, poll_interval: float = 2):
        start = time.time()
        while True:
            task = self._describe_task()
            status = task["lastStatus"]

            if status == "STOPPED":
                stopped_reason = task.get("stoppedReason", "unknown")
                raise RuntimeError(f"Task stopped before reaching RUNNING state: {stopped_reason}")
            if time.time() - start > timeout:
                raise TimeoutError(f"Task did not reach RUNNING state within {timeout}s (last status: {status})")

            if status == "RUNNING":
                containers = task.get("containers", [])
                for container in containers:
                    if container["name"] == self._container_name:
                        managed_agents = container.get("managedAgents", [])
                        for agent in managed_agents:
                            if agent["name"] == "ExecuteCommandAgent" and agent["lastStatus"] == "RUNNING":
                                return

            time.sleep(poll_interval)

    def exec(self, command) -> AWSContainerExec:
        exec_id = str(uuid.uuid4()).replace("-", "")[:8]
        pid_file = f"/tmp/{exec_id}.pid"
        inner = f"{command} & echo $! > {pid_file}"
        wrapped = f"bash -c {shlex.quote(inner)}"

        print("Waiting for container to be ready for exec...")
        self.wait_for_execute()
        
        exec_resp = self._ecs_client.execute_command(
            cluster=self._ecs_config.cluster,
            task=self._task_arn,
            container=self._container_name,
            command=wrapped,
            interactive=True,
        )
        return AWSContainerExec(session=exec_resp["session"], exec_id=exec_id)

    def exec_kill(self, exec_id: str):
        self._ecs_client.execute_command(
            cluster=self._ecs_config.cluster,
            task=self._task_arn,
            container=self._container_name,
            command=f'bash -c "kill $(cat /tmp/{exec_id}.pid)"',
            interactive=False,
        )

    def remove(self, *, v: bool = False, link: bool = False, force: bool = False):
        if force:
            try:
                self.stop()
            except Exception:
                pass

    def is_running(self) -> bool:
        try:
            return self._get_status() == "RUNNING"
        except Exception:
            return False

    def is_alive(self) -> bool:
        try:
            return self._get_status() not in ("STOPPED", "DEPROVISIONING")
        except Exception:
            return False

    def stream_container_logs(self) -> Generator[str, None, None]:
        yield from stream_task_logs(
            self._ecs_client,
            self._ecs_config.region,
            self._ecs_config.cluster,
            self._task_arn,
            self._task_definition_arn,
            self._container_name,
            self._logs_token,
        )

    def get_exit_code(self) -> Optional[int]:
        task = self._describe_task()
        if task["lastStatus"] != "STOPPED":
            return None
        containers = task.get("containers", [])
        if not containers:
            return None
        return containers[0].get("exitCode")


class AWSContainerRunner(BaseContainerRunner):
    def __init__(self):
        aws_config_path = os.environ.get(
            "AWS_CONFIG_PATH", Path(__file__).parent / "aws_config.json"
        )
        with open(aws_config_path, "r") as f:
            aws_config = json.load(f)
        self.ecs_config = ECSConfig(
            region=aws_config.get("region", "us-east-1"),
            cluster=aws_config["cluster"],
            capacity_provider=aws_config["capacity_provider"],
            task_definition=aws_config["task_definition"],
            ecr_host=aws_config["ecr_host"],
            mirroring_image_task_definition=aws_config[
                "mirroring_image_task_definition"
            ],
        )

    def prepare_image(self, image):
        mirror_image_to_ecr(image, self.ecs_config)

    def spawn_container(
        self,
        image,
        *,
        runtime: str = None,  # not used
        entrypoint: List = None,
        detach: bool = True,  # not used
        name: str = None,  # not used
        remove: bool = False,  # not used
        volumes: Dict = None,  # not used
        environment: Dict = None,
        labels: Dict = None,
        shm_size: int = None, # not used
        stdin_open: bool = False,  # not used
        tty: bool = False,  # not used
        cpu_limit: int = None,
        mem_limit: Union[str, int] = None,
        memswap_limit: int = None,  # not used
        network: str = None,  # not used
        ipc_mode: str = None,
        security_opt: List[str] = None,  # not used
    ) -> AWSContainer:
        memory = parse_mem_limit_to_bytes(mem_limit)
        if ipc_mode == "":
            ipc_mode = None
        environment = {k: str(v) for k, v in (environment or {}).items()}
        task_arn, container_name, task_definition_arn = run_container_ec2(
            docker_image_name=image,
            entrypoint=entrypoint,
            command=None,
            ecs_config=self.ecs_config,
            env_vars=environment,
            cpu=cpu_limit,
            memory=memory,
            tags=[{"key": k, "value": v} for k, v in (labels or {}).items()],
            ipc_mode=ipc_mode,
        )
        return AWSContainer(
            task_arn, container_name, task_definition_arn, self.ecs_config
        )
