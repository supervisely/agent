# coding: utf-8
from __future__ import annotations

import json
from enum import Enum
import time
from typing import Dict, Optional

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
        auths.update({reg: {"username": login, "password": pasw}})
    return auths


def _registry_auth_from_env(registry: str) -> Dict:
    auths = _auths_from_env()
    return auths.get(registry, None)


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
    auth = _registry_auth_from_env(registry)
    auth_log = hidden_auth().get(registry, None)
    logger.debug("Docker registry auth", extra={"registry": registry, "auth": auth_log})
    for i in range(0, PULL_RETRIES + 1):
        retry_str = f" (retry {i}/{PULL_RETRIES})" if i > 0 else ""
        progress_dummy = Progress(
            "Pulling image..." + retry_str,
            1,
            ext_logger=logger,
        )
        progress_dummy.iter_done_report()
        try:

            pulled_img = docker_api.images.pull(docker_image_name, auth_config=auth)
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
    auth = _registry_auth_from_env(registry)
    auth_log = hidden_auth().get(registry, None)
    logger.debug("Docker registry auth", extra={"registry": registry, "auth": auth_log})
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

            for line in docker_api.api.pull(
                docker_image_name, stream=True, decode=True, auth_config=auth
            ):
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


def hidden_auth():
    auths = _auths_from_env()
    for registry, auth in auths.items():
        username = auth.get("username")
        if username:
            username = username[0] + ("*" * (len(username) - 2))[:10] + username[-1]
        password = auth.get("password")
        if password:
            password = password[0] + ("*" * (len(password) - 2))[:10] + password[-1]
        auths[registry] = {"username": username, "password": password}
    return auths
