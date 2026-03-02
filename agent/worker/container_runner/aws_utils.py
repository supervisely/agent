import os
import time
from dataclasses import dataclass
from typing import Dict, List, Tuple, Union

import boto3


@dataclass
class ECSConfig:
    """Configuration for interacting with AWS ECS and ECR.

    Attributes:
        cluster: Name or ARN of the ECS cluster to run tasks on.
        capacity_provider: Name of the EC2 capacity provider used for EC2 launch type tasks.
        task_definition: Base task definition family or ARN used for EC2 tasks.
        ecr_host: ECR registry host (e.g. ``123456789.dkr.ecr.us-east-1.amazonaws.com``).
        mirroring_image_task_definition: Base task definition used for image mirroring
            (Fargate) tasks.
        region: AWS region. Defaults to ``"us-east-1"``.
    """

    cluster: str
    capacity_provider: str
    task_definition: str
    ecr_host: str
    mirroring_image_task_definition: str
    region: str = "us-east-1"


def get_boto3_client(service: str, region: str):
    """Create a boto3 client authenticated via environment variables.

    Args:
        service: AWS service name (e.g. ``"ecs"``, ``"ecr"``, ``"ec2"``).
        region: AWS region name (e.g. ``"us-east-1"``).

    Returns:
        A boto3 client for the specified service and region.
    """
    return boto3.client(
        service,
        region_name=region,
        aws_access_key_id=os.environ["AWS_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["AWS_SECRET_ACCESS_KEY"],
    )


def get_default_network_config(ec2_client, assign_public_ip: bool = True) -> dict:
    """Build an ECS ``awsvpcConfiguration`` dict from the account's default VPC.

    Discovers the default VPC, all of its subnets, and the default security group,
    then assembles the network configuration expected by ``ecs_client.run_task``.

    Args:
        ec2_client: A boto3 EC2 client.
        assign_public_ip: Whether to enable ``assignPublicIp``. Defaults to ``True``.

    Returns:
        A dict suitable for passing as ``networkConfiguration`` to ``run_task``.
    """
    vpcs = ec2_client.describe_vpcs(Filters=[{"Name": "isDefault", "Values": ["true"]}])
    vpc_id = vpcs["Vpcs"][0]["VpcId"]

    subnets = ec2_client.describe_subnets(
        Filters=[{"Name": "vpc-id", "Values": [vpc_id]}]
    )
    subnet_ids = [s["SubnetId"] for s in subnets["Subnets"]]

    sgs = ec2_client.describe_security_groups(
        Filters=[
            {"Name": "vpc-id", "Values": [vpc_id]},
            {"Name": "group-name", "Values": ["default"]},
        ]
    )
    sg_id = sgs["SecurityGroups"][0]["GroupId"]

    config = {
        "awsvpcConfiguration": {
            "subnets": subnet_ids,
            "securityGroups": [sg_id],
        }
    }

    if assign_public_ip:
        config["awsvpcConfiguration"]["assignPublicIp"] = "ENABLED"

    return config


def _parse_image_to_ecr_path(
    docker_image_name: str, ecr_host: str
) -> tuple[str, str, str]:
    """Parse a Docker image name into its ECR repository components.

    Strips any existing registry prefix and splits the image path into a
    repository name and tag, then constructs the full ECR URI.

    Example::

        _parse_image_to_ecr_path(
            "supervisely/base-py-sdk-light:6.73.527",
            "123.dkr.ecr.us-east-1.amazonaws.com"
        )
        # -> ("supervisely/base-py-sdk-light", "6.73.527",
        #     "123.dkr.ecr.us-east-1.amazonaws.com/supervisely/base-py-sdk-light:6.73.527")

    Args:
        docker_image_name: Docker image reference, optionally including a registry
            prefix (e.g. ``"docker.io/library/ubuntu:22.04"``).
        ecr_host: ECR registry host to use as the target registry prefix.

    Returns:
        A 3-tuple of ``(repository_name, image_tag, target_image)``.
    """
    # Strip any existing registry prefix (anything before the first slash that contains a dot or colon)
    parts = docker_image_name.split("/")
    if "." in parts[0] or ":" in parts[0]:
        image_path = "/".join(parts[1:])
    else:
        image_path = docker_image_name

    if ":" in image_path:
        repository_name, image_tag = image_path.rsplit(":", 1)
    else:
        repository_name = image_path
        image_tag = "latest"

    target_image = f"{ecr_host}/{repository_name}:{image_tag}"
    return repository_name, image_tag, target_image


def _ensure_ecr_repository(ecr_client, repository_name: str):
    """Create an ECR repository if it does not already exist.

    Silently ignores ``RepositoryAlreadyExistsException`` so this function is
    safe to call unconditionally before pushing an image.

    Args:
        ecr_client: A boto3 ECR client.
        repository_name: Repository name, which may contain slashes for
            namespaced repos (e.g. ``"org/repo"``).
    """
    try:
        ecr_client.create_repository(repositoryName=repository_name)
        print(f"Created ECR repository: {repository_name}")
    except ecr_client.exceptions.RepositoryAlreadyExistsException:
        pass


def _image_exists_in_ecr(ecr_client, repository_name: str, image_tag: str) -> bool:
    """Check whether a specific image tag exists in an ECR repository.

    Args:
        ecr_client: A boto3 ECR client.
        repository_name: Name of the ECR repository to query.
        image_tag: Image tag to look up (e.g. ``"latest"`` or ``"1.2.3"``).

    Returns:
        ``True`` if the image tag is found; ``False`` if the repository or
        image does not exist.
    """
    try:
        ecr_client.describe_images(
            repositoryName=repository_name, imageIds=[{"imageTag": image_tag}]
        )
        return True
    except ecr_client.exceptions.ImageNotFoundException:
        return False
    except ecr_client.exceptions.RepositoryNotFoundException:
        return False


def _create_task_definition_revision(
    ecs_client,
    base_task_definition: str,
    new_image: str = None,
    entrypoint: list[str] = None,
    cpu: int = None,
    memory: int = None,
    gpu: int = None,
    ipc_mode: str = None,
) -> tuple[str, str]:
    """Register a new revision of a task definition with optional overrides.

    Fetches the most recent active revision of ``base_task_definition``, applies
    the requested overrides to the first container, copies all supported optional
    fields, and registers the result as a new revision.

    Args:
        ecs_client: A boto3 ECS client.
        base_task_definition: Family name or ARN of the task definition to clone.
        new_image: Docker image URI to set on the first container. If ``None``,
            the existing image is preserved.
        entrypoint: Override for the container ``entryPoint``. If ``None``, the
            existing entrypoint is preserved.
        cpu: CPU units to assign to the first container. If ``None``, the
            existing value is preserved.
        memory: Memory (MiB) to assign to the first container. If ``None``, the
            existing value is preserved.
        gpu: Number of GPUs to request via ``resourceRequirements``. If ``None``,
            no GPU requirement is set.
        ipc_mode: IPC mode for the task (e.g. ``"host"``). Overrides any value
            in the base definition when provided.

    Returns:
        A 2-tuple of ``(task_definition_arn, container_name)`` where
        ``task_definition_arn`` is the ARN of the newly registered revision and
        ``container_name`` is the name of the first container.
    """
    task_def_response = ecs_client.describe_task_definition(
        taskDefinition=base_task_definition
    )
    task_def = task_def_response["taskDefinition"]

    container_name = task_def["containerDefinitions"][0]["name"]

    container_definitions = []
    for container in task_def["containerDefinitions"]:
        container_copy = container.copy()
        if new_image is not None:
            container_copy["image"] = new_image
        if entrypoint is not None:
            container_copy["entryPoint"] = entrypoint
        if cpu is not None:
            container_copy["cpu"] = cpu
        if memory is not None:
            container_copy["memory"] = memory
        if gpu is not None:
            container_copy["resourceRequirements"] = [
                {"type": "GPU", "value": str(gpu)}
            ]
        container_definitions.append(container_copy)

    register_params = {
        "family": task_def["family"],
        "containerDefinitions": container_definitions,
    }

    if ipc_mode is not None:
        register_params["ipcMode"] = ipc_mode

    optional_fields = [
        "taskRoleArn",
        "executionRoleArn",
        "networkMode",
        "volumes",
        "placementConstraints",
        "requiresCompatibilities",
        "cpu",
        "memory",
        "tags",
        "pidMode",
        "ipcMode",
        "proxyConfiguration",
        "inferenceAccelerators",
        "ephemeralStorage",
        "runtimePlatform",
    ]
    for field in optional_fields:
        if field in task_def and field not in register_params:
            register_params[field] = task_def[field]

    new_task_def = ecs_client.register_task_definition(**register_params)
    arn = new_task_def["taskDefinition"]["taskDefinitionArn"]
    print(f"Registered task definition revision: {arn}")
    return arn, container_name


def _get_task_log_config(
    ecs_client, task_definition_arn: str, container_name: str
) -> dict | None:
    """Extract the CloudWatch Logs configuration for a named container.

    Looks up the task definition and returns the ``awslogs`` log driver options
    for ``container_name`` if they are present.

    Args:
        ecs_client: A boto3 ECS client.
        task_definition_arn: Full ARN of the task definition to inspect.
        container_name: Name of the container whose log config to retrieve.

    Returns:
        A dict with keys ``log_group``, ``log_stream_prefix``, and ``region``
        if the container uses the ``awslogs`` driver; otherwise ``None``.
    """
    task_def = ecs_client.describe_task_definition(taskDefinition=task_definition_arn)
    for container in task_def["taskDefinition"]["containerDefinitions"]:
        if container["name"] == container_name:
            log_config = container.get("logConfiguration", {})
            if log_config.get("logDriver") == "awslogs":
                options = log_config.get("options", {})
                return {
                    "log_group": options.get("awslogs-group"),
                    "log_stream_prefix": options.get("awslogs-stream-prefix", "ecs"),
                    "region": options.get("awslogs-region"),
                }
    return None


def _stream_task_logs(
    logs_client, log_group: str, log_stream: str, next_token: str = None
) -> str | None:
    """Print new log events from a CloudWatch log stream and return the next token.

    Paginates through all available events since ``next_token``, printing each
    message to stdout. Stops when no new events are returned.

    Args:
        logs_client: A boto3 CloudWatch Logs client.
        log_group: Name of the CloudWatch log group.
        log_stream: Name of the log stream within the group.
        next_token: Pagination token from a previous call. Pass ``None`` to
            start from the beginning of the stream.

    Returns:
        The ``nextForwardToken`` to pass on the next call, or ``None`` if the
        stream was not found.
    """
    while True:
        kwargs = {
            "logGroupName": log_group,
            "logStreamName": log_stream,
            "startFromHead": True,
        }
        if next_token:
            kwargs["nextToken"] = next_token
            del kwargs["startFromHead"]  # mutually exclusive with nextToken

        try:
            response = logs_client.get_log_events(**kwargs)
        except logs_client.exceptions.ResourceNotFoundException:
            break

        events = response.get("events", [])
        for event in events:
            print(event["message"])

        new_token = response.get("nextForwardToken")
        if new_token == next_token or not events:
            return new_token
        next_token = new_token


def _wait_for_task_and_logs(
    ecs_client,
    region: str,
    cluster: str,
    task_arn: str,
    task_definition_arn: str,
    container_name: str,
    poll_interval: int = 1,
):
    """Block until an ECS task stops, streaming its CloudWatch logs to stdout.

    Polls the task status every ``poll_interval`` seconds. On each iteration,
    any new log events are printed. Raises ``RuntimeError`` if any container
    exits with a non-zero code or if the task stops before the container starts.

    Args:
        ecs_client: A boto3 ECS client.
        region: AWS region of the task and log group.
        cluster: Name or ARN of the ECS cluster.
        task_arn: ARN of the running task to monitor.
        task_definition_arn: ARN of the task definition (used to resolve the
            log configuration).
        container_name: Name of the container whose logs to stream.
        poll_interval: Seconds to wait between status polls. Defaults to ``1``.

    Raises:
        RuntimeError: If any container exits with a non-zero exit code, or if
            the task stops before a container starts.
    """
    logs_client = get_boto3_client("logs", region)
    log_config = _get_task_log_config(ecs_client, task_definition_arn, container_name)

    task_id = task_arn.split("/")[-1]
    next_log_token = None

    if log_config:
        log_stream = f"{log_config['log_stream_prefix']}/{container_name}/{task_id}"
        print(f"Streaming logs from {log_config['log_group']}/{log_stream}")

    while True:
        response = ecs_client.describe_tasks(cluster=cluster, tasks=[task_arn])
        task = response["tasks"][0]
        status = task["lastStatus"]
        print(f"Task status: {status}")

        if log_config:
            next_log_token = _stream_task_logs(
                logs_client,
                log_config["log_group"],
                log_stream,
                next_token=next_log_token,
            )

        if status == "STOPPED":
            stopped_reason = task.get("stoppedReason", "")
            if stopped_reason:
                print(f"Task stopped reason: {stopped_reason}")

            containers = task.get("containers", [])
            for container in containers:
                if container.get("exitCode", 0) != 0:
                    raise RuntimeError(
                        f"Task {task_arn} failed: container '{container['name']}' "
                        f"exited with code {container['exitCode']}. "
                        f"Reason: {container.get('reason', 'unknown')}"
                    )

            if stopped_reason and not containers:
                raise RuntimeError(
                    f"Task {task_arn} failed before container started: {stopped_reason}"
                )

            return

        time.sleep(poll_interval)


def _collect_log_lines(
    logs_client, log_group: str, log_stream: str, next_token: str = None
) -> tuple[str | None, list[str]]:
    """Fetch new log lines from a CloudWatch log stream since the last token.

    Paginates until no new events are available and collects all message strings.

    Args:
        logs_client: A boto3 CloudWatch Logs client.
        log_group: Name of the CloudWatch log group.
        log_stream: Name of the log stream within the group.
        next_token: Pagination token from a previous call. Pass ``None`` to
            start from the beginning of the stream.

    Returns:
        A 2-tuple of ``(next_token, lines)`` where ``next_token`` is the forward
        pagination token for subsequent calls and ``lines`` is a list of log
        message strings collected in this call.
    """
    lines = []
    while True:
        kwargs = {
            "logGroupName": log_group,
            "logStreamName": log_stream,
            "startFromHead": True,
        }
        if next_token:
            kwargs["nextToken"] = next_token
            del kwargs["startFromHead"]

        try:
            response = logs_client.get_log_events(**kwargs)
        except logs_client.exceptions.ResourceNotFoundException:
            return next_token, lines

        events = response.get("events", [])
        lines.extend(event["message"] for event in events)

        new_token = response.get("nextForwardToken")
        if new_token == next_token or not events:
            return new_token, lines
        next_token = new_token


def stream_task_logs(
    ecs_client,
    region: str,
    cluster: str,
    task_arn: str,
    task_definition_arn: str,
    container_name: str,
    poll_interval: int = 1,
    next_log_token: str = None,
):
    """Yield log lines from a running ECS task until it stops.

    Polls the task status and CloudWatch log stream in a loop, yielding each
    new log line as it becomes available. Returns when the task reaches
    ``STOPPED`` status.

    Args:
        ecs_client: A boto3 ECS client.
        region: AWS region of the task and log group.
        cluster: Name or ARN of the ECS cluster.
        task_arn: ARN of the running task to tail.
        task_definition_arn: ARN of the task definition (used to resolve the
            log configuration).
        container_name: Name of the container whose logs to stream.
        poll_interval: Seconds to wait between log/status polls. Defaults to ``1``.
        next_log_token: Optional starting pagination token. Pass ``None`` to
            stream from the beginning.

    Yields:
        Individual log message strings in chronological order.

    Raises:
        RuntimeError: If no CloudWatch log configuration is found for the
            specified container.
    """
    logs_client = get_boto3_client("logs", region)
    log_config = _get_task_log_config(ecs_client, task_definition_arn, container_name)

    if not log_config:
        raise RuntimeError("No CloudWatch log configuration found for container")

    task_id = task_arn.split("/")[-1]
    log_stream = f"{log_config['log_stream_prefix']}/{container_name}/{task_id}"

    while True:
        status = ecs_client.describe_tasks(cluster=cluster, tasks=[task_arn])["tasks"][
            0
        ]["lastStatus"]

        next_log_token, lines = _collect_log_lines(
            logs_client, log_config["log_group"], log_stream, next_log_token
        )
        yield from lines

        if status == "STOPPED":
            return

        time.sleep(poll_interval)


def run_container_fargate(
    ecs_config: ECSConfig,
    docker_image_name: str = None,
    entrypoint: Union[str, List[str]] = None,
    command: Union[str, List[str]] = None,
    env_vars: dict = None,
    cpu: int = None,
    memory: int = None,
    tags: List[Dict] = None,
    wait: bool = False,
) -> str:
    """Run a container on AWS Fargate and optionally wait for it to finish.

    Creates a new task definition revision from
    ``ecs_config.mirroring_image_task_definition``, applies the provided
    overrides, and launches it with the ``FARGATE`` launch type using the
    account's default VPC network configuration.

    Args:
        ecs_config: ECS/ECR configuration including cluster and task definition
            details.
        docker_image_name: Docker image URI to run. If ``None``, the image from
            the base task definition is used.
        entrypoint: Container entrypoint. A string is split on whitespace into a
            list. Defaults to the base task definition's entrypoint.
        command: Container command. A string is split on whitespace into a list.
            Defaults to the base task definition's command.
        env_vars: Additional environment variables to inject into the container
            as ``{"KEY": "value"}`` pairs.
        cpu: CPU units to allocate. Defaults to the base task definition value.
        memory: Memory in MiB to allocate. Defaults to the base task definition
            value.
        tags: List of ECS resource tags in ``[{"key": ..., "value": ...}]`` form.
        wait: If ``True``, block until the task stops and stream its logs.
            Defaults to ``False``.

    Returns:
        The ARN of the started Fargate task.

    Raises:
        RuntimeError: If ECS reports failures and no task ARN is returned, or
            if ``wait=True`` and the task exits with a non-zero status.
    """
    if isinstance(command, str):
        command = command.split() if command else []
    if isinstance(entrypoint, str):
        entrypoint = entrypoint.split() if entrypoint else []

    ecs_client = get_boto3_client("ecs", ecs_config.region)
    ec2_client = get_boto3_client("ec2", ecs_config.region)

    task_definition_arn, container_name = _create_task_definition_revision(
        ecs_client,
        ecs_config.mirroring_image_task_definition,
        new_image=docker_image_name,
        entrypoint=entrypoint,
        cpu=cpu,
        memory=memory,
    )

    container_overrides = {
        "name": container_name,
        "command": command,
        "environment": [{"name": k, "value": v} for k, v in (env_vars or {}).items()],
    }
    container_overrides = {
        k: v for k, v in container_overrides.items() if v
    }  # Remove empty fields

    response = ecs_client.run_task(
        cluster=ecs_config.cluster,
        taskDefinition=task_definition_arn,
        launchType="FARGATE",
        networkConfiguration=get_default_network_config(
            ec2_client, assign_public_ip=True
        ),
        overrides={"containerOverrides": [container_overrides]},
        tags=tags or [],
    )

    if not response["tasks"]:
        failures = response.get("failures", [])
        raise RuntimeError(f"Failed to start Fargate task: {failures}")

    task_arn = response["tasks"][0]["taskArn"]
    print(f"Started Fargate task: {task_arn}")

    if wait:
        _wait_for_task_and_logs(
            ecs_client,
            ecs_config.region,
            ecs_config.cluster,
            task_arn,
            task_definition_arn,
            container_name,
        )

    return task_arn


def mirror_image_to_ecr(
    docker_image_name: str,
    ecs_config: ECSConfig,
) -> str:
    """Ensure a public Docker image is mirrored into ECR, pulling it if needed.

    Checks whether the image already exists in ECR. If it does, returns the ECR
    URI immediately. If not, launches a Fargate mirroring task (using
    ``ecs_config.mirroring_image_task_definition``) that pulls the source image
    and pushes it to ECR, then waits for the task to finish.

    Args:
        docker_image_name: Source Docker image reference, e.g.
            ``"supervisely/base-py-sdk-light:6.73.527"``.
        ecs_config: ECS/ECR configuration including the ECR host and cluster
            details.

    Returns:
        The full ECR image URI (e.g.
        ``"123.dkr.ecr.us-east-1.amazonaws.com/supervisely/base-py-sdk-light:6.73.527"``).

    Raises:
        RuntimeError: If the mirroring task fails or exits with a non-zero status.
    """
    ecr_client = get_boto3_client("ecr", ecs_config.region)
    repository_name, image_tag, target_image = _parse_image_to_ecr_path(
        docker_image_name, ecs_config.ecr_host
    )

    print(f"Checking ECR for {target_image}...")
    _ensure_ecr_repository(ecr_client, repository_name)

    if _image_exists_in_ecr(ecr_client, repository_name, image_tag):
        print(f"Image already exists in ECR: {target_image}")
        return target_image

    print(
        f"Image not found in ECR. Launching mirroring task for {docker_image_name}..."
    )
    run_container_fargate(
        ecs_config=ecs_config,
        docker_image_name=None,
        entrypoint=None,
        command=None,
        env_vars={
            "AWS_REGION": ecs_config.region,
            "ECR_HOST": ecs_config.ecr_host,
            "SOURCE_IMAGE": docker_image_name,
            "TARGET_IMAGE": target_image,
            "AWS_ACCESS_KEY_ID": os.environ["AWS_ACCESS_KEY_ID"],
            "AWS_SECRET_ACCESS_KEY": os.environ["AWS_SECRET_ACCESS_KEY"],
        },
        wait=True,
    )

    print(f"Mirroring complete: {target_image}")
    return target_image


def run_container_ec2(
    docker_image_name: str,
    entrypoint: Union[str, List[str]],
    command: Union[str, List[str]],
    ecs_config: ECSConfig,
    env_vars: dict = None,
    cpu: int = None,
    memory: int = None,
    gpu: int = None,
    ipc_mode: str = None,
    tags: List[Dict] = None,
    wait: bool = False,
) -> Tuple[str, str, str]:
    """Run a container via the EC2 capacity provider and optionally wait for it.

    Creates a new task definition revision from ``ecs_config.task_definition``,
    applies the provided overrides, and launches the task using
    ``ecs_config.capacity_provider`` with ``enableExecuteCommand`` enabled.

    Args:
        docker_image_name: Docker image URI to run.
        entrypoint: Container entrypoint. A string is split on whitespace into a
            list.
        command: Container command. A single string is wrapped in a list.
        ecs_config: ECS/ECR configuration including the cluster and capacity
            provider details.
        env_vars: Additional environment variables to inject into the container
            as ``{"KEY": "value"}`` pairs.
        cpu: CPU units for the first container. Defaults to the base task
            definition value.
        memory: Memory in MiB for the first container. Defaults to the base task
            definition value.
        gpu: Number of GPUs to request. Defaults to no GPU requirement.
        ipc_mode: IPC mode for the task (e.g. ``"host"``).
        tags: List of ECS resource tags in ``[{"key": ..., "value": ...}]`` form.
        wait: If ``True``, block until the task stops and stream its logs.
            Defaults to ``False``.

    Returns:
        A 3-tuple of ``(task_arn, container_name, task_definition_arn)``.

    Raises:
        RuntimeError: If ECS reports failures and no task ARN is returned, or
            if ``wait=True`` and the task exits with a non-zero status.
    """
    if isinstance(command, str):
        command = [command]
    if isinstance(entrypoint, str):
        entrypoint = entrypoint.split() if entrypoint else []

    ecs_client = get_boto3_client("ecs", ecs_config.region)
    ec2_client = get_boto3_client("ec2", ecs_config.region)

    task_definition_arn, container_name = _create_task_definition_revision(
        ecs_client,
        ecs_config.task_definition,
        docker_image_name,
        entrypoint=entrypoint,
        cpu=cpu,
        memory=memory,
        gpu=gpu,
        ipc_mode=ipc_mode,
    )

    container_overrides = {
        "name": container_name,
        "command": command,
        "environment": [{"name": k, "value": v} for k, v in (env_vars or {}).items()],
    }
    container_overrides = {k: v for k, v in container_overrides.items() if v}

    response = ecs_client.run_task(
        cluster=ecs_config.cluster,
        enableExecuteCommand=True,
        taskDefinition=task_definition_arn,
        capacityProviderStrategy=[
            {"capacityProvider": ecs_config.capacity_provider, "weight": 1}
        ],
        networkConfiguration=get_default_network_config(
            ec2_client, assign_public_ip=False
        ),
        overrides={"containerOverrides": [container_overrides]},
        tags=tags or [],
    )

    if not response["tasks"]:
        failures = response.get("failures", [])
        raise RuntimeError(f"Failed to start EC2 task: {failures}")

    task_arn = response["tasks"][0]["taskArn"]
    print(f"Started EC2 task: {task_arn}")

    if wait:
        _wait_for_task_and_logs(
            ecs_client,
            ecs_config.region,
            ecs_config.cluster,
            task_arn,
            task_definition_arn,
            container_name,
        )

    return task_arn, container_name, task_definition_arn


def run(
    docker_image_name: str,
    entrypoint: str,
    command: Union[str, List[str]],
    ecs_config: ECSConfig,
    env_vars: dict = None,
) -> str:
    """Mirror an image to ECR, then run it on EC2 and wait for completion.

    Convenience wrapper that combines :func:`mirror_image_to_ecr` and
    :func:`run_container_ec2` into a single call. The function blocks until the
    task finishes and raises on failure.

    Args:
        docker_image_name: Source Docker image to mirror and run (e.g.
            ``"supervisely/base-py-sdk-light:6.73.527"``).
        entrypoint: Container entrypoint string (split on whitespace).
        command: Container command as a string or list of strings.
        ecs_config: ECS/ECR configuration used for both mirroring and running.
        env_vars: Environment variables to inject into the container as
            ``{"KEY": "value"}`` pairs.

    Returns:
        The ARN of the completed EC2 task.

    Raises:
        RuntimeError: If mirroring or the EC2 task fails.
    """
    ecr_image = mirror_image_to_ecr(docker_image_name, ecs_config)
    return run_container_ec2(
        docker_image_name=ecr_image,
        entrypoint=entrypoint,
        command=command,
        ecs_config=ecs_config,
        env_vars=env_vars,
        wait=True,
    )
