from __future__ import annotations

import pytest

pytest.importorskip("mechbench_compute")

from mechbench_runner import job_runner as jr  # noqa: E402
from mechbench_runner.api_client import ApiError  # noqa: E402
from mechbench_runner.config import Config  # noqa: E402

BODY = {
    "jobs": {"serve": ["own"], "allow": []},
    "extensions": {"admit": ["own"], "allow": [], "network": "none"},
    "upgrades": {"compute": "auto"},
    "gc": {"unused_days": 30},
}


class FakeApi:
    def __init__(self, claims: list[dict], served: list[int]) -> None:
        self.claims = list(claims)
        self.served = list(served)
        self.fetches = 0
        self.failed: list[tuple[str, str]] = []
        self.released: list[tuple[str, str, str]] = []

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return None

    def fetch_policy(self) -> dict:
        self.fetches += 1
        version = self.served[min(self.fetches, len(self.served)) - 1]
        return {"policyId": "pol_personal", "version": version, "body": BODY}

    def whoami(self) -> dict:
        return {"runner": {"id": "rnr_1", "scope": "user"}, "account": {"userId": "u_alice"}}

    def claim_next_job(self, *_a):
        if not self.claims:
            raise ApiError(401, {"code": "UNAUTHORIZED"})
        return self.claims.pop(0)

    def fail_job(self, job_id: str, message: str, timeout=None) -> None:
        self.failed.append((job_id, message))

    def release_job(self, job_id: str, code: str, message: str, timeout=None) -> None:
        self.released.append((job_id, code, message))

    def list_jobs(self):
        return []

    def live_leased(self):
        return []


class StubControl:
    def __init__(self, _state, path=None):
        self.path = path or "/tmp/stub.sock"

    def start(self):
        return None

    def stop(self):
        return None


@pytest.fixture()
def runner(monkeypatch):
    monkeypatch.setattr(jr, "ControlServer", StubControl)
    config = Config(api_base_url="http://127.0.0.1:1", api_key="mbk_test",
                    poll_interval_seconds=0.01, warm_model_id=None,
                    runner_id="rnr_1", from_stored_credentials=True)
    r = jr.JobRunner(config)
    monkeypatch.setattr(r, "_claim_control_socket", lambda: None)
    monkeypatch.setattr(r, "_reconcile_jobs", lambda _api: None)
    handled: list[str] = []
    monkeypatch.setattr(r, "_handle", lambda _api, job: handled.append(job["id"]))
    r.handled = handled
    return r


def _job(version: int) -> dict:
    return {"id": f"j_{version}", "creatorId": "u_alice", "projectId": "prj_1",
            "projectOwner": {"kind": "user", "id": "u_alice"},
            "policy": {"id": "pol_personal", "version": version}}


def test_a_runner_holding_an_older_version_refuses_and_refreshes(runner, monkeypatch):
    fake = FakeApi([_job(2)], served=[1])
    monkeypatch.setattr(jr, "ApiClient", lambda *_a, **_k: fake)
    runner.run()
    assert fake.fetches >= 2
    assert runner.handled == []
    assert fake.failed == []
    assert fake.released and fake.released[0][:2] == ("j_2", "POLICY_MISMATCH")


def test_a_refresh_that_catches_up_runs_the_job(runner, monkeypatch):
    fake = FakeApi([_job(2)], served=[1, 2])
    monkeypatch.setattr(jr, "ApiClient", lambda *_a, **_k: fake)
    runner.run()
    assert runner.handled == ["j_2"]
    assert fake.released == []


def test_a_matching_claim_runs_without_a_fetch(runner, monkeypatch):
    fake = FakeApi([_job(1)], served=[1])
    monkeypatch.setattr(jr, "ApiClient", lambda *_a, **_k: fake)
    runner.run()
    assert fake.fetches == 1
    assert runner.handled == ["j_1"]


def test_a_policy_frame_marks_the_copy_stale_and_wakes_the_loop(runner):
    runner._live.wake.clear()
    runner._policy.start(FakeApi([], served=[1]))
    runner._on_policy({"id": "pol_personal", "version": 2})
    assert runner._policy.stale
    assert runner._live.wake.is_set()
