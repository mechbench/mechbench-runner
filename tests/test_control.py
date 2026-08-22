"""The control socket (task 000283): protocol, state, events, staleness."""

from __future__ import annotations

import json
import socket
import tempfile
import threading
import time
from pathlib import Path

import pytest

from mechbench_runner.control import (
    PROTOCOL_VERSION,
    ControlError,
    ControlServer,
    RunnerState,
    probe,
    request,
)


@pytest.fixture()
def short_tmp():
    """A short directory: AF_UNIX paths are capped near 104 bytes, and
    pytest's tmp_path is already most of that."""
    d = tempfile.mkdtemp(prefix="mbr-", dir="/tmp")
    try:
        yield Path(d)
    finally:
        for f in Path(d).iterdir():
            f.unlink(missing_ok=True)
        Path(d).rmdir()


@pytest.fixture()
def served(short_tmp: Path):
    state = RunnerState(version="test", api_url="http://localhost:3000")
    server = ControlServer(state, path=short_tmp / "runner.sock")
    server.start()
    try:
        yield state, server
    finally:
        server.stop()


def test_status_reports_an_idle_runner(served):
    state, server = served
    data = request("status", path=server.path)
    assert data["phase"] == "starting"
    assert data["job"] is None
    assert data["runner_version"] == "test"
    assert data["pid"] > 0


def test_status_follows_the_job_through(served):
    state, server = served
    state.model_loaded("gemma@abc")
    state.job_claimed("j_1", "layer_ablation", "gemma@abc")
    state.job_progress(7, 42)

    data = request("status", path=server.path)
    assert data["phase"] == "executing"
    assert data["job"]["id"] == "j_1"
    assert data["job"]["protocol_kind"] == "layer_ablation"
    assert (data["job"]["done"], data["job"]["total"]) == (7, 42)
    assert data["job"]["elapsed_seconds"] >= 0
    assert data["model_id"] == "gemma@abc"

    state.job_finished("j_1")
    after = request("status", path=server.path)
    assert after["phase"] == "idle"
    assert after["job"] is None
    assert after["completed"] == 1


def test_pause_and_resume_are_visible_to_the_job_loop(served):
    state, server = served
    assert state.paused is False
    request("pause", path=server.path)
    assert state.paused is True          # the loop reads exactly this
    request("resume", path=server.path)
    assert state.paused is False


def test_a_version_mismatch_says_so(served):
    _, server = served
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(5)
    s.connect(str(server.path))
    s.sendall((json.dumps({"v": PROTOCOL_VERSION + 99, "op": "status"}) + "\n").encode())
    reply = json.loads(s.recv(65536))
    s.close()
    assert reply["ok"] is False
    assert reply["error"]["code"] == "version_mismatch"
    # The message has to name both sides, or it is useless in the field.
    assert str(PROTOCOL_VERSION) in reply["error"]["message"]


def test_unknown_ops_and_junk_do_not_kill_the_connection(served):
    _, server = served
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(5)
    s.connect(str(server.path))
    s.sendall(b"not json at all\n")
    assert json.loads(s.recv(65536))["error"]["code"] == "bad_json"
    s.sendall((json.dumps({"v": PROTOCOL_VERSION, "op": "fly"}) + "\n").encode())
    assert json.loads(s.recv(65536))["error"]["code"] == "unknown_op"
    # Still usable afterwards.
    s.sendall((json.dumps({"v": PROTOCOL_VERSION, "op": "status"}) + "\n").encode())
    assert json.loads(s.recv(65536))["ok"] is True
    s.close()


def test_subscribers_are_pushed_events(served):
    state, server = served
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(5)
    s.connect(str(server.path))
    s.sendall((json.dumps({"v": PROTOCOL_VERSION, "op": "subscribe"}) + "\n").encode())

    buf = b""
    def read_message():
        nonlocal buf
        while b"\n" not in buf:
            buf += s.recv(65536)
        line, buf = buf.split(b"\n", 1)
        return json.loads(line)

    assert read_message()["ok"] is True        # the snapshot that opens a stream

    # Emitted from this (non-server) thread, exactly as the job loop does.
    state.job_claimed("j_2", "layer_ablation", "gemma@abc")
    state.job_progress(1, 2)
    state.job_finished("j_2")

    events = [read_message() for _ in range(3)]
    s.close()
    assert [e["event"] for e in events] == ["job.claimed", "job.progress", "job.finished"]
    assert events[0]["data"]["id"] == "j_2"
    assert all(e["v"] == PROTOCOL_VERSION for e in events)


def test_probe_finds_a_live_runner_and_ignores_a_stale_socket(short_tmp: Path):
    state = RunnerState(version="test", api_url="http://x")
    server = ControlServer(state, path=short_tmp / "runner.sock")
    server.start()
    try:
        assert probe(server.path) is not None
    finally:
        server.stop()

    # A crashed runner leaves the file behind. It must not read as "running",
    # or a machine could never start a runner again after one crash.
    stale = short_tmp / "stale.sock"
    stale.touch()
    assert probe(stale) is None


def test_a_missing_socket_explains_itself(short_tmp: Path):
    with pytest.raises(ControlError) as exc:
        request("status", path=short_tmp / "nothing.sock")
    assert "no runner is listening" in str(exc.value)
