# coding: utf-8

import os
from datetime import timedelta
from urllib.parse import urlparse
import supervisely_lib as sly
import hashlib
import re
from worker.docker_utils import PullPolicy


_SERVER_ADDRESS = "SERVER_ADDRESS"
_ACCESS_TOKEN = "ACCESS_TOKEN"
_DOCKER_LOGIN = "DOCKER_LOGIN"
_DOCKER_PASSWORD = "DOCKER_PASSWORD"
_DOCKER_REGISTRY = "DOCKER_REGISTRY"


_VALUES = {
    _DOCKER_LOGIN: os.environ.get(_DOCKER_LOGIN, ""),
    _DOCKER_PASSWORD: os.environ.get(_DOCKER_PASSWORD, ""),
    _DOCKER_REGISTRY: os.environ.get(_DOCKER_REGISTRY, ""),
}


def TOKEN():
    return os.environ[_ACCESS_TOKEN]


_AGENT_HOST_DIR = "AGENT_HOST_DIR"
_WITH_LOCAL_STORAGE = "WITH_LOCAL_STORAGE"
_UPLOAD_RESULT_IMAGES = "UPLOAD_RESULT_IMAGES"
_PULL_ALWAYS = "PULL_ALWAYS"
_DEFAULT_TIMEOUTS = "DEFAULT_TIMEOUTS"
_DELETE_TASK_DIR_ON_FINISH = "DELETE_TASK_DIR_ON_FINISH"
_DELETE_TASK_DIR_ON_FAILURE = "DELETE_TASK_DIR_ON_FAILURE"
_CHECK_VERSION_COMPATIBILITY = "CHECK_VERSION_COMPATIBILITY"
_DOCKER_API_CALL_TIMEOUT = "DOCKER_API_CALL_TIMEOUT"
_HTTP_PROXY = "HTTP_PROXY"
_HTTPS_PROXY = "HTTPS_PROXY"
_NO_PROXY = "NO_PROXY"
_PUBLIC_API_RETRY_LIMIT = "PUBLIC_API_RETRY_LIMIT"
_APP_DEBUG_DOCKER_IMAGE = "APP_DEBUG_DOCKER_IMAGE"
_SLY_APPS_DOCKER_REGISTRY = "SLY_APPS_DOCKER_REGISTRY"
_SLY_EXTRA_CA_CERTS_DIR = "SLY_EXTRA_CA_CERTS_DIR"
_SLY_EXTRA_CA_CERTS_VOLUME_NAME = "SLY_EXTRA_CA_CERTS_VOLUME_NAME"

_REQUESTS_CA_BUNDLE = "REQUESTS_CA_BUNDLE"
_REQUESTS_CA_BUNDLE_DIR_CONTAINER = "REQUESTS_CA_BUNDLE_DIR_CONTAINER"
_HOST_REQUESTS_CA_BUNDLE = "HOST_REQUESTS_CA_BUNDLE"
_SSL_CERT_FILE = "SSL_CERT_FILE"
_SLY_EXTRA_CA_CERTS = "SLY_EXTRA_CA_CERTS"

# container limits
_CPU_LIMIT = "CPU_LIMIT"
_MEM_LIMIT = "MEM_LIMIT"
_SHM_SIZE = "SHM_SIZE"

# security settings (only for custom environments)
_SECURITY_OPT = "SECURITY_OPT"

_PULL_POLICY = "PULL_POLICY"

_GIT_LOGIN = "GIT_LOGIN"
_GIT_PASSWORD = "GIT_PASSWORD"
_GITHUB_TOKEN = "GITHUB_TOKEN"
_AGENT_ROOT_DIR = "AGENT_ROOT_DIR"
_DISABLE_TELEMETRY = "DISABLE_TELEMETRY"
_DEFAULT_APP_DOCKER_IMAGE = "DEFAULT_APP_DOCKER_IMAGE"
_AGENT_FILES_IN_APP_CONTAINER = "AGENT_FILES_IN_APP_CONTAINER"
_AUTO_CLEAN_INT_RANGE_DAYS = "AUTO_CLEAN_INT_RANGE_DAYS"
_BASE_IMAGES = ["supervisely/base-py-sdk:latest", "supervisely/data-versioning:latest"]
_MAX_AGENT_RESTARTS = "MAX_AGENT_RESTARTS"
_AGENT_RESTART_COUNT = "AGENT_RESTART_COUNT"


_REQUIRED_SETTINGS = [
    _SERVER_ADDRESS,
    _ACCESS_TOKEN,
]


_PULL_POLICY_DICT = {
    str(PullPolicy.ALWAYS): PullPolicy.ALWAYS,
    str(PullPolicy.IF_AVAILABLE): PullPolicy.IF_AVAILABLE,
    str(PullPolicy.IF_NOT_PRESENT): PullPolicy.IF_NOT_PRESENT,
    str(PullPolicy.NEVER): PullPolicy.NEVER,
}

_DOCKER_NET = "DOCKER_NET"
_SUPERVISELY_AGENT_FILES = "SUPERVISELY_AGENT_FILES"
_SUPERVISELY_AGENT_FILES_CONTAINER = "SUPERVISELY_AGENT_FILES_CONTAINER"
_OFFLINE_MODE = "OFFLINE_MODE"
_CROSS_AGENT_VOLUME_NAME = "CROSS_AGENT_VOLUME_NAME"
_CROSS_AGENT_DATA_DIR = "CROSS_AGENT_DATA_DIR"
_REMOVE_IDLE_DOCKER_IMAGE_AFTER_X_DAYS = "REMOVE_IDLE_DOCKER_IMAGE_AFTER_X_DAYS"
_REMOVE_OLD_AGENT = "REMOVE_OLD_AGENT"
_UPDATE_SLY_NET_AFTER_RESTART = "UPDATE_SLY_NET_AFTER_RESTART"
_DOCKER_IMAGE = "DOCKER_IMAGE"
_CONTAINER_NAME = "CONTAINER_NAME"
_FORCE_CPU_ONLY = "FORCE_CPU_ONLY"
_LOG_LEVEL = "LOG_LEVEL"
_APPS_CACHE_DIR = "APPS_CACHE_DIR"

_NET_CLIENT_DOCKER_IMAGE = "NET_CLIENT_DOCKER_IMAGE"
_NET_SERVER_PORT = "NET_SERVER_PORT"
_SLY_NET_CLIENT_PING_INTERVAL = "SLY_NET_CLIENT_PING_INTERVAL"
_TRUST_DOWNSTREAM_PROXY = "TRUST_DOWNSTREAM_PROXY"
_NET_CLIENT_CONTAINER_NAME = "NET_CLIENT_CONTAINER_NAME"
_NET_CLIENT_NETWORK = "NET_CLIENT_NETWORK"

_OPTIONAL_DEFAULTS = {
    _AGENT_HOST_DIR: f"/opt/supervisely/agents/agent-data-{TOKEN()[:8]}",
    _WITH_LOCAL_STORAGE: "true",
    _UPLOAD_RESULT_IMAGES: "true",
    _PULL_ALWAYS: None,
    _DEFAULT_TIMEOUTS: "true",
    _DELETE_TASK_DIR_ON_FINISH: "true",
    _DELETE_TASK_DIR_ON_FAILURE: "false",
    _CHECK_VERSION_COMPATIBILITY: "false",
    _DOCKER_API_CALL_TIMEOUT: "60",
    _HTTP_PROXY: "",
    _HTTPS_PROXY: "",
    _NO_PROXY: "",
    _PUBLIC_API_RETRY_LIMIT: 100,
    _CPU_LIMIT: None,
    _MEM_LIMIT: None,
    _PULL_POLICY: str(PullPolicy.IF_AVAILABLE),  # str(PullPolicy.NEVER),
    _GIT_LOGIN: None,
    _GIT_PASSWORD: None,
    _GITHUB_TOKEN: None,
    _APP_DEBUG_DOCKER_IMAGE: None,
    _SLY_APPS_DOCKER_REGISTRY: None,
    _REQUESTS_CA_BUNDLE: None,
    _SSL_CERT_FILE: None,
    _HOST_REQUESTS_CA_BUNDLE: None,
    _SHM_SIZE: "5G",
    _DOCKER_NET: None,
    _AGENT_ROOT_DIR: "/sly_agent",
    _DISABLE_TELEMETRY: None,
    _SUPERVISELY_AGENT_FILES: f"/opt/supervisely/agents/agent-files-{TOKEN()[:8]}",
    _SUPERVISELY_AGENT_FILES_CONTAINER: "/app/sly-files",
    _OFFLINE_MODE: False,
    _DEFAULT_APP_DOCKER_IMAGE: "supervisely/base-py-sdk",
    _AGENT_FILES_IN_APP_CONTAINER: "/agent-storage",
    _AUTO_CLEAN_INT_RANGE_DAYS: 7,
    _REQUESTS_CA_BUNDLE_DIR_CONTAINER: "/sly_certs",
    _SECURITY_OPT: None,
    _NET_CLIENT_DOCKER_IMAGE: "supervisely/sly-net-client:latest",
    _CROSS_AGENT_VOLUME_NAME: "supervisely-cross-agents-data",
    _CROSS_AGENT_DATA_DIR: "/cross-agent-data",
    _REMOVE_IDLE_DOCKER_IMAGE_AFTER_X_DAYS: 14,
    _SLY_EXTRA_CA_CERTS: None,
    _SLY_NET_CLIENT_PING_INTERVAL: 60,
    _TRUST_DOWNSTREAM_PROXY: "true",
    _REMOVE_OLD_AGENT: None,
    _UPDATE_SLY_NET_AFTER_RESTART: "false",
    _DOCKER_IMAGE: None,
    _NET_SERVER_PORT: None,
    _NET_CLIENT_CONTAINER_NAME: f"supervisely-net-client-{TOKEN()[:8]}",
    _NET_CLIENT_NETWORK: f"supervisely-net-{TOKEN()[:8]}",
    _CONTAINER_NAME: f"supervisely-agent-{TOKEN()[:8]}",
    _MAX_AGENT_RESTARTS: 3,
    _AGENT_RESTART_COUNT: 0,
    _SLY_EXTRA_CA_CERTS_DIR: "/sly_certs",
    _SLY_EXTRA_CA_CERTS_VOLUME_NAME: f"supervisely-agent-ca-certs-{TOKEN()[:8]}",
    _FORCE_CPU_ONLY: "false",
    _LOG_LEVEL: "INFO",
    _APPS_CACHE_DIR: "/apps_cache",
}


SENSITIVE_SETTINGS = [_ACCESS_TOKEN, _DOCKER_PASSWORD, _GIT_PASSWORD]


def get_required_settings():
    return _REQUIRED_SETTINGS.copy()


def get_optional_defaults():
    return _OPTIONAL_DEFAULTS.copy()


def read_optional_setting(name):
    return os.getenv(name, _OPTIONAL_DEFAULTS[name])


def HOST_DIR():
    """{agent root host dir}; can be a named volume; default (named volume): 'supervisely-agent-###'"""
    return read_optional_setting(_AGENT_HOST_DIR)


def AGENT_ROOT_DIR():
    """{agent root dir}; default '/sly_agent'"""
    return read_optional_setting(_AGENT_ROOT_DIR)


def _agent_to_host_path(local_path):
    return os.path.join(HOST_DIR(), os.path.relpath(local_path, start=AGENT_ROOT_DIR()))


def SERVER_ADDRESS():
    str_url = os.environ[_SERVER_ADDRESS]
    if ("http://" not in str_url) and ("https://" not in str_url):
        str_url = os.path.join("http://", str_url)  # @TODO: raise with error
    parsed_uri = urlparse(str_url)
    server_address = "{uri.scheme}://{uri.netloc}/".format(uri=parsed_uri)
    return server_address


def PUBLIC_API_SERVER_ADDRESS():
    return SERVER_ADDRESS() + "public/api/v3/"


def TASKS_DOCKER_LABEL():
    return "supervisely_{}".format(hashlib.sha256(TOKEN().encode("utf-8")).hexdigest())


def TASKS_DOCKER_LABEL_LEGACY():
    return "supervisely_{}".format(TOKEN())


def DOCKER_LOGIN():
    return _VALUES[_DOCKER_LOGIN]


def DOCKER_PASSWORD():
    return _VALUES[_DOCKER_PASSWORD]


def DOCKER_REGISTRY():
    return _VALUES[_DOCKER_REGISTRY]


def AGENT_TASKS_DIR_HOST():
    """default:  '~/.supervisely-agent/tasks"""
    return os.path.join(HOST_DIR(), "tasks")


def AGENT_TASK_SHARED_DIR_HOST():
    return _agent_to_host_path(AGENT_TASK_SHARED_DIR())


def DELETE_TASK_DIR_ON_FINISH():
    return sly.env.flag_from_env(read_optional_setting(_DELETE_TASK_DIR_ON_FINISH))


def DELETE_TASK_DIR_ON_FAILURE():
    return sly.env.flag_from_env(read_optional_setting(_DELETE_TASK_DIR_ON_FAILURE))


def DOCKER_API_CALL_TIMEOUT():
    return int(read_optional_setting(_DOCKER_API_CALL_TIMEOUT))


def AGENT_LOG_DIR():
    """default: /sly_agent/logs"""
    return os.path.join(AGENT_ROOT_DIR(), "logs")


def AGENT_TASKS_DIR():
    """default: /sly_agent/tasks"""
    return os.path.join(AGENT_ROOT_DIR(), "tasks")


def AGENT_TASK_SHARED_DIR():
    """default: /sly_agent/tasks/task_shared"""
    return os.path.join(AGENT_TASKS_DIR(), sly.task.paths.TASK_SHARED)  # pylint: disable=no-member


def AGENT_TMP_DIR():
    """default: /sly_agent/tmp"""
    return os.path.join(AGENT_ROOT_DIR(), "tmp")


def CROSS_AGENT_DATA_DIR():
    """default: /cross-agent-data"""
    return read_optional_setting(_CROSS_AGENT_DATA_DIR)


def CROSS_AGENT_VOLUME_NAME():
    """default: none, usually supervisely_agents_volume"""
    return read_optional_setting(_CROSS_AGENT_VOLUME_NAME)


def AGENT_IMPORT_DIR():
    """default: /sly_agent/import"""
    return os.path.join(AGENT_ROOT_DIR(), "import")


def AGENT_STORAGE_DIR():
    """default: /sly_agent/storage"""
    return os.path.join(AGENT_ROOT_DIR(), "storage")


def WITH_LOCAL_STORAGE():
    return sly.env.flag_from_env(read_optional_setting(_WITH_LOCAL_STORAGE))


def UPLOAD_RESULT_IMAGES():
    return sly.env.flag_from_env(read_optional_setting(_UPLOAD_RESULT_IMAGES))


def PULL_ALWAYS():
    val = read_optional_setting(_PULL_ALWAYS)
    if val is not None:
        sly.logger.warn(
            "ENV variable PULL_ALWAYS is deprecated and will be ignored."
            " Use PULL_POLICY instead with one of the following values: {}".format(
                list(_PULL_POLICY_DICT.keys())
            )
        )
    return True


def CHECK_VERSION_COMPATIBILITY():
    return sly.env.flag_from_env(read_optional_setting(_CHECK_VERSION_COMPATIBILITY))


def TIMEOUT_CONFIG_PATH():
    use_default_timeouts = sly.env.flag_from_env(read_optional_setting(_DEFAULT_TIMEOUTS))
    return None if use_default_timeouts else "/workdir/src/configs/timeouts_for_stateless.json"


def AUTO_CLEAN_INT_RANGE_DAYS():
    return int(read_optional_setting(_AUTO_CLEAN_INT_RANGE_DAYS))


def AUTO_CLEAN_TIMEDELTA_DAYS() -> timedelta:
    return timedelta(days=AUTO_CLEAN_INT_RANGE_DAYS())


def NETW_CHUNK_SIZE():
    return 1048576


def BATCH_SIZE_GET_IMAGES_INFO():
    return 100


def BATCH_SIZE_DOWNLOAD_IMAGES():
    return 20


def BATCH_SIZE_DOWNLOAD_ANNOTATIONS():
    return 1000


def BATCH_SIZE_UPLOAD_IMAGES():
    return 1000


def BATCH_SIZE_UPLOAD_ANNOTATIONS():
    return 1000


def BATCH_SIZE_ADD_IMAGES():
    return 1000


def BATCH_SIZE_LOG():
    return 100


def HTTP_PROXY():
    return read_optional_setting(_HTTP_PROXY)


def HTTPS_PROXY():
    return read_optional_setting(_HTTPS_PROXY)


def NO_PROXY():
    return read_optional_setting(_NO_PROXY)


def PUBLIC_API_RETRY_LIMIT():
    return int(read_optional_setting(_PUBLIC_API_RETRY_LIMIT))


def CPU_LIMIT():
    val = read_optional_setting(_CPU_LIMIT)
    if val is None or val.strip() == "":
        return None
    else:
        return int(val)


def MEM_LIMIT():
    val = read_optional_setting(_MEM_LIMIT)
    return val


def PULL_POLICY():
    val = read_optional_setting(_PULL_POLICY).lower()
    if val not in _PULL_POLICY_DICT:
        raise RuntimeError(
            "Unknown pull policy {!r}. Supported values: {}".format(
                val, list(_PULL_POLICY_DICT.keys())
            )
        )
    else:
        return _PULL_POLICY_DICT[val]


def GIT_LOGIN():
    return read_optional_setting(_GIT_LOGIN)


def GIT_PASSWORD():
    return read_optional_setting(_GIT_PASSWORD)


# def AGENT_APP_SOURCES_DIR():
#     return os.path.join(AGENT_ROOT_DIR(), 'app_sources')
#
#
# def AGENT_APP_SOURCES_DIR_HOST():
#     return os.path.join(HOST_DIR(), 'app_sources')


def AGENT_APP_SESSIONS_DIR():
    """default: /sly_agent/app_sessions"""
    return os.path.join(AGENT_ROOT_DIR(), "app_sessions")


def AGENT_APP_SESSIONS_DIR_HOST():
    """default: ~/.supervisely-agent/app_sessions"""
    return os.path.join(HOST_DIR(), "app_sessions")


def AGENT_APPS_CACHE_DIR_HOST():
    """default: ~/.supervisely-agent/apps_cache"""
    return os.path.join(HOST_DIR(), "apps_cache")


def AGENT_APPS_CACHE_DIR():
    """default: /sly_agent/apps_cache"""
    return os.path.join(AGENT_ROOT_DIR(), "apps_cache")


def APPS_CACHE_DIR():
    """
    Is used to access AGENT_APPS_CACHE_DIR from the app container.
    default: /apps_cache
    """
    return read_optional_setting(_APPS_CACHE_DIR)


def GITHUB_TOKEN():
    return read_optional_setting(_GITHUB_TOKEN)


def APPS_STORAGE_DIR():
    """default: /sly_agent/storage/apps"""
    return os.path.join(AGENT_STORAGE_DIR(), "apps")


def APPS_PIP_CACHE_DIR():
    """default: /sly_agent/storage/apps_pip_cache"""
    return os.path.join(AGENT_STORAGE_DIR(), "apps_pip_cache")


def APP_DEBUG_DOCKER_IMAGE():
    return read_optional_setting(_APP_DEBUG_DOCKER_IMAGE)


def REQUESTS_CA_BUNDLE():
    """Certs file in Agent container"""
    return read_optional_setting(_REQUESTS_CA_BUNDLE)


def REQUESTS_CA_BUNDLE_DIR():
    """DIR where REQUESTS_CA_BUNDLE stored"""
    if REQUESTS_CA_BUNDLE() is not None:
        return os.path.dirname(REQUESTS_CA_BUNDLE())
    return None


def REQUESTS_CA_BUNDLE_DIR_CONTAINER():
    """DIR where REQUESTS_CA_BUNDLE stored in App container"""
    return read_optional_setting(_REQUESTS_CA_BUNDLE_DIR_CONTAINER)


def REQUESTS_CA_BUNDLE_CONTAINER():
    """Certs file in App container"""
    if REQUESTS_CA_BUNDLE() is not None:
        filename = sly.fs.get_file_name_with_ext(REQUESTS_CA_BUNDLE())
        return os.path.join(REQUESTS_CA_BUNDLE_DIR_CONTAINER(), filename)
    return None


def MOUNTED_REQUESTS_CA_BUNDLE_DIR():
    """Certs file path in mounted volume inside Agent container."""
    return os.path.join(AGENT_ROOT_DIR(), "certs")


def MOUNTED_HOST_REQUESTS_CA_BUNDLE():
    """Certs file path in mounted volume on Host."""
    return os.path.join(HOST_DIR(), "certs")


def HOST_REQUESTS_CA_BUNDLE():
    return read_optional_setting(_HOST_REQUESTS_CA_BUNDLE)


def SSL_CERT_FILE():
    return read_optional_setting(_SSL_CERT_FILE)


def SHM_SIZE():
    return read_optional_setting(_SHM_SIZE)


def DOCKER_NET():
    return read_optional_setting(_NET_CLIENT_NETWORK)


def DISABLE_TELEMETRY():
    return read_optional_setting(_DISABLE_TELEMETRY)


def REMOVE_IDLE_DOCKER_IMAGE_AFTER_X_DAYS():
    return read_optional_setting(_REMOVE_IDLE_DOCKER_IMAGE_AFTER_X_DAYS)


def AGENT_ID():
    try:
        host_dir = SUPERVISELY_AGENT_FILES()
        if host_dir is None:
            return None
        if "supervisely/agent-" in host_dir:
            search = re.search("supervisely/agent-(\d+)(.*)", host_dir)
            agent_id = int(search.group(1))
            return agent_id
    except Exception as e:
        return None


def SUPERVISELY_AGENT_FILES():
    # /root/supervisely/agent-17 (host) -> /app/sly-files (net-client)
    # /root/supervisely/agent-17 (host) -> /app/sly-files (agent container)
    """{agent files host dir}; default `supervisely-agent-###`"""
    return read_optional_setting(_SUPERVISELY_AGENT_FILES)


def SUPERVISELY_AGENT_FILES_CONTAINER():
    """{agent files dir}; default /app/sly-files"""
    host_dir = SUPERVISELY_AGENT_FILES()
    if host_dir is None:
        return None
    agent_storage_dir_in_agent_container = read_optional_setting(_SUPERVISELY_AGENT_FILES_CONTAINER)
    return agent_storage_dir_in_agent_container


def AGENT_FILES_IN_APP_CONTAINER():
    """/agent-storage"""
    host_dir = SUPERVISELY_AGENT_FILES()
    if host_dir is None:
        return None
    res = read_optional_setting(_AGENT_FILES_IN_APP_CONTAINER)
    return res


def SUPERVISELY_SYNCED_APP_DATA():
    """default: ~/supervisely/agent-###/app_data"""
    agent_storage_dir = SUPERVISELY_AGENT_FILES()
    if agent_storage_dir is None:
        return None
    return os.path.join(agent_storage_dir, "app_data")


def SUPERVISELY_SYNCED_APP_DATA_CONTAINER():
    """default: /app/sly-files/app_data"""
    dir_in_container = SUPERVISELY_AGENT_FILES_CONTAINER()
    if dir_in_container is None:
        return None
    return os.path.join(dir_in_container, "app_data")


def OFFLINE_MODE():
    if read_optional_setting(_OFFLINE_MODE) == "true":
        return True

    return False


def DEFAULT_APP_DOCKER_IMAGE():
    return read_optional_setting(_DEFAULT_APP_DOCKER_IMAGE)


def SECURITY_OPT():
    value = read_optional_setting(_SECURITY_OPT)
    if value is not None:
        value = list(filter(len, value.split(",")))
        if len(value) > 0:
            return value
    return None


def SLY_APPS_DOCKER_REGISTRY():
    value = read_optional_setting(_SLY_APPS_DOCKER_REGISTRY)
    if value == "":
        return None

    return value


def NET_CLIENT_DOCKER_IMAGE():
    """defaul: supervisely/sly-net-client:latest"""
    return read_optional_setting(_NET_CLIENT_DOCKER_IMAGE)


def NET_SERVER_PORT():
    return read_optional_setting(_NET_SERVER_PORT)


def SLY_EXTRA_CA_CERTS():
    return read_optional_setting(_SLY_EXTRA_CA_CERTS)


def SLY_NET_CLIENT_PING_INTERVAL():
    return read_optional_setting(_SLY_NET_CLIENT_PING_INTERVAL)


def TRUST_DOWNSTREAM_PROXY():
    return read_optional_setting(_TRUST_DOWNSTREAM_PROXY)


def REMOVE_OLD_AGENT():
    return read_optional_setting(_REMOVE_OLD_AGENT)


def UPDATE_SLY_NET_AFTER_RESTART():
    return sly.env.flag_from_env(read_optional_setting(_UPDATE_SLY_NET_AFTER_RESTART))


def DOCKER_IMAGE():
    return read_optional_setting(_DOCKER_IMAGE)


def NET_CLIENT_CONTAINER_NAME():
    return read_optional_setting(_NET_CLIENT_CONTAINER_NAME)


def NET_CLIENT_NETWORK():
    return read_optional_setting(_NET_CLIENT_NETWORK)


def CONTAINER_NAME():
    return read_optional_setting(_CONTAINER_NAME)


def BASE_IMAGES():
    return _BASE_IMAGES


def MAX_AGENT_RESTARTS():
    return int(read_optional_setting(_MAX_AGENT_RESTARTS))


def AGENT_RESTART_COUNT():
    return int(read_optional_setting(_AGENT_RESTART_COUNT))


def SLY_EXTRA_CA_CERTS_DIR():
    return read_optional_setting(_SLY_EXTRA_CA_CERTS_DIR)


def SLY_EXTRA_CA_CERTS_FILEPATH():
    return os.path.join(SLY_EXTRA_CA_CERTS_DIR(), "instance_ca_chain.crt")


def SLY_EXTRA_CA_CERTS_BUNDLE_FILEPATH():
    return os.path.join(SLY_EXTRA_CA_CERTS_DIR(), "bundle.crt")


def SLY_EXTRA_CA_CERTS_VOLUME_NAME():
    return read_optional_setting(_SLY_EXTRA_CA_CERTS_VOLUME_NAME)


def FORCE_CPU_ONLY():
    return sly.env.flag_from_env(read_optional_setting(_FORCE_CPU_ONLY))


def LOG_LEVEL():
    return read_optional_setting(_LOG_LEVEL)


def init_constants():
    sly.fs.mkdir(AGENT_LOG_DIR())
    sly.fs.mkdir(AGENT_TASKS_DIR())
    sly.fs.mkdir(AGENT_TASK_SHARED_DIR())
    os.chmod(AGENT_TASK_SHARED_DIR(), 0o777)  # octal
    sly.fs.mkdir(AGENT_STORAGE_DIR())
    sly.fs.mkdir(AGENT_TMP_DIR())
    sly.fs.mkdir(AGENT_IMPORT_DIR())
    os.chmod(AGENT_IMPORT_DIR(), 0o777)  # octal
    PULL_ALWAYS()
    # sly.fs.mkdir(AGENT_APP_SOURCES_DIR())
    sly.fs.mkdir(AGENT_APP_SESSIONS_DIR())
    sly.fs.mkdir(APPS_STORAGE_DIR())
    sly.fs.mkdir(APPS_PIP_CACHE_DIR())
    if SUPERVISELY_AGENT_FILES_CONTAINER() is not None:
        sly.fs.mkdir(SUPERVISELY_AGENT_FILES_CONTAINER())
    if SUPERVISELY_SYNCED_APP_DATA_CONTAINER() is not None:
        sly.fs.mkdir(SUPERVISELY_SYNCED_APP_DATA_CONTAINER())

    if REQUESTS_CA_BUNDLE() is not None:
        # check if certs not in mounted folder
        if REQUESTS_CA_BUNDLE_DIR() != MOUNTED_REQUESTS_CA_BUNDLE_DIR():
            filename = sly.fs.get_file_name_with_ext(REQUESTS_CA_BUNDLE())
            sly.fs.mkdir(MOUNTED_REQUESTS_CA_BUNDLE_DIR())
            sly.fs.copy_file(
                REQUESTS_CA_BUNDLE(),
                os.path.join(MOUNTED_REQUESTS_CA_BUNDLE_DIR(), filename),
            )

    if CROSS_AGENT_DATA_DIR() is not None and not sly.fs.dir_exists(CROSS_AGENT_DATA_DIR()):
        sly.fs.mkdir(CROSS_AGENT_DATA_DIR())
