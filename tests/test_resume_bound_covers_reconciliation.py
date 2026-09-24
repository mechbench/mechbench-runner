from __future__ import annotations

import pytest

pytest.importorskip("mechbench_compute")

from mechbench_runner import job_runner as jr  # noqa: E402
from mechbench_runner.config import Config  # noqa: E402


class RecordingApi:
    def __init__(self) -> None:
        self.failed: list[tuple[str, str]] = []

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
        poll_interval_seconds=0.01, warm_model_id=None, runner_id="r_mine"))


TRANSPORT = (jr.TRANSPORT_INTERRUPT_PREFIX
             + ", after retries: PUT https://api/x unreachable: timed out")


def _job(resume_count: int, reason: str | None) -> dict:
    return {"id": "j_1", "resume": True, "resumeCount": resume_count,
            "errorMessage": reason}


class TestTheBoundAtTheResumeDecision:
    def test_a_transport_resume_past_the_bound_is_refused_and_failed(self, monkeypatch):
        r = _runner(monkeypatch)
        api = RecordingApi()
        job = _job(jr.MAX_TRANSPORT_RESUMES, TRANSPORT)
        assert r._refuse_exhausted_transport_resume(api, job) is True
        assert [j for j, _ in api.failed] == ["j_1"]

    def test_the_failure_message_says_why(self, monkeypatch):
        r = _runner(monkeypatch)
        api = RecordingApi()
        r._refuse_exhausted_transport_resume(api, _job(5, TRANSPORT))
        _, message = api.failed[0]
        assert "not" in message and "transient" in message
        assert "body limit" in message

    def test_a_transport_resume_within_the_bound_proceeds(self, monkeypatch):
        r = _runner(monkeypatch)
        api = RecordingApi()
        job = _job(jr.MAX_TRANSPORT_RESUMES - 1, TRANSPORT)
        assert r._refuse_exhausted_transport_resume(api, job) is False
        assert api.failed == []

    def test_a_crash_resume_is_never_bounded_here(self, monkeypatch):
        r = _runner(monkeypatch)
        api = RecordingApi()
        job = _job(9, "interrupted by a crash or restart; it resumes with this process")
        assert r._refuse_exhausted_transport_resume(api, job) is False
        assert api.failed == []

    def test_no_reason_means_no_refusal(self, monkeypatch):
        r = _runner(monkeypatch)
        api = RecordingApi()
        assert r._refuse_exhausted_transport_resume(api, _job(9, None)) is False

    def test_the_interrupt_reason_written_matches_the_prefix_read(self, monkeypatch):
        from mechbench_compute.bench import BenchTransportError

        r = _runner(monkeypatch)

        class Api(RecordingApi):
            def __init__(self):
                super().__init__()
                self.interrupted: list[str] = []

            def interrupt_job(self, job_id, message, timeout=None):
                self.interrupted.append(message)

        api = Api()
        r._report_error(api, {"id": "j_1"}, BenchTransportError("unreachable"))
        assert api.interrupted and api.interrupted[0].startswith(
            jr.TRANSPORT_INTERRUPT_PREFIX)
