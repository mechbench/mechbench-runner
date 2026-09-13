"""An item the spool cannot write is counted and logged, never silently
dropped (task 000485, corrected).

For weeks every item of an adapted generate node raised CBOREncodeError
inside `JobSpool.item` — it carried a live ModelRef (000488) — and the
runner's `suppress(Exception)` around the call discarded each one. The
spool looked like the block had never produced anything, and a resume
that recovered nothing read as "nothing was there". The failure must
still not fail the job (the item exists in memory; the run goes on), but
it has to be visible in the log and in the spool summary.
"""

from __future__ import annotations

import pytest

pytest.importorskip("mechbench_compute")

from mechbench_runner import job_runner as jr  # noqa: E402
from mechbench_runner.config import Config  # noqa: E402
from mechbench_runner.spool import JobSpool  # noqa: E402


class Live:
    """Nothing canonical can encode this."""


def _runner(monkeypatch):
    class StubControl:
        def __init__(self, _state, path=None):
            self.path = path or "/tmp/stub.sock"

        def start(self):
            return None

        def stop(self):
            return None

    monkeypatch.setattr(jr, "ControlServer", StubControl)
    r = jr.JobRunner(Config(
        api_base_url="http://127.0.0.1:1", api_key="k",
        poll_interval_seconds=0.01, warm_model_id=None, runner_id="r_mine"))
    r._spool = JobSpool("j_drop")
    r._spool.node_start("gen", "sha256:fp")
    return r


class TestADroppedItem:
    def test_does_not_raise(self, monkeypatch):
        r = _runner(monkeypatch)
        r._spool_item("gen", "k1", {"item": Live()})  # must not propagate

    def test_is_counted_per_node(self, monkeypatch):
        r = _runner(monkeypatch)
        for i in range(3):
            r._spool_item("gen", f"k{i}", {"item": Live()})
        r._spool_item("other", "k0", {"item": Live()})
        assert r._spool.dropped == {"gen": 3, "other": 1}

    def test_is_logged_once_per_node_with_the_reason(self, monkeypatch, capsys):
        r = _runner(monkeypatch)
        for i in range(3):
            r._spool_item("gen", f"k{i}", {"item": Live()})
        out = capsys.readouterr().out
        assert out.count("could not be spooled") == 1
        assert "'gen'" in out and "k0" in out
        assert "CBOREncodeError" in out or "Error" in out

    def test_a_good_item_still_lands_and_counts_nothing(self, monkeypatch):
        r = _runner(monkeypatch)
        r._spool_item("gen", "k1", {"text": "story", "n": 1})
        assert r._spool.dropped == {}
        assert len(list((r._spool.root / "gen" / "items").glob("*.cbor"))) == 1

    def test_the_summary_reports_drops(self, monkeypatch):
        r = _runner(monkeypatch)
        r._spool_item("gen", "k1", {"item": Live()})
        r._spool_item("gen", "k2", {"text": "fine"})
        s = r._spool.summary()
        assert s.get("dropped") == {"gen": 1}

    def test_no_drops_means_no_dropped_key(self, monkeypatch):
        r = _runner(monkeypatch)
        r._spool_item("gen", "k2", {"text": "fine"})
        assert "dropped" not in r._spool.summary()
