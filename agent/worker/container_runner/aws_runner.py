import json
import os
import shlex
import time
import uuid
from pathlib import Path
from typing import Dict, Generator, List, Literal, Optional, Union

import construct as c
import websocket
from worker.container_runner.aws_utils import (
    ECSConfig,
    get_boto3_client,
    mirror_image_to_ecr,
    run_container_ec2,
    stream_task_logs,
)
from worker.container_runner.container_runner import BaseContainer, BaseContainerRunner


def parse_mem_limit_to_bytes(mem_limit) -> int:
    """Convert a memory limit value to an integer number of bytes.

    Accepts the same formats used by Docker's ``--memory`` flag.

    Args:
        mem_limit: Memory limit as one of:

            - ``int`` or ``float`` — treated directly as bytes.
            - ``str`` — a numeric string optionally suffixed with a unit:
              ``b`` (bytes), ``k`` (kibibytes), ``m`` (mebibytes), or
              ``g`` (gibibytes). Case-insensitive. An empty string returns
              ``None``.
            - ``None`` — returns ``None`` (no limit).

    Returns:
        The memory limit in bytes as an ``int``, or ``None`` if no limit was
        specified.

    Raises:
        ValueError: If ``mem_limit`` is of an unsupported type.

    Examples::

        parse_mem_limit_to_bytes("512m")   # -> 536870912
        parse_mem_limit_to_bytes("2g")     # -> 2147483648
        parse_mem_limit_to_bytes(1048576)  # -> 1048576
        parse_mem_limit_to_bytes(None)     # -> None
    """
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
    """A handle to a command execution session inside a running ECS container.

    Wraps the SSM WebSocket session returned by ``ecs.execute_command``. Use
    :meth:`stream_logs` to iterate over output lines and :meth:`get_exit_code`
    to retrieve the process exit code after the stream is exhausted.

    Args:
        session: The ``session`` dict from the ECS ``execute_command`` response,
            containing ``streamUrl`` and ``tokenValue``.
        exec_id: Short identifier for this exec (used to locate the PID file
            written by :meth:`AWSContainer.exec`).
    """

    def __init__(self, session: dict, exec_id: str):
        self._session = session
        self._exec_id = exec_id
        self._exit_code = None
        self._connection = self._init_connection()

    def _init_connection(self):
        """Open the SSM WebSocket connection and send the authentication token.

        Returns:
            An open ``websocket.WebSocket`` connection.

        Raises:
            RuntimeError: If ``session`` is ``None``.
        """
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
        """Yield output lines from the remote command until it finishes.

        Parses the binary SSM agent framing format, extracts ``output_stream_data``
        frames, and splits their payloads into individual lines. Sets
        ``_exit_code`` when an ``exit_code`` frame is received. Closes the
        WebSocket connection when the stream ends or on any exception.

        Yields:
            Individual output lines (strings) produced by the remote command,
            in order.
        """
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
        """Return the exit code of the remote command, if available.

        Returns:
            The integer exit code set when an ``exit_code`` frame is received,
            or ``None`` if the stream has not yet delivered that frame.
        """
        return self._exit_code

    def close(self):
        """Close the WebSocket connection, ignoring any errors.

        Safe to call multiple times; subsequent calls are no-ops.
        """
        if self._connection is not None:
            try:
                self._connection.close()
            except Exception:
                pass
            self._connection = None

    def __del__(self):
        self.close()


class AWSContainer(BaseContainer):
    """A handle to a running ECS task, implementing the ``BaseContainer`` interface.

    Provides lifecycle management (stop, wait, remove), command execution via
    ECS Execute Command, log streaming, and status inspection for a single ECS
    task.

    Args:
        task_arn: ARN of the ECS task this object represents.
        container_name: Name of the primary container within the task.
        task_definition_arn: ARN of the task definition revision used to launch
            the task (needed for log configuration lookup).
        ecs_config: ECS/ECR configuration for the cluster and region.
    """

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
        """Fetch the current ECS task description from the API.

        Returns:
            The task description dict as returned by ``ecs.describe_tasks``.

        Raises:
            RuntimeError: If the task is not found in the cluster.
        """
        response = self._ecs_client.describe_tasks(
            cluster=self._ecs_config.cluster, tasks=[self._task_arn]
        )
        if not response["tasks"]:
            raise RuntimeError(f"Task {self._task_arn} not found")
        return response["tasks"][0]

    def _get_status(self) -> str:
        """Return the current ECS ``lastStatus`` of the task.

        Returns:
            One of ``"PROVISIONING"``, ``"PENDING"``, ``"RUNNING"``,
            ``"DEPROVISIONING"``, or ``"STOPPED"``.
        """
        return self._describe_task()["lastStatus"]

    def stop(self, *, timeout: Optional[float] = None):
        """Send a stop request to the ECS task.

        The task transitions to ``STOPPED`` asynchronously. Use :meth:`wait`
        to block until it has fully stopped.

        Args:
            timeout: Unused. Present for interface compatibility.
        """
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
        """Block until the ECS task reaches ``STOPPED`` status.

        Polls the task status every second until the task stops or the optional
        timeout elapses.

        Args:
            timeout: Maximum number of seconds to wait. If ``None``, waits
                indefinitely.
            condition: Accepted values are ``"not-running"``, ``"next-exit"``,
                ``"removed"``, or ``None``; all are treated equivalently and
                resolve when the task reaches ``STOPPED``.

        Returns:
            A dict ``{"StatusCode": exit_code}`` where ``exit_code`` is the
            exit code of the first container (``0`` if not available).

        Raises:
            TimeoutError: If the task does not stop within ``timeout`` seconds.
        """
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
        """Block until the ECS Execute Command agent is ready inside the container.

        Polls the task description until the ``ExecuteCommandAgent`` managed
        agent reports ``RUNNING`` status for the target container.

        Args:
            timeout: Maximum seconds to wait before raising. Defaults to
                ``300``.
            poll_interval: Seconds between status polls. Defaults to ``2``.

        Raises:
            RuntimeError: If the task stops before the agent becomes ready.
            TimeoutError: If the agent does not become ready within ``timeout``
                seconds.
        """
        start = time.time()
        while True:
            task = self._describe_task()
            status = task["lastStatus"]

            if status == "STOPPED":
                stopped_reason = task.get("stoppedReason", "unknown")
                raise RuntimeError(
                    f"Task stopped before reaching RUNNING state: {stopped_reason}"
                )
            if time.time() - start > timeout:
                raise TimeoutError(
                    f"Task did not reach RUNNING state within {timeout}s (last status: {status})"
                )

            if status == "RUNNING":
                containers = task.get("containers", [])
                for container in containers:
                    if container["name"] == self._container_name:
                        managed_agents = container.get("managedAgents", [])
                        for agent in managed_agents:
                            if (
                                agent["name"] == "ExecuteCommandAgent"
                                and agent["lastStatus"] == "RUNNING"
                            ):
                                return

            time.sleep(poll_interval)

    def exec(self, command) -> AWSContainerExec:
        """Execute a shell command inside the running container.

        Wraps the command so it runs in the background and writes its PID to a
        temp file (enabling later cancellation via :meth:`exec_kill`), then
        opens an ECS Execute Command interactive session.

        Args:
            command: Shell command string to run inside the container.

        Returns:
            An :class:`AWSContainerExec` handle whose :meth:`~AWSContainerExec.stream_logs`
            method yields the command's output.
        """
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
        """Kill a background command previously started by :meth:`exec`.

        Sends ``kill`` to the PID recorded in ``/tmp/{exec_id}.pid`` inside the
        container.

        Args:
            exec_id: The exec ID returned implicitly via
                :class:`AWSContainerExec` (the 8-character hex string used as
                the PID file name).
        """
        self._ecs_client.execute_command(
            cluster=self._ecs_config.cluster,
            task=self._task_arn,
            container=self._container_name,
            command=f'bash -c "kill $(cat /tmp/{exec_id}.pid)"',
            interactive=False,
        )

    def remove(self, *, v: bool = False, link: bool = False, force: bool = False):
        """Remove the container, optionally stopping it first.

        ECS tasks are not explicitly deleted; this method only stops the task
        when ``force=True``. Present for interface compatibility with local
        container runners.

        Args:
            v: Unused (volume removal flag in Docker API).
            link: Unused (link removal flag in Docker API).
            force: If ``True``, attempt to stop the task before removal.
                Errors during stop are silently ignored.
        """
        if force:
            try:
                self.stop()
            except Exception:
                pass

    def is_running(self) -> bool:
        """Check whether the task is currently in ``RUNNING`` status.

        Returns:
            ``True`` if the task status is ``"RUNNING"``; ``False`` otherwise
            or if the status check raises an exception.
        """
        try:
            return self._get_status() == "RUNNING"
        except Exception:
            return False

    def is_alive(self) -> bool:
        """Check whether the task has not yet stopped or begun deprovisioning.

        Returns:
            ``True`` if the task status is neither ``"STOPPED"`` nor
            ``"DEPROVISIONING"``; ``False`` otherwise or on any exception.
        """
        try:
            return self._get_status() not in ("STOPPED", "DEPROVISIONING")
        except Exception:
            return False

    def stream_container_logs(self) -> Generator[str, None, None]:
        """Yield CloudWatch log lines from the container until the task stops.

        Delegates to :func:`~worker.container_runner.aws_utils.stream_task_logs`,
        resuming from the last pagination token if called multiple times.

        Yields:
            Individual log message strings in chronological order.
        """
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
        """Return the exit code of the first container, if the task has stopped.

        Returns:
            The integer exit code, or ``None`` if the task has not yet stopped
            or no container exit code is available.
        """
        task = self._describe_task()
        if task["lastStatus"] != "STOPPED":
            return None
        containers = task.get("containers", [])
        if not containers:
            return None
        return containers[0].get("exitCode")


class AWSContainerRunner(BaseContainerRunner):
    """A ``BaseContainerRunner`` implementation that runs containers on AWS ECS (EC2 launch type).

    Reads its configuration from a JSON file (default path:
    ``aws_config.json`` in the same directory, overridable via the
    ``AWS_CONFIG_PATH`` environment variable) and exposes the standard
    :meth:`prepare_image` and :meth:`spawn_container` interface.

    Expected keys in the config file:

    - ``cluster`` — ECS cluster name or ARN.
    - ``capacity_provider`` — EC2 capacity provider name.
    - ``task_definition`` — base task definition family or ARN.
    - ``ecr_host`` — ECR registry host.
    - ``mirroring_image_task_definition`` — task definition used for image mirroring.
    - ``region`` *(optional)* — AWS region; defaults to ``"us-east-1"``.
    """

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

    def prepare_image(self, image: str):
        """Ensure a Docker image is available in ECR, mirroring it if necessary.

        Delegates to :func:`~worker.container_runner.aws_utils.mirror_image_to_ecr`.
        Should be called before :meth:`spawn_container` to avoid cold-start
        delays when the image has not been mirrored yet.

        Args:
            image: Source Docker image reference (e.g.
                ``"supervisely/base-py-sdk-light:6.73.527"``).
        """
        mirror_image_to_ecr(image, self.ecs_config)

    def spawn_container(
        self,
        image: str,
        *,
        runtime: str = None,  # not used
        entrypoint: List = None,
        detach: bool = True,  # not used
        name: str = None,  # not used
        remove: bool = False,  # not used
        volumes: Dict = None,  # not used
        environment: Dict = None,
        labels: Dict = None,
        shm_size: int = None,  # not used
        stdin_open: bool = False,  # not used
        tty: bool = False,  # not used
        cpu_limit: int = None,
        mem_limit: Union[str, int] = None,
        memswap_limit: int = None,  # not used
        network: str = None,  # not used
        ipc_mode: str = None,
        security_opt: List[str] = None,  # not used
    ) -> AWSContainer:
        """Launch a new ECS task and return a handle to the running container.

        Converts Docker-style parameters to their ECS equivalents, registers a
        new task definition revision, and starts the task via the configured
        EC2 capacity provider. Several Docker-specific parameters are accepted
        for interface compatibility but are silently ignored.

        Args:
            image: Docker image URI to run (should already be mirrored to ECR
                via :meth:`prepare_image`).
            runtime: Ignored (Docker container runtime flag).
            entrypoint: Container entrypoint as a list of strings.
            detach: Ignored (tasks always start detached on ECS).
            name: Ignored (ECS tasks are identified by ARN, not name).
            remove: Ignored.
            volumes: Ignored (volume mounts are not supported in this runner).
            environment: Environment variables to inject as ``{"KEY": "value"}``
                pairs. All values are coerced to strings.
            labels: Resource tags to apply to the ECS task, converted to
                ``[{"key": ..., "value": ...}]`` format.
            shm_size: Ignored.
            stdin_open: Ignored.
            tty: Ignored.
            cpu_limit: CPU units for the container. Passed directly to the task
                definition revision.
            mem_limit: Memory limit in Docker format (e.g. ``"512m"``, ``"2g"``,
                or an integer number of bytes). Converted via
                :func:`parse_mem_limit_to_bytes`.
            memswap_limit: Ignored.
            network: Ignored.
            ipc_mode: IPC mode for the task (e.g. ``"host"``). An empty string
                is treated as ``None``.
            security_opt: Ignored.

        Returns:
            An :class:`AWSContainer` handle to the newly started task.
        """
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
