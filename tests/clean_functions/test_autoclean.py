import os
import shutil
from logging import getLogger
from agent.worker import agent_utils

from conftest import run_session


logger = getLogger()


def test_remove_old(
    runned_session,
    stoped_session,
    sly_files_path,
    sly_agent_path,
    mocked_paths,
    monkeypatch,
):
    # setup
    def mock_age(*args, **kwargs):
        return 0

    storage = sly_agent_path / "storage"
    monkeypatch.setattr(agent_utils.os.path, "getatime", mock_age)
    monkeypatch.setattr(agent_utils.os.path, "getmtime", mock_age)

    # test body
    cleaner = agent_utils.AppDirCleaner(logger)
    working_sessions = [runned_session]
    cleaner.auto_clean(working_sessions)

    # results
    # clean finished
    assert os.listdir(sly_agent_path / "app_sessions") == [str(runned_session)]

    # clean cache (it's old enough)
    assert len(os.listdir(storage / "apps_pip_cache")) == 0

    # clean logs (it's old enough)
    assert len(os.listdir(sly_agent_path / "logs")) == 0

    # tags untouched
    assert len(os.listdir(storage / "apps" / "github.com" / "supervisely-ecosystem")) == 2


def test_remove_crashed(
    runned_session,
    stoped_session,
    sly_files_path,
    sly_agent_path,
    mocked_paths,
    monkeypatch,
):
    # setup
    def mock_age(*args, **kwargs):
        return 0

    monkeypatch.setattr(agent_utils.os.path, "getatime", mock_age)
    monkeypatch.setattr(agent_utils.os.path, "getmtime", mock_age)

    storage = sly_agent_path / "storage"

    # test body
    cleaner = agent_utils.AppDirCleaner(logger)
    cleaner.auto_clean(working_apps=[])

    # results
    # clean finished and crashed
    assert len(os.listdir(sly_agent_path / "app_sessions")) == 0

    # clean cache (it's old enough)
    assert len(os.listdir(storage / "apps_pip_cache")) == 0

    # clean logs (it's old enough)
    assert len(os.listdir(sly_agent_path / "logs")) == 0

    # tags untouched
    assert len(os.listdir(storage / "apps" / "github.com" / "supervisely-ecosystem")) == 2


def test_not_old_enough(
    runned_session,
    stoped_session,
    sly_files_path,
    sly_agent_path,
    mocked_paths,
):
    # setup
    storage = sly_agent_path / "storage"

    # test body
    cleaner = agent_utils.AppDirCleaner(logger)
    cleaner.auto_clean(working_apps=[])

    # results
    # apps not old enough
    assert len(os.listdir(sly_agent_path / "app_sessions")) == 2

    # cache untouched (it's not old enough)
    assert len(os.listdir(storage / "apps_pip_cache")) == 2

    # logs untouched (it's not old enough)
    assert len(os.listdir(sly_agent_path / "logs")) == 2

    # tags untouched
    assert len(os.listdir(storage / "apps" / "github.com" / "supervisely-ecosystem")) == 2


def test_clean_files_for_non_existing_session(
    runned_session,
    stoped_session,
    sly_files_path,
    sly_agent_path,
    mocked_paths,
):
    # setup
    session_wo_info, module, _ = run_session(sly_agent_path, sly_files_path)
    shutil.rmtree(str(sly_agent_path / "app_sessions" / str(session_wo_info)))

    # test body
    cleaner = agent_utils.AppDirCleaner(logger)
    cleaner.auto_clean(working_apps=[runned_session])

    # results
    # apps not old enough
    assert sorted(os.listdir(sly_agent_path / "app_sessions")) == sorted(
        [str(runned_session), str(stoped_session)]
    )

    # session_wo_info - has no lihnked session in app_session
    assert len(os.listdir(sly_files_path)) == 2
