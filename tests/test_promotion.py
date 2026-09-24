from __future__ import annotations

import pytest

pytest.importorskip("mechbench_compute")

from mechbench_runner import job_runner as jr  # noqa: E402
from mechbench_runner.api_client import ApiError  # noqa: E402
from mechbench_runner.config import Config  # noqa: E402


class RecordingApi:
    def __init__(self, fail_first: bool = False):
        self.progress: list[dict] = []
        self.fail_first = fail_first
        self.completed = False

    def report_progress(self, job_id, num, den, *, unit=None, status=None,
                        node=None):
        if self.fail_first and not self.progress:
            self.progress.append({"num": num, "status": status, "failed": True})
            raise ApiError(503, "unavailable")
        self.progress.append({"num": num, "den": den, "unit": unit,
                              "status": status, "node": node})

    def declare_preparing(self, *_a, **_k):
        return None

    def report_preparing_step(self, *_a, **_k):
        return None

    def complete_job_cbor(self, *_a, **_k):
        self.completed = True


class StubControl:
    def __init__(self, _state, path=None):
        self.path = path or "/tmp/stub.sock"

    def start(self):
        return None

    def stop(self):
        return None


def build(monkeypatch, api, ticks):
    monkeypatch.setattr(jr, "ControlServer", StubControl)
    config = Config(
        api_base_url="http://localhost:3000",
        api_key="mbk_test",
        poll_interval_seconds=0.01,
        warm_model_id=None,
    )
    runner = jr.JobRunner(config)

    def fake_run(_spec, *, on_progress=None, secrets=None):
        for i in ticks:
            on_progress(i, max(ticks))
        return {"protocol": "layer_ablation"}

    monkeypatch.setattr(runner._executor, "run", fake_run)
    monkeypatch.setattr(runner._executor, "_model_loaded", lambda *_a, **_k: None)
    monkeypatch.setattr(jr, "dump_canonical", lambda _p: b"\xa0")
    job = {
        "id": "job_1",
        "protocolKind": "layer_ablation",
        "spec": {"prompt": "hi", "modelId": "google/gemma-4@abc"},
    }
    runner._handle(api, job)
    return runner


def test_first_report_promotes_to_running(monkeypatch):
    api = RecordingApi()
    build(monkeypatch, api, ticks=[1, 2, 3, 4, 5])
    assert api.progress, "no progress was reported at all"
    assert api.progress[0]["status"] == "running"


def test_promotion_is_not_throttled_away(monkeypatch):
    api = RecordingApi()
    build(monkeypatch, api, ticks=[1, 2, 3, 4, 5, 6, 7, 8, 9, 10])
    assert api.progress[0]["num"] == 1
    assert api.progress[0]["status"] == "running"


def test_status_is_sent_once_not_on_every_tick(monkeypatch):
    api = RecordingApi()
    build(monkeypatch, api, ticks=list(range(1, 21)))
    promotions = [p for p in api.progress if p.get("status") == "running"]
    assert len(promotions) == 1


def test_a_failed_first_report_leaves_the_promotion_owed(monkeypatch):
    api = RecordingApi(fail_first=True)
    build(monkeypatch, api, ticks=[1, 2, 3, 4, 5])
    assert api.progress[0]["status"] == "running"
    assert api.progress[1]["status"] == "running"


def test_the_job_still_completes(monkeypatch):
    api = RecordingApi()
    build(monkeypatch, api, ticks=[1, 2])
    assert api.completed is True


def test_a_node_boundary_defeats_the_throttle(monkeypatch):
    api = RecordingApi()
    monkeypatch.setattr(jr, "ControlServer", StubControl)
    config = Config(
        api_base_url="http://localhost:3000", api_key="mbk_test",
        poll_interval_seconds=0.01, warm_model_id=None,
    )
    runner = jr.JobRunner(config)

    def fake_run(_spec, *, on_progress=None, secrets=None):
        on_progress(5, 8, {"index": 1, "count": 2, "id": "a", "done": 5, "total": 5})
        on_progress(6, 8, {"index": 1, "count": 2, "id": "a", "done": 6, "total": 6})
        on_progress(7, 8, {"index": 2, "count": 2, "id": "b", "done": 1, "total": 2})
        on_progress(8, 8, {"index": 2, "count": 2, "id": "b", "done": 2, "total": 2})
        return {"protocol": "layer_ablation"}

    monkeypatch.setattr(runner._executor, "run", fake_run)
    monkeypatch.setattr(runner._executor, "_model_loaded", lambda *_a, **_k: None)
    monkeypatch.setattr(jr, "dump_canonical", lambda _p: b"\xa0")
    runner._handle(api, {
        "id": "job_1", "protocolKind": "pipeline",
        "spec": {"prompt": "", "modelId": "google/gemma-4@abc"},
    })
    nums = [p["num"] for p in api.progress]
    assert 7 in nums
    assert 6 not in nums
    assert api.progress[-1]["node"]["index"] == 2
