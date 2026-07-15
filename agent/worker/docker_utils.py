# coding: utf-8
from __future__ import annotations

import base64
import json
import re
from enum import Enum
import time
from typing import Dict, List, Optional

from supervisely.app import DialogWindowError
from supervisely.task.progress import Progress

from worker import constants


PULL_RETRIES = 5
PULL_RETRY_DELAY = 5


class PullPolicy(Enum):
    def __str__(self):
        return str(self.value)

    ALWAYS = "Always".lower()
    IF_AVAILABLE = "IfAvailable".lower()
    IF_NOT_PRESENT = "IfNotPresent".lower()
    NEVER = "Never".lower()


class PullStatus(Enum):
    START = "Pulling fs layer"
    DOWNLOAD = "Downloading"
    EXTRACT = "Extracting"
    COMPLETE_LOAD = "Download complete"
    COMPLETE_PULL = "Pull complete"
    OTHER = "Other (unknown)"

    def is_equal(self, status: str) -> bool:
        return status == self.value

    @classmethod
    def from_str(cls, status: Optional[str]) -> PullStatus:
        dct = {
            "Pulling fs layer": PullStatus.START,
            "Downloading": PullStatus.DOWNLOAD,
            "Extracting": PullStatus.EXTRACT,
            "Download complete": PullStatus.COMPLETE_LOAD,
            "Pull complete": PullStatus.COMPLETE_PULL,
        }
        return dct.get(status, PullStatus.OTHER)


def _auths_from_env() -> Dict:
    doc_logs = constants.DOCKER_LOGIN().split(",")
    doc_pasws = constants.DOCKER_PASSWORD().split(",")
    doc_regs = constants.DOCKER_REGISTRY().split(",")
    auths = {}
    for login, pasw, reg in zip(doc_logs, doc_pasws, doc_regs):
        if reg == "" or (login == "" and pasw == ""):
            continue
        auths.update({reg: {"username": login, "password": pasw}})
    return auths


def _registry_auth_from_env(registry: str) -> Dict:
    auths = _auths_from_env()
    return auths.get(registry, None)


_ECR_REGISTRY_RE = re.compile(
    r"^\d{12}\.dkr\.ecr(?:-fips)?\.(?P<region>[a-z0-9-]+)\.(?:amazonaws\.com(?:\.cn)?|sc2s\.sgov\.gov|c2s\.ic\.gov)$"
)
# ECR tokens are valid for 12 hours; refresh a bit earlier to avoid using
# a token that expires mid-pull
_ECR_TOKEN_EXPIRATION_MARGIN = 15 * 60
_ecr_auth_cache = {}


def _registry_auth_from_aws(registry: str, logger) -> Optional[Dict]:
    match = _ECR_REGISTRY_RE.match(registry)
    if match is None:
        return None

    cached = _ecr_auth_cache.get(registry)
    if cached is not None and cached[0] > time.time():
        return cached[1]

    try:
        import boto3
    except ImportError:
        logger.warning(
            "Image is hosted on AWS ECR, but boto3 is not installed; "
            "AWS IAM authentication is not available",
            extra={"registry": registry},
        )
        return None

    try:
        ecr_client = boto3.client("ecr", region_name=match.group("region"))
        auth_data = ecr_client.get_authorization_token()["authorizationData"][0]
        username, password = (
            base64.b64decode(auth_data["authorizationToken"]).decode("utf-8").split(":", 1)
        )
        auth = {"username": username, "password": password}
        expires_at = auth_data["expiresAt"].timestamp() - _ECR_TOKEN_EXPIRATION_MARGIN
        _ecr_auth_cache[registry] = (expires_at, auth)
        logger.info(
            "Got ECR authorization token using AWS IAM credentials",
            extra={"registry": registry},
        )
        return auth
    except Exception as e:
        logger.warning(
            "Unable to get ECR authorization token using AWS IAM credentials, "
            "falling back to the Docker config file. If the agent host uses an EC2 "
            "instance role, make sure the instance metadata service is reachable "
            "from containers (IMDSv2 hop limit >= 2), or provide AWS credentials "
            "to the agent container via environment variables or a mounted ~/.aws.",
            extra={"registry": registry, "error": str(e)},
        )
        return None


def resolve_auth(registry: str, logger) -> Optional[Dict]:
    """Resolve pull credentials for the registry.

    Priority: explicit DOCKER_LOGIN/DOCKER_PASSWORD/DOCKER_REGISTRY credentials,
    then AWS IAM credentials for ECR registries. Returns None when nothing
    matched, so that docker-py falls back to the Docker config file
    (~/.docker/config.json or $DOCKER_CONFIG) and credential helpers,
    the same way the docker CLI does.
    """
    return resolve_auth_candidates(registry, logger)[0]


def resolve_auth_candidates(registry: str, logger) -> List[Optional[Dict]]:
    auth = _registry_auth_from_env(registry)
    if auth is not None:
        return [auth]

    auth = _registry_auth_from_aws(registry, logger)
    if auth is not None:
        return [auth, None]
    return [None]


def _run_with_auth_fallback(operation, auth_candidates, logger):
    from docker.errors import DockerException

    try:
        return operation(auth_candidates[0])
    except DockerException as e:
        if len(auth_candidates) == 1:
            raise
        logger.warning(
            "Docker registry request with AWS IAM credentials failed, "
            "falling back to the Docker config file",
            extra={"error": str(e)},
        )
        return operation(auth_candidates[1])


def _iter_with_auth_fallback(operation, auth_candidates, logger):
    from docker.errors import DockerException

    try:
        yield from operation(auth_candidates[0])
    except DockerException as e:
        if len(auth_candidates) == 1:
            raise
        logger.warning(
            "Docker registry request with AWS IAM credentials failed, "
            "falling back to the Docker config file",
            extra={"error": str(e)},
        )
        yield from operation(auth_candidates[1])


def docker_pull_if_needed(docker_api, docker_image_name, policy, logger, progress=True):
    logger.info(
        "docker_pull_if_needed args",
        extra={
            "policy": policy,
            "type(policy)": type(policy),
            "policy == PullPolicy.ALWAYS": str(policy) == str(PullPolicy.ALWAYS),
            "policy == PullPolicy.NEVER": str(policy) == str(PullPolicy.NEVER),
            "policy == PullPolicy.IF_NOT_PRESENT": str(policy) == str(PullPolicy.IF_NOT_PRESENT),
            "policy == PullPolicy.IF_AVAILABLE": str(policy) == str(PullPolicy.IF_AVAILABLE),
        },
    )
    if str(policy) == str(PullPolicy.ALWAYS):
        if progress is False:
            _docker_pull(docker_api, docker_image_name, logger)
        else:
            _docker_pull_progress(docker_api, docker_image_name, logger)
    elif str(policy) == str(PullPolicy.NEVER):
        pass
    elif str(policy) == str(PullPolicy.IF_NOT_PRESENT):
        if not _docker_image_exists(docker_api, docker_image_name):
            if progress is False:
                _docker_pull(docker_api, docker_image_name, logger)
            else:
                _docker_pull_progress(docker_api, docker_image_name, logger)
    elif str(policy) == str(PullPolicy.IF_AVAILABLE):
        if progress is False:
            _docker_pull(
                docker_api,
                docker_image_name,
                logger,
                raise_exception=True,
            )
        else:
            _docker_pull_progress(
                docker_api,
                docker_image_name,
                logger,
                raise_exception=True,
            )
    else:
        raise RuntimeError(f"Unknown pull policy {str(policy)}")
    if not _docker_image_exists(docker_api, docker_image_name):
        raise DialogWindowError(
            title=f"Docker image {docker_image_name} not found. Agent's PULL_POLICY is {str(policy)}.",
            description=(
                "The initiation of the pulling process was either prevented due to the pull policy settings "
                "or it was halted mid-way because the host lacks sufficient disk space."
            ),
        )


def resolve_registry(docker_image_name):
    from docker.utils import parse_repository_tag
    from docker.auth import resolve_repository_name

    try:
        repository, _ = parse_repository_tag(docker_image_name)
        registry, _ = resolve_repository_name(repository)
        return registry
    except Exception:
        return None


def _docker_pull(docker_api, docker_image_name, logger, raise_exception=True):
    from docker.errors import DockerException

    logger.info("Docker image will be pulled", extra={"image_name": docker_image_name})
    registry = resolve_registry(docker_image_name)
    auth_candidates = resolve_auth_candidates(registry, logger)
    logger.debug(
        "Docker registry auth",
        extra={"registry": registry, "auth": _hide_credentials(auth_candidates[0])},
    )
    for i in range(0, PULL_RETRIES + 1):
        retry_str = f" (retry {i}/{PULL_RETRIES})" if i > 0 else ""
        progress_dummy = Progress(
            "Pulling image..." + retry_str,
            1,
            ext_logger=logger,
        )
        progress_dummy.iter_done_report()
        try:
            attempt_auth_candidates = auth_candidates if i == 0 else auth_candidates[:1]
            pulled_img = _run_with_auth_fallback(
                lambda auth: docker_api.images.pull(docker_image_name, auth_config=auth),
                attempt_auth_candidates,
                logger,
            )
            logger.info(
                "Docker image has been pulled",
                extra={"pulled": {"tags": pulled_img.tags, "id": pulled_img.id}},
            )
            return
        except DockerException as e:
            if i >= PULL_RETRIES:
                if raise_exception is True:
                    raise e
                    # raise DockerException(
                    #     "Unable to pull image: see actual error above. "
                    #     "Please, run the task again or contact support team."
                    # )
                else:
                    logger.warn(
                        "Pulling step is skipped. Unable to pull image: {!r}.".format(str(e))
                    )
                    return
            logger.warning("Unable to pull image: %s", str(e))
            logger.info("Retrying in %d seconds...", PULL_RETRY_DELAY)
            time.sleep(PULL_RETRY_DELAY)


def _docker_pull_progress(docker_api, docker_image_name, logger, raise_exception=True):
    logger.info("Docker image will be pulled", extra={"image_name": docker_image_name})
    from docker.errors import DockerException

    registry = resolve_registry(docker_image_name)
    auth_candidates = resolve_auth_candidates(registry, logger)
    logger.debug(
        "Docker registry auth",
        extra={"registry": registry, "auth": _hide_credentials(auth_candidates[0])},
    )
    for i in range(0, PULL_RETRIES + 1):
        try:
            layers_total_load = {}
            layers_current_load = {}
            layers_total_extract = {}
            layers_current_extract = {}
            started = set()
            loaded = set()
            pulled = set()

            retry_str = f" (retry {i}/{PULL_RETRIES})" if i > 0 else ""

            progress_full = Progress(
                "Preparing dockerimage" + retry_str,
                1,
                ext_logger=logger,
            )
            progres_ext = Progress(
                "Extracting layers" + retry_str,
                1,
                is_size=True,
                ext_logger=logger,
            )
            progress_load = Progress(
                "Downloading layers" + retry_str,
                1,
                is_size=True,
                ext_logger=logger,
            )

            attempt_auth_candidates = auth_candidates if i == 0 else auth_candidates[:1]
            lines = _iter_with_auth_fallback(
                lambda auth: docker_api.api.pull(
                    docker_image_name, stream=True, decode=True, auth_config=auth
                ),
                attempt_auth_candidates,
                logger,
            )
            for line in lines:
                status = PullStatus.from_str(line.get("status", None))
                layer_id = line.get("id", None)
                progress_details = line.get("progressDetail", {})
                need_report = True

                if status is PullStatus.START:
                    started.add(layer_id)
                    need_report = False
                elif status is PullStatus.DOWNLOAD:
                    layers_current_load[layer_id] = progress_details.get("current", 0)
                    layers_total_load[layer_id] = progress_details.get(
                        "total", layers_current_load[layer_id]
                    )
                    total_load = sum(layers_total_load.values())
                    current_load = sum(layers_current_load.values())
                    if total_load > progress_load.total:
                        progress_load.set(current_load, total_load)
                    elif (current_load - progress_load.current) / total_load > 0.01:
                        progress_load.set(current_load, total_load)
                    else:
                        need_report = False
                elif status is PullStatus.COMPLETE_LOAD:
                    loaded.add(layer_id)
                elif status is PullStatus.EXTRACT:
                    layers_current_extract[layer_id] = progress_details.get("current", 0)
                    layers_total_extract[layer_id] = progress_details.get(
                        "total", layers_current_extract[layer_id]
                    )
                    total_ext = sum(layers_total_extract.values())
                    current_ext = sum(layers_current_extract.values())
                    if total_ext > progres_ext.total:
                        progres_ext.set(current_ext, total_ext)
                    elif (current_ext - progres_ext.current) / total_ext > 0.01:
                        progres_ext.set(current_ext, total_ext)
                    else:
                        need_report = False
                elif status is PullStatus.COMPLETE_PULL:
                    pulled.add(layer_id)

                if started != pulled:
                    if need_report:
                        if started == loaded:
                            progres_ext.report_progress()
                        else:
                            progress_load.report_progress()
                elif len(pulled) > 0:
                    progress_full.report_progress()

            progress_full.iter_done()
            progress_full.report_progress()
            logger.info("Docker image has been pulled", extra={"image_name": docker_image_name})
            return
        except DockerException as e:
            if i >= PULL_RETRIES:
                if raise_exception is True:
                    raise e
                    # raise DockerException(
                    #     "Unable to pull image: see actual error above. "
                    #     "Please, run the task again or contact support team."
                    # )
                else:
                    logger.warn(
                        "Pulling step is skipped. Unable to pull image: {!r}.".format(repr(e))
                    )
                    return
            logger.warning("Unable to pull image: %s", str(e))
            logger.info("Retrying in %d seconds...", PULL_RETRY_DELAY)
            time.sleep(PULL_RETRY_DELAY)


def _docker_image_exists(docker_api, docker_image_name):
    from docker.errors import ImageNotFound

    try:
        docker_img = docker_api.images.get(docker_image_name)
    except ImageNotFound:
        return False
    return True


def _hide_credentials(auth: Optional[Dict]) -> Optional[Dict]:
    if auth is None:
        return None

    def mask(value):
        if not value:
            return value
        return value[0] + ("*" * (len(value) - 2))[:10] + value[-1]

    return {"username": mask(auth.get("username")), "password": mask(auth.get("password"))}


def hidden_auth():
    auths = _auths_from_env()
    for registry, auth in auths.items():
        auths[registry] = _hide_credentials(auth)
    return auths
