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
        assert not jr.JobRunner._is_transport(BenchError("PUT -> 400: bad path"))

    def test_a_block_that_raised_is_not(self):
        assert not jr.JobRunner._is_transport(ValueError("bad layer index"))

    def test_it_looks_through_the_cause_chain(self):
        try:
            try:
                raise BenchTransportError("unreachable: write timed out")
            except BenchTransportError as inner:
                raise RuntimeError("node 'gen' failed") from inner
        except RuntimeError as outer:
            assert jr.JobRunner._is_transport(outer)

    def test_it_looks_through_implicit_context_too(self):
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
        runner = _runner(monkeypatch)

        class Refusing(RecordingApi):
            def interrupt_job(self, job_id, message, timeout=None):
                raise RuntimeError("api down too")

        jr._spool_result("j_5", b"\xa0", "00")
        runner._report_error(Refusing(), {"id": "j_5"},
                             BenchTransportError("unreachable"))
        assert _spooled("j_5")

class TestTheResumeBoundIsNotOptional:
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
