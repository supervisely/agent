# coding: utf-8
"""sly-net-client log forwarding must deliver each docker log line once.

Regression for supervisely/issues#6216: every reconnect of the log stream (net-client
restart, stream end) re-read the container's whole history and forwarded it again, and each
reconnect attached one more handler, multiplying every line sent to events.publish.
"""

import json
import os
import time
import uuid
from collections import Counter
from datetime import datetime, timezone

import pytest

# worker.agent imports constants that read these envs at import time
os.environ.setdefault("ACCESS_TOKEN", "x")
os.environ.setdefault("SERVER_ADDRESS", "https://localhost")
os.environ.setdefault("DOCKER_REGISTRY", "x")
os.environ.setdefault("AGENT_HOST_DIR", "/tmp/agent")

from worker import agent_utils, constants  # noqa: E402
from worker.agent import Agent  # noqa: E402
from worker.agent_utils import ContainerLogFollower, parse_docker_log_timestamp_ns  # noqa: E402

SEC = 10**9
T0 = 1_790_000_000 * SEC


def _docker_ts(ts_ns):
    # docker's RFC3339NanoFixed: always 9 fractional digits, UTC as "Z"
    base = datetime.fromtimestamp(ts_ns // SEC, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    return "{}.{:09d}Z".format(base, ts_ns % SEC)


class FakeContainer:
    """Mimics the docker log API: `since` in whole seconds is inclusive, output is chunked."""

    def __init__(self, chunk_size=7):
        self.entries = []  # (ts_ns, text)
        self.chunk_size = chunk_size
        self.since_calls = []

    def log(self, ts_ns, text):
        self.entries.append((ts_ns, text))

    def logs(self, stdout, stderr, follow, stream, timestamps, since):
        assert timestamps and stream and follow and isinstance(since, int) and since > 0
        self.since_calls.append(since)
        data = b"".join(
            "{} {}\n".format(_docker_ts(ts), text).encode("utf-8")
            for ts, text in self.entries
            if ts >= since * SEC
        )
        for i in range(0, len(data), self.chunk_size):
            yield data[i : i + self.chunk_size]


def _follow(follower, container):
    out = []
    follower.follow(container, out.append)
    return out


def test_history_before_start_is_not_forwarded():
    c = FakeContainer()
    c.log(T0 - 5 * SEC, "old boot")
    c.log(T0 - 1, "just before start")
    c.log(T0 + 10, "new")
    assert _follow(ContainerLogFollower(start_ns=T0), c) == ["new"]


def test_reconnects_deliver_every_line_exactly_once():
    c = FakeContainer()
    f = ContainerLogFollower(start_ns=T0)
    c.log(T0 + 100, "a")
    c.log(T0 + 200, "b")
    assert _follow(f, c) == ["a", "b"]

    # container restarted: new lines in the same second as the cursor, and after it
    c.log(T0 + 300, "c")
    c.log(T0 + 2 * SEC, "d")
    assert _follow(f, c) == ["c", "d"]

    # reconnect with nothing new must not replay anything
    assert _follow(f, c) == []
    c.log(T0 + 3 * SEC, "e")
    assert _follow(f, c) == ["e"]
    assert c.since_calls[-1] == (T0 + 2 * SEC) // SEC  # resumes near the cursor, not at 0


def test_lines_sharing_a_timestamp_across_reconnect():
    c = FakeContainer()
    f = ContainerLogFollower(start_ns=T0)
    ts = T0 + 500
    c.log(ts, "x1")
    c.log(ts, "x2")
    assert _follow(f, c) == ["x1", "x2"]
    c.log(ts, "x3")  # identical nanosecond timestamp, written after the stream closed
    c.log(ts + 1, "y")
    assert _follow(f, c) == ["x3", "y"]


def test_out_of_order_stderr_line_is_delivered_once():
    # stdout and stderr are stamped by separate goroutines: file order != timestamp order
    c = FakeContainer()
    f = ContainerLogFollower(start_ns=T0)
    c.log(T0 + 200, "registering VPN")
    c.log(T0 + 100, "curl: (22) 502")
    c.log(T0 + 300, "retrying")
    assert _follow(f, c) == ["registering VPN", "curl: (22) 502", "retrying"]
    # after a reconnect: the replayed prefix is skipped, then out-of-order lines are new again
    c.log(T0 + 400, "next")
    c.log(T0 + 350, "late stderr")
    assert _follow(f, c) == ["next", "late stderr"]
    assert _follow(f, c) == []


def test_multibyte_text_split_across_chunks():
    c = FakeContainer(chunk_size=3)
    c.log(T0 + 1, "подключение к VPN ✓")
    c.log(T0 + 2, "")
    c.log(T0 + 3, "next")
    assert _follow(ContainerLogFollower(start_ns=T0), c) == ["подключение к VPN ✓", "", "next"]


@pytest.mark.parametrize(
    "value, expected",
    [
        ("2026-09-24T03:45:33.123456789Z", 1790221533 * SEC + 123456789),
        ("2026-09-24T03:45:33.5Z", 1790221533 * SEC + 500000000),
        ("2026-09-24T03:45:33Z", 1790221533 * SEC),
        ("2026-09-24T06:45:33.000000001+03:00", 1790221533 * SEC + 1),
        ("not-a-timestamp", None),
    ],
)
def test_parse_docker_log_timestamp(value, expected):
    assert parse_docker_log_timestamp_ns(value) == expected


def _docker_client():
    docker = pytest.importorskip("docker")
    try:
        client = docker.from_env()
        client.ping()
        client.images.pull("busybox", tag="1.36")
    except Exception as exc:
        pytest.skip("docker daemon is not usable: {}".format(exc))
    return client


def test_restarting_net_client_lines_reach_log_queue_once(monkeypatch, tmp_path):
    """Real docker: a net-client that crashes every second, streamed by the agent's daemon body."""
    dc = _docker_client()
    name = "sly-net-client-test-" + uuid.uuid4().hex[:8]
    monkeypatch.setattr(constants, "NET_CLIENT_CONTAINER_NAME", lambda: name)
    monkeypatch.setattr(constants, "AGENT_LOG_DIR", lambda: str(tmp_path))
    container = dc.containers.run(
        "busybox:1.36",
        [
            "sh",
            "-c",
            "for i in 1 2 3; do echo line-$(cat /proc/sys/kernel/random/uuid); done; sleep 1; exit 1",
        ],
        name=name,
        detach=True,
        restart_policy={"Name": "always"},
    )
    try:
        time.sleep(3)
        history = container.logs().decode().splitlines()
        assert history, "container produced no history to (not) replay"

        agent = Agent.__new__(Agent)  # skip __init__: it connects to the server
        agent.docker_api = dc
        agent.log_queue = agent_utils.LogQueue()
        agent.net_logger = None
        agent._net_client_log_follower = ContainerLogFollower()

        # the body _run_daemon re-enters after each stream end, across several container restarts
        for _ in range(4):
            deadline = time.monotonic() + 30
            while container.reload() or container.status != "running":
                assert time.monotonic() < deadline, "net-client did not come back up"
                time.sleep(0.1)
            Agent.task_stream_net_client_logs(agent)

        container.stop(timeout=0)
        container.reload()
        assert container.attrs["RestartCount"] >= 3
        delivered = [json.loads(e)["message"] for e in agent.log_queue.q.queue]
        all_lines = container.logs().decode().splitlines()
        assert len(set(all_lines)) == len(all_lines)  # uuids: every line is distinct

        counts = Counter(delivered)
        assert [l for l, n in counts.items() if n > 1] == []
        assert set(history).isdisjoint(counts)
        new_lines = [l for l in all_lines if l not in history]
        # lines written after the last reader returned were never streamed; everything else was
        assert new_lines[: len(delivered)] == delivered
        assert len(delivered) >= 9
    finally:
        container.remove(force=True)
