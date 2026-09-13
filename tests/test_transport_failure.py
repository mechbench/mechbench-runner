"""A job whose UPLOAD failed is interrupted, not failed (task 000464).

Experiment 014's adapted run died twice at node `gen`, once with 197 of
200 stories generated and once with 200 of 200:

    PUT https://api.mechbench.ai/objects/.../gen unreachable:
        The write operation timed out

The payload was 0.9 MB and the API was healthy moments later. The old
path called `fail_job` and then `_clear_spool`, which is right for a block
that raised and wrong here: the compute succeeded, so the spooled items
were the only copy of 35 minutes of generation and they were deleted.

The distinction is drawn by CLASS, not by matching the message:
`bench.BenchTransportError` is raised only after compute's bounded retry
has exhausted itself on something that never returned a verdict.
"""

from __future__ import annotations

import pytest

pytest.importorskip("mechbench_compute")

from mechbench_compute.bench import BenchError, BenchTransportError  # noqa: E402

from mechbench_runner import job_runner as jr  # noqa: E402
from mechbench_runner.config import Config  # noqa: E402
from mechbench_runner.paths import spool_dir  # noqa: E402


class RecordingApi:
    def __init__(self) -> None:
        self.interrupted: list[tuple[str, str]] = []
        self.failed: list[tuple[str, str]] = []

    def interrupt_job(self, job_id, message, timeout=None):
        self.interrupted.append((job_id, message))

    def fail_job(self, job_id, message, timeout=None):
        self.failed.append((job_id, message))


def _runner(monkeypatch):
    class StubControl:
        def __init__(self, _state, path=None):
            self.path = path or "/tmp/stub.sock"

        def start(self):
            return None

        def stop(self):
            return None

    monkeypatch.setattr(jr, "ControlServer", StubControl)
    return jr.JobRunner(Config(
        api_base_url="http://127.0.0.1:1", api_key="k",
        poll_interval_seconds=0.01, warm_model_id=None, runner_id="r_mine",
    ))


def _spooled(job_id: str) -> bool:
    return (spool_dir() / job_id).is_dir()


class TestClassification:
    def test_a_transport_error_is_recognised(self):
        assert jr.JobRunner._is_transport(BenchTransportError("unreachable"))

    def test_an_ordinary_bench_error_is_not(self):
        """A 400 from the API is the job's own problem."""
        assert not jr.JobRunner._is_transport(BenchError("PUT -> 400: bad path"))

    def test_a_block_that_raised_is_not(self):
        assert not jr.JobRunner._is_transport(ValueError("bad layer index"))

    def test_it_looks_through_the_cause_chain(self):
        """The executor wraps a node's exception with the node id."""
        try:
            try:
                raise BenchTransportError("unreachable: write timed out")
            except BenchTransportError as inner:
                raise RuntimeError("node 'gen' failed") from inner
        except RuntimeError as outer:
            assert jr.JobRunner._is_transport(outer)

    def test_it_looks_through_implicit_context_too(self):
        """`raise X` inside an `except` block sets __context__, not
        __cause__, and the executor does not always use `from`."""
        try:
            try:
                raise BenchTransportError("unreachable")
            except BenchTransportError:
                raise RuntimeError("node 'gen' failed")  # noqa: B904
        except RuntimeError as outer:
            assert jr.JobRunner._is_transport(outer)

    def test_a_cycle_in_the_chain_terminates(self):
        a = RuntimeError("a")
        b = RuntimeError("b")
        a.__cause__ = b
        b.__cause__ = a
        assert jr.JobRunner._is_transport(a) is False


class TestReporting:
    def test_a_transport_failure_interrupts_and_keeps_the_spool(self, monkeypatch):
        runner = _runner(monkeypatch)
        api = RecordingApi()
        jr._spool_result("j_1", b"\xa0", "00")
        runner._report_error(api, {"id": "j_1"},
                             BenchTransportError("PUT /objects/x unreachable"))
        assert api.failed == []
        assert [j for j, _ in api.interrupted] == ["j_1"]
        assert _spooled("j_1"), "the spool is the only copy of the compute"

    def test_a_real_failure_still_fails_and_clears(self, monkeypatch):
        runner = _runner(monkeypatch)
        api = RecordingApi()
        jr._spool_result("j_2", b"\xa0", "00")
        runner._report_error(api, {"id": "j_2"}, ValueError("bad layer index"))
        assert [j for j, _ in api.failed] == ["j_2"]
        assert api.interrupted == []
        assert not _spooled("j_2")

    def test_a_4xx_still_fails(self, monkeypatch):
        """The payload was rejected; resuming would re-send the same bytes."""
        runner = _runner(monkeypatch)
        api = RecordingApi()
        runner._report_error(api, {"id": "j_3"},
                             BenchError("PUT /objects/x -> 400: bad path"))
        assert [j for j, _ in api.failed] == ["j_3"]
        assert api.interrupted == []

    def test_the_interrupt_reason_says_it_was_the_transport(self, monkeypatch):
        runner = _runner(monkeypatch)
        api = RecordingApi()
        runner._report_error(api, {"id": "j_4"},
                             BenchTransportError("PUT /objects/x unreachable"))
        _, message = api.interrupted[0]
        assert "unreachable" in message and "retries" in message

    def test_hf_tokens_are_still_redacted(self, monkeypatch):
        runner = _runner(monkeypatch)
        api = RecordingApi()
        runner._report_error(api, {"id": "j_6"}, BenchTransportError(
            "PUT https://api/x?token=hf_abcdefgh12345678 unreachable"))
        _, message = api.interrupted[0]
        assert "hf_[redacted]" in message and "abcdefgh" not in message

    def test_an_unreportable_interrupt_is_not_fatal(self, monkeypatch):
        """Reconciliation interrupts our own orphan on the next pass."""
        runner = _runner(monkeypatch)

        class Refusing(RecordingApi):
            def interrupt_job(self, job_id, message, timeout=None):
                raise RuntimeError("api down too")

        jr._spool_result("j_5", b"\xa0", "00")
        runner._report_error(Refusing(), {"id": "j_5"},
                             BenchTransportError("unreachable"))
        assert _spooled("j_5")

class TestTheResumeBoundIsNotOptional:
    """Interrupt-and-resume assumes the failure was transient. When it is
    deterministic — a result the server will not accept — each resume
    re-runs the whole node to reach the same rejection (000483).

    014 demonstrated it: a 35-minute generation node, an upload the API
    stalled on every time, and a loop that would have run until the
    battery died. The retry is only safe because it is bounded.
    """

    def test_the_first_failures_interrupt(self, monkeypatch):
        runner = _runner(monkeypatch)
        api = RecordingApi()
        exc = BenchTransportError("unreachable")
        for _ in range(jr.MAX_TRANSPORT_RESUMES):
            runner._report_error(api, {"id": "j_1"}, exc)
        assert len(api.interrupted) == jr.MAX_TRANSPORT_RESUMES
        assert api.failed == []

    def test_the_next_one_fails(self, monkeypatch):
        runner = _runner(monkeypatch)
        api = RecordingApi()
        exc = BenchTransportError("unreachable")
        for _ in range(jr.MAX_TRANSPORT_RESUMES + 1):
            runner._report_error(api, {"id": "j_1"}, exc)
        assert [j for j, _ in api.failed] == ["j_1"]

    def test_the_failure_says_it_is_not_transient(self, monkeypatch):
        runner = _runner(monkeypatch)
        api = RecordingApi()
        exc = BenchTransportError("PUT /objects/x unreachable")
        for _ in range(jr.MAX_TRANSPORT_RESUMES + 1):
            runner._report_error(api, {"id": "j_1"}, exc)
        _, message = api.failed[0]
        assert "not" in message and "transient" in message
        assert "body limit" in message, "point the reader at the real cause"

    def test_the_servers_resume_count_carries_the_bound(self, monkeypatch):
        """A restart empties the in-process tally; `resumeCount` does not,
        so the bound survives the runner dying mid-loop."""
        runner = _runner(monkeypatch)
        api = RecordingApi()
        runner._report_error(api, {"id": "j_1", "resumeCount": 9},
                            BenchTransportError("unreachable"))
        assert api.interrupted == []
        assert [j for j, _ in api.failed] == ["j_1"]

    def test_the_bound_is_per_job(self, monkeypatch):
        runner = _runner(monkeypatch)
        api = RecordingApi()
        exc = BenchTransportError("unreachable")
        for _ in range(jr.MAX_TRANSPORT_RESUMES + 1):
            runner._report_error(api, {"id": "j_1"}, exc)
        runner._report_error(api, {"id": "j_2"}, exc)
        assert [j for j, _ in api.interrupted][-1] == "j_2"
        assert [j for j, _ in api.failed] == ["j_1"]

    def test_a_finished_job_releases_its_budget(self, monkeypatch):
        runner = _runner(monkeypatch)
        runner._transport_interrupts["j_1"] = jr.MAX_TRANSPORT_RESUMES
        runner._transport_interrupts.pop("j_1", None)
        assert runner._transport_resumes("j_1", {"id": "j_1"}) == 0
