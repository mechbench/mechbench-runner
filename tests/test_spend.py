"""The job spend ledger and the machine's shared limiter (task 000338).

The ledger is the runner's view of money: what a job has spent so far,
reported with progress and shown in `mechbench status`. The limiter is
the machine's view of quota: one set of buckets for every job here,
surviving a restart, because an account that was throttled ten seconds
ago is still throttled after the process comes back.
"""

from __future__ import annotations

import json
import time

import pytest

pytest.importorskip("mechbench_compute")

from mechbench_runner import job_runner as jr  # noqa: E402
from mechbench_runner.config import Config  # noqa: E402
from mechbench_runner.spend import SharedLimiter, SpendLedger  # noqa: E402


class RecordingApi:
    def __init__(self):
        self.progress: list[dict] = []

    def report_progress(self, job_id, num, den, *, unit=None, status=None,
                        node=None, spent_usd=None):
        self.progress.append({"num": num, "status": status,
                              "spent_usd": spent_usd})

    def declare_preparing(self, *_a, **_k):
        return None

    def report_preparing_step(self, *_a, **_k):
        return None

    def complete_job_cbor(self, *_a, **_k):
        return None


class StubControl:
    def __init__(self, _state, path=None):
        self.path = path or "/tmp/stub.sock"

    def start(self):
        return None

    def stop(self):
        return None


class TestLedger:
    def test_it_counts_what_the_executor_spends_against_the_runs_cap(self):
        ledger = SpendLedger(2.5)
        assert ledger.cap_usd == 2.5 and ledger.spent_usd == 0.0
        ledger.budget.settle(0.0, 0.25)
        assert ledger.spent_usd == 0.25 and ledger.calls == 1
        assert ledger.describe() == "$0.2500 of $2.50"
        assert ledger.to_wire() == {"spent_usd": 0.25, "calls": 1, "cap_usd": 2.5}

    def test_a_job_that_bought_nothing_reports_nothing(self):
        ledger = SpendLedger(None)
        assert ledger.changed() is False        # most jobs are local
        ledger.budget.settle(0.0, 0.001)
        assert ledger.changed() is True
        ledger.mark_reported()
        assert ledger.changed() is False

    def test_the_cap_stops_spending_at_the_job_level(self):
        from mechbench_compute.providers.errors import BudgetExceeded

        ledger = SpendLedger(0.10)
        node = ledger.budget.child(5.0)         # a node cap far above it
        assert node.cap_usd == 0.10
        with pytest.raises(BudgetExceeded):
            node.reserve(0.5)


class TestSharedLimiter:
    def test_a_hold_survives_a_restart(self, tmp_path):
        path = tmp_path / "limits.json"
        first = SharedLimiter(path)
        first.penalize("anthropic", "claude-opus-5", "acct", 30.0)
        assert path.exists()
        state = json.loads(path.read_text())
        assert state["holds"] and state["holds"][0]["until"] > time.time() + 20

        # A new process, the same machine: still held.
        second = SharedLimiter(path)
        snap = second.snapshot()
        assert snap["holds"][0]["provider"] == "anthropic"
        assert 20 < snap["holds"][0]["seconds"] <= 30

    def test_buckets_persist_and_status_can_read_them(self, tmp_path):
        path = tmp_path / "limits.json"
        lim = SharedLimiter(path)
        lim.acquire("openai", "gpt-5", "acct", "requests", 5)
        lim.save()
        snap = SharedLimiter(path).snapshot()
        bucket = next(b for b in snap["buckets"] if b["currency"] == "requests")
        assert bucket["available"] < bucket["capacity"]
        assert bucket["provider"] == "openai"

    def test_nothing_it_writes_names_a_credential(self, tmp_path):
        from mechbench_compute.providers.limiter import scope_for

        path = tmp_path / "limits.json"
        scope = scope_for("anthropic", {"token": "sk-ant-super-secret"})
        lim = SharedLimiter(path)
        lim.acquire("anthropic", "claude-opus-5", scope, "requests", 1)
        lim.penalize("anthropic", "claude-opus-5", scope, 5.0)
        text = path.read_text()
        assert "sk-ant-super-secret" not in text
        assert scope in text and len(scope) == 12


class TestReporting:
    def test_spend_rides_along_with_progress(self, monkeypatch):
        monkeypatch.setattr(jr, "ControlServer", StubControl)
        api = RecordingApi()
        runner = jr.JobRunner(Config(
            api_base_url="http://localhost:3000", api_key="mbk_test",
            poll_interval_seconds=0.01, warm_model_id=None))

        def fake_run(_spec, *, on_progress=None, secrets=None, budget=None):
            # Stand in for the transport settling a call mid-run.
            for i in (1, 2, 3, 4, 5):
                if i == 3 and budget is not None:
                    budget.settle(0.0, 0.0125)
                on_progress(i, 5)
            return {"protocol": "pipeline"}

        monkeypatch.setattr(runner._executor, "run", fake_run)
        monkeypatch.setattr(runner._executor, "_model_loaded", lambda *_a, **_k: None)
        monkeypatch.setattr(jr, "dump_canonical", lambda _p: b"\xa0")
        runner._handle(api, {
            "id": "job_1", "protocolKind": "pipeline",
            "spec": {"graph": {"nodes": [], "edges": []}, "budgetUsd": 1.0},
        })
        spends = [p["spent_usd"] for p in api.progress if p["spent_usd"] is not None]
        assert spends and spends[0] == pytest.approx(0.0125)
        # The total is reported, not a delta, so a dropped report is free.
        assert all(s == pytest.approx(0.0125) for s in spends)
        # Ticks before any spend carry nothing at all.
        assert api.progress[0]["spent_usd"] is None
