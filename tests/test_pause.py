from __future__ import annotations

import asyncio
import json

import pytest

pytest.importorskip("mechbench_compute")

from mechbench_compute.protocol import Stop  # noqa: E402

from mechbench_runner import job_runner as jr  # noqa: E402
from mechbench_runner.api_client import ApiError  # noqa: E402
from mechbench_runner.channel import LiveChannel  # noqa: E402
from mechbench_runner.config import Config  # noqa: E402
from mechbench_runner.control import RunnerState  # noqa: E402
from mechbench_runner.extensions import Extensions  # noqa: E402
from mechbench_runner.spool import JobSpool, held_spools  # noqa: E402

BODY = {
    "jobs": {"serve": ["own"], "allow": []},
    "extensions": {"admit": ["own"], "allow": [], "network": "none"},
    "upgrades": {"compute": "auto"},
    "gc": {"unused_days": 30},
}

ITEMS = ["k0", "k1", "k2", "k3"]


def _job(job_id: str, resume: bool = False) -> dict:
    return {"id": job_id, "protocolKind": "pipeline", "resume": resume,
            "resumeCount": 1 if resume else 0,
            "creatorId": "u_alice", "projectId": "prj_1",
            "projectOwner": {"kind": "user", "id": "u_alice"},
            "policy": {"id": "pol_personal", "version": 1},
            "spec": {"bindings": {"model": "org/m@r"}}}


class FakeApi:
    def __init__(self, claims: list) -> None:
        self.claims = list(claims)
        self.asked = 0
        self.paused: list[str] = []
        self.completed: list[str] = []
        self.failed: list[str] = []
        self.interrupted: list[str] = []
        self.told: list[bool] = []
        self.progress: list[str] = []
        self.pause_requested: set[str] = set()
        self.statuses: dict[str, str] = {}

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return None

    def fetch_policy(self) -> dict:
        return {"policyId": "pol_personal", "version": 1, "body": BODY}

    def whoami(self) -> dict:
        return {"runner": {"id": "rnr_1", "scope": "user"},
                "account": {"userId": "u_alice"}}

    def claim_next_job(self, *_a):
        self.asked += 1
        if not self.claims:
            raise ApiError(401, {"code": "UNAUTHORIZED"})
        nxt = self.claims.pop(0)
        return nxt() if callable(nxt) else nxt

    def report_progress(self, job_id, num, den, **_kw):
        self.progress.append(job_id)
        return {"ok": True, "pauseRequested": job_id in self.pause_requested}

    def declare_preparing(self, job_id, steps):
        return {"ok": True, "steps": steps, "pauseRequested": False}

    def report_preparing_step(self, *_a, **_k):
        return {"ok": True}

    def report_paused(self, job_id, timeout=None):
        self.paused.append(job_id)

    def complete_job_cbor(self, job_id, cbor_bytes, content_hash):
        self.completed.append(job_id)

    def fail_job(self, job_id, message, timeout=None):
        self.failed.append(job_id)

    def interrupt_job(self, job_id, message, timeout=None):
        self.interrupted.append(job_id)

    def set_paused(self, paused, timeout=None):
        self.told.append(paused)
        return {"ok": True}

    def get_job(self, job_id):
        if job_id not in self.statuses:
            raise ApiError(404, {"code": "NOT_FOUND"})
        return {"id": job_id, "status": self.statuses[job_id]}

    def list_jobs(self):
        return []

    def live_leased(self):
        return []


class StubControl:
    tell_server = None

    def __init__(self, _state, path=None):
        self.path = path or "/tmp/stub.sock"

    def start(self):
        return None

    def stop(self):
        return None


class FakeWs:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send(self, raw: str) -> None:
        self.sent.append(json.loads(raw))


_sent = iter(range(10**6))


def _command(runner, name: str, **args) -> dict:
    ws = FakeWs()
    frame = {"v": 1, "type": "command", "id": f"c_{next(_sent)}", "name": name,
             **({"args": args} if args else {})}
    asyncio.run(runner._channel._run_command(ws, frame))
    return ws.sent[-1]


@pytest.fixture()
def runner(monkeypatch):
    monkeypatch.setattr(jr, "ControlServer", StubControl)
    monkeypatch.setattr(jr.shutil, "which", lambda _name: None)
    config = Config(api_base_url="http://127.0.0.1:1", api_key="mbk_test",
                    poll_interval_seconds=0.01, warm_model_id=None,
                    runner_id="rnr_1", from_stored_credentials=True)
    r = jr.JobRunner(config)
    monkeypatch.setattr(r, "_claim_control_socket", lambda: None)
    monkeypatch.setattr(r, "_sweep_cache", lambda _job: None)
    monkeypatch.setattr(r, "_start_gpu", lambda _job_id: None)
    monkeypatch.setattr(r._executor, "_model_loaded", lambda *a, **k: None)
    monkeypatch.setattr(jr, "dump_canonical", lambda _p: b"\xa0")
    r.runs: list[dict] = []
    return r


def _executor(runner, monkeypatch, *, during=None):
    def run(_spec, on_progress=None, secrets=None, resume=None, budget=None,
            stop=None):
        job_id = runner._active_job
        runner._executor._model = object()
        runner._executor._model_id = "org/m@r"
        seen = dict(((resume or {}).get("gen") or {}).get("items") or {})
        runner.runs.append({"job": job_id, "resumed": sorted(seen)})
        runner._executor._on_node_start("gen", "fp")
        for i, key in enumerate(ITEMS):
            if key in seen:
                continue
            runner._executor._on_spool_item("gen", key, {"id": key})
            on_progress(i + 1, len(ITEMS), {"index": 0, "count": 1, "id": "gen"})
            if during is not None and i == 1:
                during(job_id)
            if stop is not None and stop():
                raise Stop("gen", f"stopped after item {key!r}, which is spooled")
        return {"protocol": "pipeline"}

    monkeypatch.setattr(runner._executor, "run", run)


def _exit_after_idle(runner, monkeypatch, *, then=None, idle: int = 3):
    slept = {"paused": 0}

    def sleep(_seconds):
        if not runner.state.paused:
            return
        slept["paused"] += 1
        if slept["paused"] == idle:
            if then is not None:
                then()
            else:
                runner.state.request_exit(0, "test over")

    monkeypatch.setattr(runner, "_sleep", sleep)
    return slept


def test_a_pause_command_stops_the_job_at_a_safe_point_and_frees_the_model(
        runner, monkeypatch):
    acks: list[dict] = []
    _executor(runner, monkeypatch,
              during=lambda _id: acks.append(_command(runner, "pause")))
    api = FakeApi([_job("j_1"), _job("j_2")])
    monkeypatch.setattr(jr, "ApiClient", lambda *_a, **_k: api)
    _exit_after_idle(runner, monkeypatch)
    phases: list[str] = []
    runner.state.add_listener(lambda m: phases.append(m["data"]["phase"]))

    assert runner.run() == 0

    assert acks[0]["ok"] is True and acks[0]["state"]["phase"] == "pausing"
    assert api.paused == ["j_1"]
    assert api.failed == [] and api.completed == []
    assert api.asked == 1, "a paused runner claimed another job"
    assert runner._executor._model is None and runner._executor._model_id is None
    assert sorted(JobSpool("j_1").resume_map()["gen"]["items"]) == ["k0", "k1"]
    assert held_spools() == ["j_1"]
    snap = runner.state.snapshot()
    assert snap["phase"] == "paused" and snap["paused"] is True
    assert snap["model_id"] is None
    assert "pausing" in phases and phases[-1] == "paused"


def test_resume_claims_again_and_the_paused_job_runs_from_its_spool(
        runner, monkeypatch):
    _executor(runner, monkeypatch, during=lambda _id: _command(runner, "pause"))
    api = FakeApi([_job("j_1"), _job("j_1", resume=True)])
    monkeypatch.setattr(jr, "ApiClient", lambda *_a, **_k: api)
    _exit_after_idle(runner, monkeypatch, then=lambda: _command(runner, "resume"))

    runner.run()

    assert api.paused == ["j_1"]
    assert api.asked == 3
    assert runner.runs == [{"job": "j_1", "resumed": []},
                           {"job": "j_1", "resumed": ["k0", "k1"]}]
    assert api.completed == ["j_1"] and api.failed == []
    assert held_spools() == []


def test_pause_job_pauses_the_active_job_and_the_runner_moves_on(runner, monkeypatch):
    acks: list[dict] = []

    def during(job_id):
        if job_id == "j_1":
            acks.append(_command(runner, "pause_job", id="j_9"))
            acks.append(_command(runner, "pause_job", id="j_1"))

    _executor(runner, monkeypatch, during=during)
    api = FakeApi([_job("j_1"), _job("j_2")])
    monkeypatch.setattr(jr, "ApiClient", lambda *_a, **_k: api)

    runner.run()

    assert acks[0]["ok"] is False and "j_9" in acks[0]["error"]
    assert acks[1]["ok"] is True
    assert api.paused == ["j_1"]
    assert api.completed == ["j_2"]
    assert runner.state.paused is False
    assert held_spools() == ["j_1"]


def test_a_progress_reply_asking_for_a_pause_does_what_the_command_does(
        runner, monkeypatch):
    _executor(runner, monkeypatch,
              during=lambda job_id: api.pause_requested.add(job_id)
              if job_id == "j_1" else None)
    api = FakeApi([_job("j_1"), _job("j_2")])
    monkeypatch.setattr(jr, "ApiClient", lambda *_a, **_k: api)

    runner.run()

    assert api.paused == ["j_1"]
    assert api.completed == ["j_2"] and api.failed == []
    assert sorted(JobSpool("j_1").resume_map()["gen"]["items"]) == ITEMS


def test_a_job_that_ends_before_its_next_safe_point_completes(runner, monkeypatch):
    _executor(runner, monkeypatch)
    runner.state.request_stop("j_1")
    api = FakeApi([_job("j_1")])
    monkeypatch.setattr(jr, "ApiClient", lambda *_a, **_k: api)

    runner.run()

    assert api.paused == [] and api.completed == ["j_1"]


def test_welcome_paused_after_a_restart_claims_nothing(runner, monkeypatch):
    _executor(runner, monkeypatch)
    api = FakeApi([_job("j_1")])
    monkeypatch.setattr(jr, "ApiClient", lambda *_a, **_k: api)
    asyncio.run(runner._channel._handle(FakeWs(), {"v": 1, "type": "welcome",
                                                   "paused": True}))
    _exit_after_idle(runner, monkeypatch)

    runner.run()

    assert api.asked == 0
    assert runner.state.snapshot()["phase"] == "paused"


def test_welcome_not_paused_lifts_a_local_pause(runner):
    runner.state.pause()
    asyncio.run(runner._channel._handle(FakeWs(), {"v": 1, "type": "welcome",
                                                   "paused": False}))
    assert runner.state.paused is False


def test_jobs_next_answering_paused_holds_the_runner(runner, monkeypatch):
    _executor(runner, monkeypatch)
    api = FakeApi([{"paused": True}, _job("j_1")])
    monkeypatch.setattr(jr, "ApiClient", lambda *_a, **_k: api)
    _exit_after_idle(runner, monkeypatch)

    runner.run()

    assert api.asked == 1
    assert runner.state.paused is True
    assert runner.runs == []


def test_a_stop_never_fails_the_job_or_clears_its_spool(runner, monkeypatch):
    def run(_spec, on_progress=None, secrets=None, resume=None, budget=None, stop=None):
        runner._executor._model = object()
        runner._executor._on_node_start("gen", "fp")
        runner._executor._on_spool_item("gen", "k0", {"id": "k0"})
        raise Stop("gen", "stopped after item 'k0', which is spooled")

    monkeypatch.setattr(runner._executor, "run", run)
    api = FakeApi([])

    assert runner._handle(api, _job("j_1")) == jr.PAUSED

    assert api.failed == [] and api.completed == [] and api.interrupted == []
    assert api.paused == ["j_1"]
    assert list(JobSpool("j_1").resume_map()["gen"]["items"]) == ["k0"]
    assert runner._executor._model is None
    assert runner._active_job is None and runner.state.snapshot()["job"] is None


def test_a_pause_that_cannot_be_reported_still_keeps_the_spool(runner, monkeypatch):
    def run(_spec, on_progress=None, secrets=None, resume=None, budget=None, stop=None):
        runner._executor._on_node_start("gen", "fp")
        runner._executor._on_spool_item("gen", "k0", {"id": "k0"})
        raise Stop("gen", "stopped")

    monkeypatch.setattr(runner._executor, "run", run)
    api = FakeApi([])

    def unreachable(*_a, **_k):
        raise OSError("network down")

    api.report_paused = unreachable
    assert runner._handle(api, _job("j_1")) == jr.PAUSED
    assert api.failed == []
    assert held_spools() == ["j_1"]


def test_update_is_refused_while_a_spool_is_held(runner):
    JobSpool("j_held").node_start("gen", "fp")
    ack = _command(runner, "update")
    assert ack["ok"] is False
    assert "j_held" in ack["error"] and "restart from zero" in ack["error"]
    assert runner.state.exit_requested is None


def test_the_self_upgrade_waits_while_a_spool_is_held(monkeypatch):
    from mechbench_runner import extensions as ext

    def refuse(*_a, **_k):
        raise AssertionError("upgraded while a spool was held")

    monkeypatch.setattr(ext.release_manifest, "upgrade", refuse)
    monkeypatch.setattr(ext.install_mod, "detect", refuse)
    e = Extensions(manifest_url="http://127.0.0.1:1/manifest.json")
    assert e.self_upgrade(BODY, hold=lambda: "a paused job's spool") is None


def test_a_cancelled_jobs_spool_is_cleared_at_the_next_flush(runner):
    for job_id in ("j_paused", "j_cancelled", "j_gone"):
        JobSpool(job_id).node_start("gen", "fp")
    api = FakeApi([])
    api.statuses = {"j_paused": "paused", "j_cancelled": "cancelled"}
    runner._flush_spool(api)
    assert held_spools() == ["j_paused"]


def test_a_local_pause_tells_the_server(runner, monkeypatch):
    api = FakeApi([])
    monkeypatch.setattr(jr, "ApiClient", lambda *_a, **_k: api)
    assert runner._tell_server("pause") is None
    assert runner._tell_server("resume") is None
    assert api.told == [True, False]


def test_a_runner_resume_withdraws_its_own_stop_but_not_the_jobs():
    state = RunnerState(version="0", api_url="http://127.0.0.1:1")
    state.job_claimed("j_1", "pipeline", None)
    state.pause()
    assert state.stop_requested() and state.phase == "pausing"
    state.resume()
    assert not state.stop_requested() and state.phase == "executing"
    state.request_stop("j_1")
    state.pause()
    state.resume()
    assert state.stop_requested()


def test_a_job_claimed_while_paused_stops_at_its_first_safe_point():
    state = RunnerState(version="0", api_url="http://127.0.0.1:1")
    state.pause()
    state.job_claimed("j_1", "pipeline", None)
    assert state.stop_requested()


def test_every_event_and_the_hello_carry_the_phase():
    state = RunnerState(version="0", api_url="http://127.0.0.1:1")
    seen: list[dict] = []
    state.add_listener(seen.append)
    state.set_phase("idle")
    state.pause()
    state.emit("job.finished", {"id": "j_1"})
    assert [m["data"]["phase"] for m in seen] == ["paused", "paused"]
    channel = LiveChannel.__new__(LiveChannel)
    channel.state = state
    hello = channel._hello()
    assert hello["phase"] == "paused" and hello["paused"] is True


def test_a_nodes_second_start_does_not_restart_its_watch(runner):
    started: list[str] = []

    class Watch:
        def start(self, nid):
            started.append(nid)

    runner._watch = Watch()
    runner._watched = set()
    runner._spool_node_start("peer", "fp")
    runner._spool_node_start("peer", "fp")
    assert started == ["peer"]
