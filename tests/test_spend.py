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
        assert ledger.changed() is False
        ledger.budget.settle(0.0, 0.001)
        assert ledger.changed() is True
        ledger.mark_reported()
        assert ledger.changed() is False

    def test_the_cap_stops_spending_at_the_job_level(self):
        from mechbench_compute.providers.errors import BudgetExceeded

        ledger = SpendLedger(0.10)
        node = ledger.budget.child(5.0)
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

    def test_slots_held_when_the_file_was_written_are_free_after_a_restart(
            self, tmp_path):
        path = tmp_path / "limits.json"
        first = SharedLimiter(path)
        for _ in range(8):
            first.acquire("anthropic", "claude-opus-5", "acct", "concurrency", 1)
        first.acquire("anthropic", "claude-opus-5", "acct", "requests", 1)
        first.save()
        second = SharedLimiter(path)
        assert all(b["currency"] != "concurrency"
                   for b in second.snapshot()["buckets"])
        for _ in range(8):
            assert second.acquire("anthropic", "claude-opus-5", "acct",
                                  "concurrency", 1) == 0.0
        snap = second.snapshot()
        slots = next(b for b in snap["buckets"] if b["currency"] == "concurrency")
        assert slots["capacity"] == 8 and slots["available"] == 0

    def test_a_file_from_before_the_fix_loads_with_every_slot_free(self, tmp_path):
        path = tmp_path / "limits.json"
        path.write_text(json.dumps({"version": 1, "saved_at": time.time(),
            "buckets": [
                {"key": ["anthropic", "claude-haiku-4-5", "acct", "concurrency"],
                 "tokens": 1.0, "capacity": 8.0, "per_second": 0.0},
                {"key": ["openai", "gpt-5", "acct", "requests"],
                 "tokens": 3.0, "capacity": 500.0, "per_second": 500 / 60},
            ], "holds": []}))
        lim = SharedLimiter(path)
        snap = {b["currency"]: b for b in lim.snapshot()["buckets"]}
        assert "concurrency" not in snap
        assert snap["requests"]["available"] < 500
        for _ in range(8):
            assert lim.acquire("anthropic", "claude-haiku-4-5", "acct",
                               "concurrency", 1) == 0.0

    def test_the_file_names_no_concurrency_bucket(self, tmp_path):
        path = tmp_path / "limits.json"
        lim = SharedLimiter(path)
        lim.acquire("openai", "gpt-5", "acct", "concurrency", 3)
        lim.acquire("openai", "gpt-5", "acct", "requests", 1)
        lim.save()
        keys = [e["key"] for e in json.loads(path.read_text())["buckets"]]
        assert keys == [["openai", "gpt-5", "acct", "requests"]]

    def test_a_release_saves_the_other_currencies(self, tmp_path):
        path = tmp_path / "limits.json"
        lim = SharedLimiter(path, save_every=0.0)
        lim.acquire("openai", "gpt-5", "acct", "concurrency", 1)
        lim.acquire("openai", "gpt-5", "acct", "requests", 7)
        assert not path.exists()
        lim.release("openai", "gpt-5", "acct", "concurrency", 1)
        entry = json.loads(path.read_text())["buckets"][0]
        assert entry["key"][3] == "requests" and entry["tokens"] < 500

    def test_the_runner_saves_the_limiter_when_it_stops(self, monkeypatch):
        import signal

        from mechbench_runner.paths import limits_path

        monkeypatch.setattr(jr, "ControlServer", StubControl)
        config = Config(api_base_url="http://127.0.0.1:1", api_key="mbk_test",
                        poll_interval_seconds=0.01, warm_model_id=None,
                        runner_id="rnr_1", from_stored_credentials=True)
        runner = jr.JobRunner(config)
        runner._limiter.acquire("openai", "gpt-5", "acct", "requests", 4)
        assert not limits_path().exists()
        handlers = {}
        monkeypatch.setattr(jr.signal, "signal",
                            lambda sig, fn: handlers.__setitem__(sig, fn))

        def stop_at_start():
            handlers[signal.SIGTERM](signal.SIGTERM, None)
            raise SystemExit(0)

        monkeypatch.setattr(runner, "_claim_control_socket", stop_at_start)
        with pytest.raises(SystemExit):
            runner.run()
        assert runner._shutdown is True
        entry = json.loads(limits_path().read_text())["buckets"][0]
        assert entry["key"] == ["openai", "gpt-5", "acct", "requests"]


class TestReporting:
    def test_spend_rides_along_with_progress(self, monkeypatch):
        monkeypatch.setattr(jr, "ControlServer", StubControl)
        api = RecordingApi()
        runner = jr.JobRunner(Config(
            api_base_url="http://localhost:3000", api_key="mbk_test",
            poll_interval_seconds=0.01, warm_model_id=None))

        def fake_run(_spec, *, on_progress=None, secrets=None, budget=None):
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
        assert all(s == pytest.approx(0.0125) for s in spends)
        assert api.progress[0]["spent_usd"] is None
