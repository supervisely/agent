from typing import Tuple
import pytest
import os
from pathlib import Path

from agent.worker import agent_utils


@pytest.fixture(scope="function")
def tmp_path(tmp_path_factory) -> Path:
    return tmp_path_factory.mktemp("tmp")


@pytest.fixture()
def sly_files_path(tmp_path) -> Path:
    """Temporary path for sly-files data"""
    tmp_dir: Path = tmp_path / "app" / "sly-files" / "app_data/"
    tmp_dir.mkdir(exist_ok=True, parents=True)
    return tmp_dir


@pytest.fixture()
def sly_agent_path(tmp_path) -> Path:
    """Temporary path for sly-files data"""
    tmp_dir: Path = tmp_path / "sly_agent"
    (tmp_dir / "app_sessions").mkdir(parents=True, exist_ok=True)
    storage = tmp_dir / "storage"
    (storage / "apps_pip_cache").mkdir(parents=True, exist_ok=True)
    (storage / "app").mkdir(parents=True, exist_ok=True)
    return tmp_dir


@pytest.fixture()
def runned_session(sly_agent_path: Path, sly_files_path: Path):
    return run_session(sly_agent_path, sly_files_path)[0]


@pytest.fixture()
def stoped_session(sly_agent_path: Path, sly_files_path: Path):
    task_id, _, _ = run_session(sly_agent_path, sly_files_path)
    app_session = sly_agent_path / "app_sessions" / str(task_id)
    agent_utils.TaskDirCleaner(str(app_session)).allow_cleaning()
    return task_id


@pytest.fixture()
def mocked_paths(sly_agent_path: Path, sly_files_path: Path, monkeypatch):
    storage = sly_agent_path / "storage"
    AGENT_APP_SESSIONS_DIR = lambda: str(sly_agent_path / "app_sessions")
    SUPERVISELY_SYNCED_APP_DATA_CONTAINER = lambda: str(sly_files_path)
    AGENT_LOG_DIR = lambda: str(sly_agent_path / "logs")
    APPS_PIP_CACHE_DIR = lambda: str(storage / "apps_pip_cache")
    APPS_STORAGE_DIR = lambda: str(storage / "apps")

    monkeypatch.setattr(agent_utils.constants, "AGENT_APP_SESSIONS_DIR", AGENT_APP_SESSIONS_DIR)
    monkeypatch.setattr(
        agent_utils.constants,
        "SUPERVISELY_SYNCED_APP_DATA_CONTAINER",
        SUPERVISELY_SYNCED_APP_DATA_CONTAINER,
    )
    monkeypatch.setattr(agent_utils.constants, "AGENT_LOG_DIR", AGENT_LOG_DIR)
    monkeypatch.setattr(agent_utils.constants, "APPS_PIP_CACHE_DIR", APPS_PIP_CACHE_DIR)
    monkeypatch.setattr(agent_utils.constants, "APPS_STORAGE_DIR", APPS_STORAGE_DIR)


def _generate_id(pth: Path) -> int:
    return len(os.listdir(pth)) + 1


def _module_name(sly_files_path: Path) -> Tuple[str, int]:
    ind = _generate_id(sly_files_path)
    return f"module_{ind}", ind


def _mkdir_and_touch(path: Path, filename: str = "randomfile.txt"):
    path.mkdir(parents=True, exist_ok=True)
    file = path / filename
    with open(file, "a"):
        os.utime(file, None)


def run_session(agent_path: Path, files_path: Path):
    task_id = _generate_id(agent_path / "app_sessions")
    module, module_id = _module_name(files_path)

    app_session = agent_path / "app_sessions" / str(task_id)
    app_logs = app_session / "logs"
    app_repo = app_session / "repo"
    _mkdir_and_touch(app_logs)
    _mkdir_and_touch(app_repo)
    agent_utils.TaskDirCleaner(str(app_session)).forbid_dir_cleaning()

    agent_logs = agent_path / "logs"
    _mkdir_and_touch(agent_logs, f"logs_{task_id}")

    storage = agent_path / "storage"
    pip_cache = storage / "apps_pip_cache"
    # module_id = _generate_id(pip_cache)
    module_pip_cache = pip_cache / str(module_id) / "v.1"
    _mkdir_and_touch(module_pip_cache)

    app_storage = (
        storage / "apps" / "github.com" / "supervisely-ecosystem" / module / str(module_id) / "v.1"
    )
    _mkdir_and_touch(app_storage)

    files = files_path / module / str(task_id) / "models"
    _mkdir_and_touch(files)

    return task_id, module, module_id
