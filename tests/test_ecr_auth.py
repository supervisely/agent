import os
from unittest.mock import Mock, call

from docker.errors import DockerException

os.environ.setdefault("ACCESS_TOKEN", "test-token")

from worker import constants  # noqa: F401 - initializes worker imports in the expected order
from worker import docker_utils


ECR_REGISTRY = "123456789012.dkr.ecr.us-east-1.amazonaws.com"
ECR_AUTH = {"username": "AWS", "password": "iam-token"}


class _Progress:
    def __init__(self, *args, **kwargs):
        pass

    def iter_done_report(self):
        pass

    def iter_done(self):
        pass

    def report_progress(self):
        pass


def test_resolve_auth_candidates_does_not_fallback_from_explicit_credentials(monkeypatch):
    explicit_auth = {"username": "user", "password": "password"}
    monkeypatch.setattr(docker_utils, "_registry_auth_from_env", lambda registry: explicit_auth)
    aws_auth = Mock()
    monkeypatch.setattr(docker_utils, "_registry_auth_from_aws", aws_auth)

    candidates = docker_utils.resolve_auth_candidates(ECR_REGISTRY, Mock())

    assert candidates == [explicit_auth]
    aws_auth.assert_not_called()


def test_resolve_auth_candidates_adds_docker_config_after_aws(monkeypatch):
    monkeypatch.setattr(docker_utils, "_registry_auth_from_env", lambda registry: None)
    monkeypatch.setattr(
        docker_utils, "_registry_auth_from_aws", lambda registry, logger: ECR_AUTH
    )

    candidates = docker_utils.resolve_auth_candidates(ECR_REGISTRY, Mock())

    assert candidates == [ECR_AUTH, None]


def test_docker_pull_falls_back_from_aws_to_docker_config(monkeypatch):
    monkeypatch.setattr(docker_utils, "Progress", _Progress)
    monkeypatch.setattr(docker_utils, "resolve_registry", lambda image: ECR_REGISTRY)
    monkeypatch.setattr(
        docker_utils, "resolve_auth_candidates", lambda registry, logger: [ECR_AUTH, None]
    )
    pulled_image = Mock(tags=["repo:tag"], id="sha256:image")
    docker_api = Mock()
    docker_api.images.pull.side_effect = [DockerException("IAM denied"), pulled_image]

    docker_utils._docker_pull(docker_api, "repo:tag", Mock())

    assert docker_api.images.pull.call_args_list == [
        call("repo:tag", auth_config=ECR_AUTH),
        call("repo:tag", auth_config=None),
    ]


def test_docker_pull_progress_falls_back_from_aws_to_docker_config(monkeypatch):
    monkeypatch.setattr(docker_utils, "Progress", _Progress)
    monkeypatch.setattr(docker_utils, "resolve_registry", lambda image: ECR_REGISTRY)
    monkeypatch.setattr(
        docker_utils, "resolve_auth_candidates", lambda registry, logger: [ECR_AUTH, None]
    )
    docker_api = Mock()

    def failed_stream():
        yield from ()
        raise DockerException("IAM denied")

    docker_api.api.pull.side_effect = [failed_stream(), iter(())]

    docker_utils._docker_pull_progress(docker_api, "repo:tag", Mock())

    assert docker_api.api.pull.call_args_list == [
        call("repo:tag", stream=True, decode=True, auth_config=ECR_AUTH),
        call("repo:tag", stream=True, decode=True, auth_config=None),
    ]


def test_docker_pull_retries_aws_after_failed_docker_config_fallback(monkeypatch):
    monkeypatch.setattr(docker_utils, "Progress", _Progress)
    monkeypatch.setattr(docker_utils, "resolve_registry", lambda image: ECR_REGISTRY)
    monkeypatch.setattr(
        docker_utils, "resolve_auth_candidates", lambda registry, logger: [ECR_AUTH, None]
    )
    monkeypatch.setattr(docker_utils.time, "sleep", lambda delay: None)
    pulled_image = Mock(tags=["repo:tag"], id="sha256:image")
    docker_api = Mock()
    docker_api.images.pull.side_effect = [
        DockerException("IAM denied"),
        DockerException("Docker config denied"),
        pulled_image,
    ]

    docker_utils._docker_pull(docker_api, "repo:tag", Mock())

    assert docker_api.images.pull.call_args_list == [
        call("repo:tag", auth_config=ECR_AUTH),
        call("repo:tag", auth_config=None),
        call("repo:tag", auth_config=ECR_AUTH),
    ]


def test_registry_data_falls_back_from_aws_to_docker_config(monkeypatch):
    from worker import task_update

    monkeypatch.setattr(docker_utils, "resolve_registry", lambda image: ECR_REGISTRY)
    monkeypatch.setattr(
        docker_utils, "resolve_auth_candidates", lambda registry, logger: [ECR_AUTH, None]
    )
    registry_data = Mock(id="sha256:image")
    image_collection = Mock()
    image_collection.get_registry_data.side_effect = [
        DockerException("IAM denied"),
        registry_data,
    ]
    monkeypatch.setattr(task_update, "ImageCollection", lambda docker_client: image_collection)
    container = Mock()
    container.attrs = {"Config": {"Image": "repo:tag"}}
    container.image.attrs = {"RepoDigests": ["repo@sha256:image"]}

    updated = task_update.check_and_pull_sly_net_if_needed(Mock(), container, Mock())

    assert updated is False
    assert image_collection.get_registry_data.call_args_list == [
        call("repo:tag", auth_config=ECR_AUTH),
        call("repo:tag", auth_config=None),
    ]
