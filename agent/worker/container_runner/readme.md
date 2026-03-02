# AWS Container Runner
A thin wrapper around AWS ECS that lets you launch, monitor, and interact with Docker containers on EC2-backed ECS clusters using the same interface as a local Docker runner.

## How it works
The wrapper is built around three layers:
1. aws_utils.py — Low-level AWS primitives
Stateless functions that talk directly to the AWS APIs:

mirror_image_to_ecr — Checks whether a public Docker image already exists in your ECR registry. If not, it launches a short-lived Fargate task that pulls the image and pushes it to ECR. This ensures all images are served from within your AWS account, avoiding external registry rate limits and improving pull latency.
run_container_ec2 — Clones a base ECS task definition, applies overrides (image, entrypoint, CPU, memory, GPU, environment variables), registers a new revision, and starts the task using your EC2 capacity provider.
stream_task_logs — Tails CloudWatch Logs for a running task, yielding lines as they arrive.

2. aws_container_runner.py — High-level container interface
Two classes that implement the BaseContainer / BaseContainerRunner interface:
AWSContainerRunner is the entry point. It reads cluster configuration from a JSON file and exposes two methods:

prepare_image(image) — Mirror an image to ECR before running it.
spawn_container(image, ...) — Launch a container and return an AWSContainer handle.

AWSContainer is the handle returned by spawn_container. It wraps a single ECS task and provides:
MethodDescriptionis_running()Returns True if the task is in RUNNING state.is_alive()Returns True if the task has not yet stopped or begun deprovisioning.wait(timeout, condition)Blocks until the task stops; returns {"StatusCode": exit_code}.stop()Sends a stop request to the task (async).remove(force)Optionally stops the task; present for interface compatibility.stream_container_logs()Yields CloudWatch log lines until the task stops.get_exit_code()Returns the container exit code, or None if still running.exec(command)Runs a shell command inside the container via ECS Execute Command.exec_kill(exec_id)Kills a background command started by exec.
AWSContainerExec is returned by AWSContainer.exec. It opens an SSM WebSocket session and exposes:

stream_logs() — Yields output lines from the remote command.
get_exit_code() — Returns the command's exit code after the stream ends.

## Configuration
AWS config file
AWSContainerRunner reads its cluster configuration from a JSON file. The default path is aws_config.json in the same directory as aws_container_runner.py. Override it with the AWS_CONFIG_PATH environment variable.
json{
  "region": "us-east-1",
  "cluster": "my-ecs-cluster",
  "capacity_provider": "my-ec2-capacity-provider",
  "task_definition": "my-base-task-def",
  "ecr_host": "123456789012.dkr.ecr.us-east-1.amazonaws.com",
  "mirroring_image_task_definition": "my-mirror-task-def"
}
KeyDescriptionregionAWS region. Defaults to us-east-1 if omitted.clusterECS cluster name or ARN.capacity_providerEC2 capacity provider used for all container tasks.task_definitionBase task definition cloned for each container run.ecr_hostECR registry host (the part before the first /).mirroring_image_task_definitionTask definition used by the Fargate image-mirroring task.
AWS credentials
Credentials are read from the standard environment variables:
AWS_ACCESS_KEY_ID=...
AWS_SECRET_ACCESS_KEY=...

## Demo

1. Create a run.env file (optional)
Add any environment variables you want injected into the container, one per line:
# run.env
MY_VAR=hello
ANOTHER_VAR=world

2. Set required environment variables
bashexport AWS_ACCESS_KEY_ID=...
export AWS_SECRET_ACCESS_KEY=...

# Required: the image to run
export CONTAINER_IMAGE=ubuntu:22.04

# Optional: override the image's default entrypoint
export CONTAINER_ENTRYPOINT="bash -c 'echo hello && sleep 5'"

3. Run
bashpython demo.py