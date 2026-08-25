"""The watchdog's dying breath, and mid-run download visibility.

On 2026-08-25 a healthy 10 GB checkpoint download was killed as a wedge
(it reported nothing) and the job it belonged to read "running" on the
board for an hour after its runner died. Three behaviors keep that from
recurring: progress ticks stamp the watchdog, mid-run download bytes
ride the node view's detail onto the board, and a stall FAILS the
active job before the process dies.
"""

from __future__ import annotations

import pytest

pytest.importorskip("mechbench_compute")

from mechbench_runner import job_runner as jr  # noqa: E402
from mechbench_runner.config import Config  # noqa: E402


class RecordingApi:
    def __init__(self):
        self.progress: list[dict] = []
        self.failed: list[dict] = []

    def report_progress(self, job_id, num, den, *, unit=None, status=None,
                        node=None):
        self.progress.append({"job": job_id, "num": num, "den": den,
                              "node": node, "status": status})

    def fail_job(self, job_id, message, timeout=None):
        self.failed.append({"job": job_id, "message": message,
                            "timeout": timeout})


def _runner(monkeypatch):
    class StubControl:
        def __init__(self, _state, path=None):
            self.path = path or "/tmp/stub.sock"

        def start(self):
            return None

        def stop(self):
            return None

    monkeypatch.setattr(jr, "ControlServer", StubControl)
    config = Config(
        api_base_url="http://127.0.0.1:1", api_key="k",
        poll_interval_seconds=0.01, warm_model_id=None,
    )
    return jr.JobRunner(config)


class TestStallFailsTheJob:
    def test_active_job_is_failed_with_a_bounded_timeout(self, monkeypatch):
        runner = _runner(monkeypatch)
        api = RecordingApi()
        runner._active_job = "j_1"
        runner._active_api = api
        runner._announce_stall(908.0)
        assert len(api.failed) == 1
        f = api.failed[0]
        assert f["job"] == "j_1"
        assert "908s" in f["message"] and "watchdog" in f["message"]
        assert f["timeout"] is not None  # the dying breath must be bounded

    def test_no_active_job_means_nothing_to_fail(self, monkeypatch):
        runner = _runner(monkeypatch)
        api = RecordingApi()
        runner._active_api = api
        runner._announce_stall(908.0)
        assert api.failed == []

    def test_a_failing_fail_report_does_not_stop_the_death(self, monkeypatch):
        runner = _runner(monkeypatch)

        class Grumpy(RecordingApi):
            def fail_job(self, *a, **k):
                raise ConnectionError("network is the thing that is stuck")

        runner._active_job = "j_1"
        runner._active_api = Grumpy()
        runner._announce_stall(908.0)  # must not raise


class TestMidRunDownloadBytes:
    def test_bytes_ride_the_node_detail_once_a_node_is_active(self, monkeypatch):
        runner = _runner(monkeypatch)
        api = RecordingApi()
        runner._active_job = "j_1"
        runner._active_api = api
        runner._last_node = {"index": 1, "count": 2, "id": "read"}
        runner._last_scalar = (0, 2)
        runner._announce_download_bytes(3_200_000_000, 10_300_000_000)
        assert len(api.progress) == 1
        node = api.progress[0]["node"]
        assert node["index"] == 1 and node["count"] == 2
        assert "3.2" in node["detail"] and "10.3" in node["detail"]
        # the scalar stays the node scalar, not the byte count
        assert api.progress[0]["num"] == 0 and api.progress[0]["den"] == 2

    def test_before_any_node_the_preparing_checklist_owns_the_bytes(
        self, monkeypatch
    ):
        runner = _runner(monkeypatch)
        api = RecordingApi()
        runner._active_job = "j_1"
        runner._active_api = api
        runner._last_node = None
        steps = []
        monkeypatch.setattr(runner, "_report_step", steps.append)
        runner._announce_download_bytes(1, 100)
        assert api.progress == []  # no node detail PATCH
        assert steps and steps[0]["unit"] == "bytes"


class TestProgressStampsTheWatchdog:
    def test_every_tick_feeds_the_watchdog(self, monkeypatch):
        runner = _runner(monkeypatch)
        api = RecordingApi()
        stamps = []
        monkeypatch.setattr(runner._watchdog, "stamp", lambda: stamps.append(1))
        monkeypatch.setattr(runner._executor, "run",
                            lambda _s, on_progress=None, secrets=None: (
                                on_progress(1, 2), on_progress(2, 2),
                                {"protocol": "layer_ablation"})[-1])
        monkeypatch.setattr(runner._executor, "_model_loaded",
                            lambda *_a, **_k: None)
        monkeypatch.setattr(jr, "dump_canonical", lambda _p: b"\xa0")
        api.declare_preparing = lambda *a, **k: None
        api.report_preparing_step = lambda *a, **k: None
        api.complete_job_cbor = lambda *a, **k: None
        runner._handle(api, {
            "id": "j_1", "protocolKind": "layer_ablation",
            "spec": {"prompt": "hi", "modelId": "org/m@r"},
        })
        assert len(stamps) >= 2
