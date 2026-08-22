"""The live channel, runner side (task 000289).

Driven against a real WebSocket server rather than a mocked transport:
what is worth checking here is the handshake, the heartbeat reply,
command application and the de-duplication — none of which a stubbed
socket would exercise.
"""

from __future__ import annotations

import json
import queue
import threading

import pytest

from mechbench_runner.channel import PROTOCOL_VERSION, LiveChannel, channel_url
from mechbench_runner.config import Config
from mechbench_runner.control import RunnerState

websockets_sync = pytest.importorskip("websockets.sync.server")


class FakeApi:
    """A one-connection API that records what the runner said."""

    def __init__(self):
        self.received: queue.Queue[dict] = queue.Queue()
        self.connected = threading.Event()
        self._outbound: queue.Queue[dict] = queue.Queue()
        self._server = None
        self._thread = None
        self.port = 0
        self.auth_headers: list[str] = []

    def _handler(self, ws):
        self.auth_headers.append(ws.request.headers.get("authorization", ""))
        self.connected.set()
        stop = threading.Event()

        def pump():
            while not stop.is_set():
                try:
                    frame = self._outbound.get(timeout=0.1)
                except queue.Empty:
                    continue
                try:
                    ws.send(json.dumps(frame))
                except Exception:
                    return

        t = threading.Thread(target=pump, daemon=True)
        t.start()
        try:
            for raw in ws:
                self.received.put(json.loads(raw))
        except Exception:
            pass
        finally:
            stop.set()

    def start(self):
        self._server = websockets_sync.serve(self._handler, "127.0.0.1", 0)
        self.port = self._server.socket.getsockname()[1]
        self._thread = threading.Thread(
            target=self._server.serve_forever, daemon=True
        )
        self._thread.start()
        return self

    def stop(self):
        if self._server is not None:
            self._server.shutdown()

    def send(self, frame: dict):
        self._outbound.put(frame)

    def next(self, kind: str | None = None, timeout: float = 5.0) -> dict:
        """Next frame, optionally of a given type."""
        deadline = timeout
        while deadline > 0:
            step = min(0.5, deadline)
            try:
                frame = self.received.get(timeout=step)
            except queue.Empty:
                deadline -= step
                continue
            if kind is None or frame.get("type") == kind:
                return frame
        raise AssertionError(f"no {kind or 'frame'} arrived within {timeout}s")


def command(cmd_id: str, name: str) -> dict:
    return {"v": PROTOCOL_VERSION, "type": "command", "id": cmd_id, "name": name}


@pytest.fixture()
def api():
    server = FakeApi().start()
    yield server
    server.stop()


@pytest.fixture()
def channel(api):
    state = RunnerState(version="0.1.0", api_url=f"http://127.0.0.1:{api.port}")
    config = Config(
        api_base_url=f"http://127.0.0.1:{api.port}",
        api_key="mbk_test_secret",
        poll_interval_seconds=0.01,
        warm_model_id=None,
        from_stored_credentials=True,
    )
    ch = LiveChannel(config, state)
    ch.start()
    assert api.connected.wait(timeout=5), "the channel never dialled in"
    yield ch, state, api
    ch.stop()


class TestUrl:
    def test_https_becomes_wss(self):
        assert channel_url("https://api.mechbench.ai") == (
            "wss://api.mechbench.ai/runners/channel"
        )

    def test_http_becomes_ws(self):
        assert channel_url("http://localhost:3000/") == (
            "ws://localhost:3000/runners/channel"
        )


class TestHandshake:
    def test_presents_the_stored_key(self, channel):
        _, _, api = channel
        assert api.auth_headers[0] == "Bearer mbk_test_secret"

    def test_introduces_itself(self, channel):
        _, _, api = channel
        hello = api.next("hello")
        assert hello["v"] == PROTOCOL_VERSION
        assert "python" in hello["platform"]
        assert hello["hostname"]


class TestHeartbeat:
    def test_answers_a_ping(self, channel):
        _, _, api = channel
        api.next("hello")
        api.send({"v": PROTOCOL_VERSION, "type": "ping"})
        assert api.next("pong")["type"] == "pong"


class TestCommands:
    def test_pause_and_resume_reach_the_state(self, channel):
        _, state, api = channel
        api.next("hello")
        api.send(command("c1", "pause"))
        ack = api.next("ack")
        assert ack["ok"] is True
        assert ack["id"] == "c1"
        assert state.paused is True

        api.send(command("c2", "resume"))
        assert api.next("ack")["ok"] is True
        assert state.paused is False

    def test_the_ack_carries_the_new_state(self, channel):
        _, _, api = channel
        api.next("hello")
        api.send(command("c3", "status"))
        ack = api.next("ack")
        assert ack["state"]["runner_version"] == "0.1.0"

    def test_a_redelivered_command_is_acked_but_not_reapplied(self, channel):
        """The property the task asks for: a pause delivered twice is a pause.

        Checked against a command whose second application would be
        visible — resume between the two deliveries, and if the duplicate
        were applied the runner would end up paused again.
        """
        _, state, api = channel
        api.next("hello")
        api.send(command("dup", "pause"))
        assert api.next("ack")["ok"] is True
        assert state.paused is True

        state.resume()
        api.send(command("dup", "pause"))
        assert api.next("ack")["id"] == "dup"
        assert state.paused is False, "the duplicate was applied a second time"

    def test_an_unknown_command_is_refused_not_ignored(self, channel):
        _, _, api = channel
        api.next("hello")
        api.send(command("c4", "self-destruct"))
        ack = api.next("ack")
        assert ack["ok"] is False
        assert "self-destruct" in ack["error"]


class TestTelemetry:
    def test_state_events_are_forwarded(self, channel):
        _, state, api = channel
        api.next("hello")
        state.job_claimed("job_9", "layer_ablation", "google/gemma-4")
        frame = api.next("event")
        assert frame["event"] == "job.claimed"
        assert frame["data"]["id"] == "job_9"

    def test_forwarding_never_breaks_the_job_thread(self, channel):
        ch, state, api = channel
        api.next("hello")
        ch.stop()  # the channel is gone; the job thread must not care
        state.job_claimed("job_10", "layer_ablation", None)
        state.job_finished("job_10")
        assert state.snapshot()["completed"] == 1


class TestResilience:
    def test_a_channel_with_no_key_simply_does_not_start(self):
        state = RunnerState(version="0.1.0", api_url="http://127.0.0.1:1")
        config = Config(
            api_base_url="http://127.0.0.1:1",
            api_key=None,
            poll_interval_seconds=0.01,
            warm_model_id=None,
        )
        ch = LiveChannel(config, state)
        ch.start()
        assert ch.connected is False
        ch.stop()

    def test_an_unreachable_api_is_not_fatal(self):
        # Port 1 refuses instantly. The channel must keep retrying in its
        # own thread and never raise into the caller.
        state = RunnerState(version="0.1.0", api_url="http://127.0.0.1:1")
        config = Config(
            api_base_url="http://127.0.0.1:1",
            api_key="mbk_x",
            poll_interval_seconds=0.01,
            warm_model_id=None,
        )
        ch = LiveChannel(config, state)
        ch.start()
        state.job_claimed("job_11", "layer_ablation", None)  # must not raise
        assert ch.connected is False
        ch.stop()
