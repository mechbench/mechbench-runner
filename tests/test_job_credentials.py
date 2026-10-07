from __future__ import annotations

import signal
from pathlib import Path

import pytest

from mechbench_runner import job_credentials as jc
from mechbench_runner import paths
from mechbench_runner.api_client import ApiError

SECRET = "sk-ant-never-on-disk-5c1f0a"
HF_SECRET = "hf_never-on-disk-77e2"


def _on_disk(root: Path, *values: str) -> list[str]:
    found = []
    for p in root.rglob("*"):
        if p.is_file() and not p.is_symlink():
            data = p.read_bytes()
            found += [f"{p}: {v}" for v in values if v.encode() in data]
    return found


class CredentialsApi:
    def __init__(self, credentials=None, status=None):
        self.asked: list[str] = []
        self.credentials = credentials or {}
        self.status = status

    def job_credentials(self, job_id):
        self.asked.append(job_id)
        if self.status is not None:
            raise ApiError(self.status, {"code": "JOB_NOT_ACTIVE"})
        return {"jobId": job_id, "providers": sorted(self.credentials),
                "credentials": {k: dict(v) for k, v in self.credentials.items()},
                "missing": []}


class TestTheHolder:
    def test_it_asks_for_the_providers_the_claim_names_and_empties_in_place(self):
        held = jc.JobCredentials()
        api = CredentialsApi({"anthropic": {"token": SECRET}})
        secrets = held.fetch(api, {"id": "j_1", "providers": ["anthropic"]})
        assert api.asked == ["j_1"]
        assert secrets == {"anthropic": {"token": SECRET}}
        lent = secrets["anthropic"]
        held.clear()
        assert secrets == {} and lent == {} and held.empty

    def test_a_claim_naming_no_provider_asks_for_nothing(self):
        held = jc.JobCredentials()
        api = CredentialsApi({"anthropic": {"token": SECRET}})
        assert held.fetch(api, {"id": "j_1", "providers": []}) == {}
        assert api.asked == []

    def test_credentials_an_older_api_put_in_the_claim_are_taken_out_of_it(self):
        held = jc.JobCredentials()
        job = {"id": "j_1", "integrations": {"hf": {"token": HF_SECRET}}}
        api = CredentialsApi()
        assert held.fetch(api, job) == {"hf": {"token": HF_SECRET}}
        assert "integrations" not in job and api.asked == []
        held.clear()

    def test_a_job_no_longer_in_flight_is_an_error_not_an_empty_answer(self):
        held = jc.JobCredentials()
        with pytest.raises(ApiError):
            held.fetch(CredentialsApi(status=409), {"id": "j_1", "providers": ["hf"]})
        assert held.empty

    def test_the_process_empties_it_at_exit(self):
        import subprocess
        import sys

        script = (
            "import atexit\n"
            "atexit.register(lambda: print('empty at exit:', __import__("
            "'mechbench_runner.job_credentials', fromlist=['HELD']).HELD.empty))\n"
            "from mechbench_runner.job_credentials import HELD\n"
            "class Api:\n"
            "    def job_credentials(self, job_id):\n"
            "        return {'credentials': {'hf': {'token': 'x'}}, 'missing': []}\n"
            "HELD.fetch(Api(), {'id': 'j_1', 'providers': ['hf']})\n"
            "print('held:', not HELD.empty)\n"
        )
        out = subprocess.run([sys.executable, "-c", script], capture_output=True,
                             text=True, check=True).stdout
        assert "held: True" in out and "empty at exit: True" in out


compute = pytest.importorskip("mechbench_compute")

from mechbench_runner import job_runner as jr  # noqa: E402
from mechbench_runner.config import Config  # noqa: E402


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


class JobApi(CredentialsApi):
    def __init__(self, check, credentials):
        super().__init__(credentials)
        self.check = check
        self.completed: list[str] = []
        self.claim_tokens = {"j_remote": "tok"}

    def report_progress(self, job_id, num, den, **_kw):
        self.check("progress")

    def declare_preparing(self, *a, **k):
        return None

    def report_preparing_step(self, *a, **k):
        return None

    def complete_job_cbor(self, job_id, cbor_bytes, content_hash):
        self.check("delivery")
        self.completed.append(job_id)


def _job():
    records = [{"id": f"r{i}", "coords": {}, "system": "be brief", "user": f"q{i}"}
               for i in range(3)]
    graph = {"dataflow": 2, "edges": [], "nodes": [{
        "id": "ask", "block": "text/chat",
        "params": {"model": {"provider": "anthropic", "model": "claude-fable-5-1"},
                   "budget_usd": 1.0, "max_tokens": 16},
        "inputs": {"records": records}}]}
    return {"id": "j_remote", "protocolKind": "pipeline",
            "providers": ["anthropic", "hf"],
            "spec": {"graph": graph, "params": {}}}


class TestAJobWithARemoteNode:
    def _wire(self, monkeypatch):
        from mechbench_compute.providers import http

        home = paths.mechbench_dir()
        seen: list[str] = []
        sent: list[str] = []

        def check(moment):
            seen.append(moment)
            leaked = _on_disk(home, SECRET, HF_SECRET)
            assert not leaked, f"a credential on disk at {moment}: {leaked}"

        def fake_post(url, *, headers, payload=None, **_kw):
            sent.append(headers.get("x-api-key", ""))
            check("provider call")
            return http.HttpResponse(status=200, headers={}, body={
                "id": "msg_1", "type": "message", "role": "assistant",
                "model": "claude-fable-5-1",
                "content": [{"type": "text", "text": "an answer"}],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 5, "output_tokens": 3}})

        monkeypatch.setattr(http, "post_json", fake_post)
        return home, check, seen, sent

    def test_no_credential_reaches_the_disk_and_the_holder_is_empty_after(
            self, monkeypatch):
        home, check, seen, sent = self._wire(monkeypatch)
        runner = _runner(monkeypatch)
        api = JobApi(check, {"anthropic": {"token": SECRET},
                             "hf": {"token": HF_SECRET}})
        lent: list[dict] = []
        real_run = runner._executor.run

        def run(spec, **kw):
            lent.append(kw["secrets"])
            return real_run(spec, **kw)

        monkeypatch.setattr(runner._executor, "run", run)
        runner._handle(api, _job())
        assert api.completed == ["j_remote"]
        assert api.asked == ["j_remote"]
        assert sent and all(s == SECRET for s in sent)
        assert {"provider call", "progress", "delivery"} <= set(seen)
        check("after the job")
        assert jc.HELD.empty
        assert lent == [{}]
        assert any(home.rglob("*")), "the job wrote nothing under ~/.mechbench to check"

    def test_a_failed_job_empties_the_holder_and_a_restart_asks_again(
            self, monkeypatch):
        _home, check, _seen, _sent = self._wire(monkeypatch)
        runner = _runner(monkeypatch)
        api = JobApi(check, {"anthropic": {"token": SECRET}})
        lent: list[dict] = []

        def crash(spec, **kw):
            lent.append(kw["secrets"])
            assert kw["secrets"] == {"anthropic": {"token": SECRET}}
            raise RuntimeError("the machine lost power")

        monkeypatch.setattr(runner._executor, "run", crash)
        with pytest.raises(RuntimeError):
            runner._handle(api, _job())
        assert jc.HELD.empty and lent == [{}]
        check("after the failure")

        restarted = _runner(monkeypatch)
        def rerun(spec, **kw):
            lent.append({k: dict(v) for k, v in kw["secrets"].items()})
            return {"protocol": "pipeline"}

        monkeypatch.setattr(restarted._executor, "run", rerun)
        monkeypatch.setattr(jr, "dump_canonical", lambda _p: b"\xa0")
        restarted._handle(api, {**_job(), "resume": True, "resumeCount": 1})
        assert api.asked == ["j_remote", "j_remote"]
        assert lent[-1] == {"anthropic": {"token": SECRET}}
        assert jc.HELD.empty

    def test_a_failure_reports_no_value_the_job_was_lent(self, monkeypatch, capsys):
        _home, check, _seen, _sent = self._wire(monkeypatch)
        runner = _runner(monkeypatch)
        custom = "plain-custom-credential-0042"
        api = JobApi(check, {"anthropic": {"token": SECRET}, "custom": {"key": custom}})
        failed: list[str] = []
        api.fail_job = lambda job_id, message, timeout=None: failed.append(message)

        def crash(spec, **kw):
            raise RuntimeError(f"the provider refused {custom} and {SECRET}")

        monkeypatch.setattr(runner._executor, "run", crash)
        job = _job()
        with pytest.raises(RuntimeError) as got:
            runner._handle(api, job)
        assert jc.HELD.empty
        _message, trace = runner._failure_text(got.value)
        assert "RuntimeError" in trace
        assert custom not in trace and SECRET not in trace
        runner._report_error(api, job, got.value)
        [message] = failed
        assert "the provider refused" in message
        assert custom not in message and SECRET not in message

    def test_sigterm_while_idle_empties_the_holder(self, monkeypatch):
        runner = _runner(monkeypatch)
        jc.HELD.fetch(CredentialsApi({"hf": {"token": HF_SECRET}}),
                      {"id": "j_x", "providers": ["hf"]})
        before = signal.getsignal(signal.SIGTERM)
        try:
            runner.install_signal_handlers()
            signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)
        finally:
            signal.signal(signal.SIGTERM, before)
            signal.signal(signal.SIGINT, signal.default_int_handler)
        assert jc.HELD.empty
