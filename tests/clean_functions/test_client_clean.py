import os
import shutil
from logging import getLogger
from agent.worker import agent_utils

from conftest import run_session


logger = getLogger()


def test_remove_finished(
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
    cleaner.clean_all_app_data()

    # results
    # clean finished
    assert os.listdir(sly_agent_path / "app_sessions") == [str(runned_session)]

    # PIP cache unrouched
    assert len(os.listdir(storage / "apps_pip_cache")) == 2

    # logs cache unrouched
    assert len(os.listdir(sly_agent_path / "logs")) == 2

    # all tags removed
    assert os.path.exists(storage / "apps")
    assert not os.path.exists(storage / "apps" / "github.com" / "supervisely-ecosystem")


def test_remove_pip_cache(
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
    cleaner.clean_pip_cache()

    # results
    # sessions untouched
    assert len(os.listdir(sly_agent_path / "app_sessions")) == 2

    # all pip cache removed
    assert len(os.listdir(storage / "apps_pip_cache")) == 0

    # logs untouched
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
    session_wo_info, _, _ = run_session(sly_agent_path, sly_files_path)
    shutil.rmtree(str(sly_agent_path / "app_sessions" / str(session_wo_info)))

    # test body
    cleaner = agent_utils.AppDirCleaner(logger)
    cleaner.clean_all_app_data()

    # results
    # apps not old enough
    assert os.listdir(sly_agent_path / "app_sessions") == [str(runned_session)]

    # session_wo_info - has no lihnked session in app_session
    assert os.listdir(sly_files_path) == [f"module_{runned_session}"]
