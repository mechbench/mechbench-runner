"""The result spool (epic 000320, task 000323 first half).

On 2026-09-07 a runner finished a job the server had already reaped,
was refused on upload (409), printed "failed to report failure" and
moved on — a complete, hash-verified result discarded. Three
behaviours make that impossible: the result is written to disk BEFORE
the upload is attempted; a refused or failed upload keeps the spool
and never fails the job; reconciliation delivers spooled results to
jobs that will still take them (interrupted, or our own orphans, which
are interrupted first) and clears those that ended otherwise.
"""

from __future__ import annotations

import pytest

pytest.importorskip("mechbench_compute")

from mechbench_runner import job_runner as jr  # noqa: E402
from mechbench_runner.config import Config  # noqa: E402
from mechbench_runner.paths import spool_dir  # noqa: E402


class RecordingApi:
    def __init__(self, jobs=()):
        self.jobs = {j["id"]: j for j in jobs}
        self.completed: list[tuple[str, bytes, str]] = []
        self.interrupted: list[str] = []
        self.failed: list[str] = []
        self.progress: list[dict] = []
        self.refuse: set[str] = set()  # job ids whose completion 409s

    def list_jobs(self):
        return list(self.jobs.values())

    def get_job(self, job_id):
        return self.jobs[job_id]

    def complete_job_cbor(self, job_id, cbor_bytes, content_hash):
        if job_id in self.refuse:
            raise RuntimeError("mechbench-api 409: job not running (status=failed)")
        self.completed.append((job_id, cbor_bytes, content_hash))
        self.jobs.setdefault(job_id, {"id": job_id})["status"] = "done"

    def interrupt_job(self, job_id, message, timeout=None):
        assert timeout is not None
        self.interrupted.append(job_id)
        self.jobs.setdefault(job_id, {"id": job_id})["status"] = "interrupted"

    def fail_job(self, job_id, message, timeout=None):
        self.failed.append(job_id)

    def report_progress(self, job_id, num, den, *, unit=None, status=None,
                        node=None, resumed_from=None):
        self.progress.append({"job": job_id, "status": status,
                              "resumed_from": resumed_from})

    def declare_preparing(self, *a, **k):
        return None

    def report_preparing_step(self, *a, **k):
        return None


def _runner(monkeypatch, runner_id="r_mine"):
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
        poll_interval_seconds=0.01, warm_model_id=None, runner_id=runner_id,
    )
    return jr.JobRunner(config)


def _spooled(job_id: str) -> bool:
    return (spool_dir() / job_id / "result.cbor").is_file()


class TestSpoolHelpers:
    def test_round_trip_and_clear(self):
        jr._spool_result("j_a", b"\xa1\x01\x02", "deadbeef")
        assert jr._spooled_result("j_a") == (b"\xa1\x01\x02", "deadbeef")
        assert jr._spooled_job_ids() == ["j_a"]
        jr._clear_spool("j_a")
        assert jr._spooled_result("j_a") is None
        assert jr._spooled_job_ids() == []

    def test_a_half_written_result_is_not_a_result(self):
        d = spool_dir() / "j_half"
        d.mkdir(parents=True)
        (d / "result.cbor.tmp").write_bytes(b"\xa0")
        assert jr._spooled_result("j_half") is None
        assert jr._spooled_job_ids() == []


class TestDeliver:
    def test_accepted_upload_clears_the_spool(self, monkeypatch):
        runner = _runner(monkeypatch)
        api = RecordingApi([{"id": "j_1", "status": "running"}])
        jr._spool_result("j_1", b"\xa0", "00")
        runner._deliver(api, "j_1", b"\xa0", "00")
        assert api.completed == [("j_1", b"\xa0", "sha256:00")]
        assert not _spooled("j_1")

    def test_a_refused_upload_keeps_the_spool_and_does_not_fail_the_job(
        self, monkeypatch
    ):
        runner = _runner(monkeypatch)
        api = RecordingApi([{"id": "j_1", "status": "failed"}])
        api.refuse.add("j_1")
        jr._spool_result("j_1", b"\xa0", "00")
        runner._deliver(api, "j_1", b"\xa0", "00")  # must not raise
        assert api.completed == [] and api.failed == []
        assert _spooled("j_1")


class TestFlushAtReconciliation:
    def test_an_interrupted_job_takes_its_late_result(self, monkeypatch):
        runner = _runner(monkeypatch)
        api = RecordingApi([{"id": "j_1", "status": "interrupted",
                             "claimedByRunnerId": "r_mine"}])
        jr._spool_result("j_1", b"\xa0", "00")
        runner._flush_spool(api)
        assert api.completed == [("j_1", b"\xa0", "sha256:00")]
        assert api.interrupted == []  # already interrupted; no extra call
        assert not _spooled("j_1")

    def test_our_own_running_orphan_is_interrupted_then_completed(self, monkeypatch):
        runner = _runner(monkeypatch)
        api = RecordingApi([{"id": "j_1", "status": "running",
                             "claimedByRunnerId": "r_mine"}])
        jr._spool_result("j_1", b"\xa0", "00")
        runner._flush_spool(api)
        assert api.interrupted == ["j_1"]
        assert [c[0] for c in api.completed] == ["j_1"]
        assert not _spooled("j_1")

    def test_a_job_that_ended_otherwise_clears_its_stale_result(self, monkeypatch):
        runner = _runner(monkeypatch)
        api = RecordingApi([{"id": "j_done", "status": "done"},
                            {"id": "j_failed", "status": "failed"}])
        jr._spool_result("j_done", b"\xa0", "00")
        jr._spool_result("j_failed", b"\xa0", "00")
        runner._flush_spool(api)
        assert api.completed == [] and api.interrupted == []
        assert not _spooled("j_done") and not _spooled("j_failed")

    def test_another_machines_job_is_not_delivered(self, monkeypatch):
        runner = _runner(monkeypatch)
        api = RecordingApi([{"id": "j_1", "status": "interrupted",
                             "claimedByRunnerId": "r_other"}])
        jr._spool_result("j_1", b"\xa0", "00")
        runner._flush_spool(api)
        assert api.completed == []
        assert _spooled("j_1")

    def test_the_job_in_hand_is_left_alone(self, monkeypatch):
        runner = _runner(monkeypatch)
        runner._active_job = "j_live"
        api = RecordingApi([{"id": "j_live", "status": "running",
                             "claimedByRunnerId": "r_mine"}])
        jr._spool_result("j_live", b"\xa0", "00")
        runner._flush_spool(api)
        assert api.completed == [] and api.interrupted == []

    def test_a_refusal_keeps_the_spool_for_the_next_pass(self, monkeypatch):
        runner = _runner(monkeypatch)
        api = RecordingApi([{"id": "j_1", "status": "interrupted",
                             "claimedByRunnerId": "r_mine"}])
        api.refuse.add("j_1")
        jr._spool_result("j_1", b"\xa0", "00")
        runner._flush_spool(api)  # must not raise
        assert _spooled("j_1")

    def test_reconciliation_flushes_before_it_reports_orphans(self, monkeypatch):
        # The spooled orphan is delivered (interrupted + completed) and is
        # therefore NOT reported as an orphan by the pass that follows.
        runner = _runner(monkeypatch)
        api = RecordingApi([{"id": "j_1", "status": "running",
                             "claimedByRunnerId": "r_mine",
                             "updatedAt": "2026-09-07T00:00:00.000Z"}])
        jr._spool_result("j_1", b"\xa0", "00")
        runner._reconcile_jobs(api)
        assert api.interrupted == ["j_1"]  # once, by the flush
        assert [c[0] for c in api.completed] == ["j_1"]


class TestHandleWithASpooledResult:
    def test_a_resumed_job_whose_result_is_spooled_is_delivered_not_recomputed(
        self, monkeypatch
    ):
        runner = _runner(monkeypatch)
        api = RecordingApi([{"id": "j_1", "status": "preparing",
                             "claimedByRunnerId": "r_mine"}])
        jr._spool_result("j_1", b"\xa0", "00")
        ran = []
        monkeypatch.setattr(runner._executor, "run",
                            lambda *a, **k: ran.append(1))
        runner._handle(api, {
            "id": "j_1", "protocolKind": "layer_ablation", "resume": True,
            "resumeCount": 1,
            "spec": {"prompt": "hi", "modelId": "org/m@r"},
        })
        assert ran == []  # nothing recomputed
        assert [c[0] for c in api.completed] == ["j_1"]
        assert api.progress and api.progress[0]["status"] == "running"
        assert api.progress[0]["resumed_from"]["reused"] == 1
        assert not _spooled("j_1")

    def test_a_fresh_run_spools_then_delivers_and_clears(self, monkeypatch):
        runner = _runner(monkeypatch)
        api = RecordingApi([{"id": "j_2", "status": "preparing",
                             "claimedByRunnerId": "r_mine"}])
        monkeypatch.setattr(runner._executor, "run",
                            lambda _s, on_progress=None, secrets=None: (
                                on_progress(1, 1), {"protocol": "layer_ablation"})[-1])
        monkeypatch.setattr(runner._executor, "_model_loaded",
                            lambda *_a, **_k: None)
        monkeypatch.setattr(jr, "dump_canonical", lambda _p: b"\xa0")
        runner._handle(api, {
            "id": "j_2", "protocolKind": "layer_ablation",
            "spec": {"prompt": "hi", "modelId": "org/m@r"},
        })
        assert [c[0] for c in api.completed] == ["j_2"]
        assert not _spooled("j_2")
        # a fresh (non-resumed) run reports no resumedFrom
        assert all(p["resumed_from"] is None for p in api.progress)

    def test_a_resumed_run_from_scratch_says_reused_zero(self, monkeypatch):
        runner = _runner(monkeypatch)
        api = RecordingApi([{"id": "j_3", "status": "preparing",
                             "claimedByRunnerId": "r_mine"}])
        gen = {"index": 1, "count": 1, "id": "gen"}
        monkeypatch.setattr(runner._executor, "run",
                            lambda _s, on_progress=None, secrets=None: (
                                on_progress(1, 2, gen), on_progress(2, 2, gen),
                                {"protocol": "pipeline"})[-1])
        monkeypatch.setattr(runner._executor, "_model_loaded",
                            lambda *_a, **_k: None)
        monkeypatch.setattr(jr, "dump_canonical", lambda _p: b"\xa0")
        runner._handle(api, {
            "id": "j_3", "protocolKind": "pipeline", "resume": True,
            "resumeCount": 1,
            "spec": {"bindings": {"model": "org/m@r"}},
        })
        firsts = [p for p in api.progress if p["resumed_from"] is not None]
        assert len(firsts) == 1
        assert firsts[0]["resumed_from"] == {"node": "gen", "reused": 0}
