"""Reconciliation: the runner repairs the server's idea of its work.

A job read "running" on the board for an hour after its runner died
(2026-08-25). The dying-breath fail covers watchdog deaths; this covers
everything else — kill -9, power loss, a fail report that never landed
— because the NEXT process compares the server's claims against local
truth. The invariant that makes it safe: only jobs THIS machine claims
are ever touched.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

pytest.importorskip("mechbench_compute")

from mechbench_runner import job_runner as jr  # noqa: E402
from mechbench_runner.config import Config  # noqa: E402


def _iso(minutes_ago: float) -> str:
    when = datetime.now(UTC) - timedelta(minutes=minutes_ago)
    return when.strftime("%Y-%m-%dT%H:%M:%S.000Z")


class RecordingApi:
    def __init__(self, jobs):
        self.jobs = jobs
        self.failed: list[str] = []

    def list_jobs(self):
        return self.jobs

    def fail_job(self, job_id, message, timeout=None):
        assert timeout is not None  # reconciliation must be bounded
        self.failed.append(job_id)


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
        poll_interval_seconds=0.01, warm_model_id=None,
        runner_id=runner_id,
    )
    return jr.JobRunner(config)


def job(job_id, *, status="running", claimed_by=None, updated_minutes_ago=1.0):
    return {"id": job_id, "status": status, "claimedByRunnerId": claimed_by,
            "updatedAt": _iso(updated_minutes_ago)}


class TestOwnClaims:
    def test_an_orphan_this_machine_claims_is_failed(self, monkeypatch):
        runner = _runner(monkeypatch)
        api = RecordingApi([job("j_orphan", claimed_by="r_mine")])
        runner._reconcile_jobs(api)
        assert api.failed == ["j_orphan"]

    def test_the_job_in_hand_is_never_touched(self, monkeypatch):
        runner = _runner(monkeypatch)
        runner._active_job = "j_live"
        api = RecordingApi([
            job("j_live", claimed_by="r_mine"),
            job("j_orphan", claimed_by="r_mine"),
        ])
        runner._reconcile_jobs(api)
        assert api.failed == ["j_orphan"]

    def test_another_machines_claim_is_never_touched(self, monkeypatch):
        runner = _runner(monkeypatch)
        api = RecordingApi([job("j_theirs", claimed_by="r_other")])
        runner._reconcile_jobs(api)
        assert api.failed == []

    def test_terminal_jobs_are_not_in_scope(self, monkeypatch):
        runner = _runner(monkeypatch)
        api = RecordingApi([
            job("j_done", status="done", claimed_by="r_mine"),
            job("j_failed", status="failed", claimed_by="r_mine"),
        ])
        runner._reconcile_jobs(api)
        assert api.failed == []

    def test_without_a_runner_identity_attributed_jobs_are_left_alone(
        self, monkeypatch
    ):
        # A hand-pasted env key has no runner record; it cannot prove an
        # attributed claim is its own.
        runner = _runner(monkeypatch, runner_id=None)
        api = RecordingApi([job("j_x", claimed_by="r_somebody")])
        runner._reconcile_jobs(api)
        assert api.failed == []


class TestLegacyClaims:
    """Pre-attribution jobs (API < 0048) name no runner: reconciled only
    from an idle runner, only after a long silence."""

    def test_stale_and_idle_reconciles(self, monkeypatch):
        runner = _runner(monkeypatch)
        api = RecordingApi([job("j_zombie", claimed_by=None,
                                updated_minutes_ago=60)])
        runner._reconcile_jobs(api)
        assert api.failed == ["j_zombie"]

    def test_fresh_is_left_alone(self, monkeypatch):
        runner = _runner(monkeypatch)
        api = RecordingApi([job("j_new", claimed_by=None,
                                updated_minutes_ago=2)])
        runner._reconcile_jobs(api)
        assert api.failed == []

    def test_a_busy_runner_does_not_touch_legacy_jobs(self, monkeypatch):
        runner = _runner(monkeypatch)
        runner._active_job = "j_live"
        api = RecordingApi([job("j_zombie", claimed_by=None,
                                updated_minutes_ago=60)])
        runner._reconcile_jobs(api)
        assert api.failed == []

    def test_an_unreadable_timestamp_reads_as_fresh(self, monkeypatch):
        runner = _runner(monkeypatch)
        api = RecordingApi([{"id": "j_odd", "status": "running",
                             "claimedByRunnerId": None, "updatedAt": None}])
        runner._reconcile_jobs(api)
        assert api.failed == []


class TestResilience:
    def test_a_listing_failure_is_survived(self, monkeypatch):
        runner = _runner(monkeypatch)

        class Down:
            def list_jobs(self):
                raise ConnectionError("api unreachable")

        runner._reconcile_jobs(Down())  # must not raise

    def test_a_refused_fail_does_not_stop_the_pass(self, monkeypatch):
        runner = _runner(monkeypatch)

        class Choosy(RecordingApi):
            def fail_job(self, job_id, message, timeout=None):
                if job_id == "j_refused":
                    raise ValueError("NOT_CLAIMANT")
                super().fail_job(job_id, message, timeout=timeout)

        api = Choosy([
            job("j_refused", claimed_by="r_mine"),
            job("j_orphan", claimed_by="r_mine"),
        ])
        runner._reconcile_jobs(api)
        assert api.failed == ["j_orphan"]
