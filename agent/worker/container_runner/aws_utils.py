import boto3
import os
import time
from dataclasses import dataclass
from typing import Dict, List, Tuple, Union


@dataclass
class ECSConfig:
    cluster: str
    capacity_provider: str
    task_definition: str
    ecr_host: str
    mirroring_image_task_definition: str
    region: str = "us-east-1"


def get_boto3_client(service: str, region: str):
    """Helper to create boto3 clients with consistent credentials."""
    return boto3.client(
        service,
        region_name=region,
        aws_access_key_id=os.environ["AWS_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["AWS_SECRET_ACCESS_KEY"],
    )


def get_default_network_config(ec2_client, assign_public_ip: bool = True) -> dict:
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
    """
    Parse a docker image name into ECR components.

    supervisely/base-py-sdk-light:6.73.527
      -> repository_name: supervisely/base-py-sdk-light
      -> image_tag: 6.73.527
      -> target_image: {ecr_host}/supervisely/base-py-sdk-light:6.73.527

    Returns (repository_name, image_tag, target_image)
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
    """Create ECR repository if it doesn't exist. Handles nested names like org/repo."""
    try:
        ecr_client.create_repository(repositoryName=repository_name)
        print(f"Created ECR repository: {repository_name}")
    except ecr_client.exceptions.RepositoryAlreadyExistsException:
        pass


def _image_exists_in_ecr(ecr_client, repository_name: str, image_tag: str) -> bool:
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
    """
    Extract CloudWatch log configuration for a container from a task definition.
    Returns dict with {log_group, log_stream_prefix, region} or None if not configured.
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
    """Stream new log events from a CloudWatch log stream since the last token."""
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
    """Wait for ECS task to finish, streaming CloudWatch logs when available."""
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
    """Fetch new log lines since last token. Returns (next_token, lines)."""
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
    """Yield log lines from a running ECS task until it stops."""
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
    """Run a container on Fargate (FARGATE launch type)."""
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
    """
    Ensure a Docker image is mirrored to ECR.
    Returns the ECR image URI.
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
    """Run a container using the EC2 capacity provider."""
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
    """Full pipeline: mirror image to ECR, create task def revision, run and wait."""
    ecr_image = mirror_image_to_ecr(docker_image_name, ecs_config)
    return run_container_ec2(
        docker_image_name=ecr_image,
        entrypoint=entrypoint,
        command=command,
        ecs_config=ecs_config,
        env_vars=env_vars,
        wait=True,
    )
