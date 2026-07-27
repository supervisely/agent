# coding: utf-8
"""Tests for Agent._run_daemon — the resilient daemon supervisor.

Verifies a crashing daemon is retried (restart=True) or safely run once (restart=False),
with sliding-window exponential backoff, and that it never propagates (which would kill the agent).
"""

import os
import threading
from unittest.mock import MagicMock

# worker.agent imports constants that read these envs at import time
os.environ.setdefault("ACCESS_TOKEN", "x")
os.environ.setdefault("SERVER_ADDRESS", "https://localhost")
os.environ.setdefault("DOCKER_REGISTRY", "x")
os.environ.setdefault("AGENT_HOST_DIR", "/tmp/agent")

from worker.agent import (  # noqa: E402
    Agent,
    DAEMON_RESTART_WAIT_SEC,
    DAEMON_RESTART_WAIT_MAX_SEC,
)


class FakeStop:
    """Stand-in for threading.Event that stops the loop after N backoff waits and
    records the wait timeouts (so we can assert the backoff schedule) without sleeping."""

    def __init__(self, stop_after_waits):
        self._set = False
        self._stop_after = stop_after_waits
        self.wait_timeouts = []

    def is_set(self):
        return self._set

    def set(self):
        self._set = True

    def wait(self, timeout):
        self.wait_timeouts.append(timeout)
        if len(self.wait_timeouts) >= self._stop_after:
            self._set = True
            return True
        return False


def _fake_agent(stop):
    fake = MagicMock()
    fake._stop_daemons = stop
    fake.logger = MagicMock()
    return fake


def test_restart_true_retries_until_stop():
    stop = FakeStop(stop_after_waits=3)
    fake = _fake_agent(stop)
    calls = {"n": 0}

    def target():
        calls["n"] += 1
        raise RuntimeError("boom")

    Agent._run_daemon(fake, target, "d", restart=True)

    assert calls["n"] == 3  # one attempt before each of the 3 backoff waits
    assert fake.logger.error.call_count == 3
    assert stop.wait_timeouts == [30, 60, 120]  # 30 * 2**(k-1)


def test_backoff_caps_at_max():
    stop = FakeStop(stop_after_waits=8)
    fake = _fake_agent(stop)

    def target():
        raise RuntimeError("boom")

    Agent._run_daemon(fake, target, "d", restart=True)

    waits = stop.wait_timeouts
    assert waits[0] == DAEMON_RESTART_WAIT_SEC
    assert waits == sorted(waits)  # non-decreasing
    assert waits[-1] == DAEMON_RESTART_WAIT_MAX_SEC  # capped


def test_restart_false_single_attempt_on_exception():
    stop = FakeStop(stop_after_waits=99)
    fake = _fake_agent(stop)
    calls = {"n": 0}

    def target():
        calls["n"] += 1
        raise RuntimeError("boom")

    Agent._run_daemon(fake, target, "d", restart=False)

    assert calls["n"] == 1  # run-once: no retry
    assert stop.wait_timeouts == []  # never reached backoff
    assert fake.logger.error.call_count == 1


def test_restart_false_clean_return():
    stop = FakeStop(stop_after_waits=99)
    fake = _fake_agent(stop)
    calls = {"n": 0}

    def target():
        calls["n"] += 1  # returns cleanly

    Agent._run_daemon(fake, target, "d", restart=False)

    assert calls["n"] == 1
    assert stop.wait_timeouts == []
    assert fake.logger.error.call_count == 0


def test_stop_set_before_start_never_runs():
    stop = threading.Event()
    stop.set()
    fake = _fake_agent(stop)
    calls = {"n": 0}

    def target():
        calls["n"] += 1

    Agent._run_daemon(fake, target, "d", restart=True)

    assert calls["n"] == 0  # loop guard sees stop immediately


def test_never_propagates():
    stop = FakeStop(stop_after_waits=1)
    fake = _fake_agent(stop)

    def target():
        raise KeyError("unexpected")

    # must not raise out of _run_daemon (that would crash the agent)
    Agent._run_daemon(fake, target, "d", restart=True)
