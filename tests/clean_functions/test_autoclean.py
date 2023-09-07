import os
from logging import getLogger
from agent.worker import agent_utils


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
