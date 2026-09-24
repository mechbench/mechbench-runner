from __future__ import annotations

import pytest

pytest.importorskip("mechbench_compute")

from mechbench_runner import job_runner as jr  # noqa: E402
from mechbench_runner.api_client import ApiError  # noqa: E402
from mechbench_runner.config import Config  # noqa: E402


class FakeApi:
    def __init__(self, *_a, **_k):
        self.claims = 0

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return None

    def claim_next_job(self):
        self.claims += 1
        raise ApiError(401, {"code": "UNAUTHORIZED"})


class StubControl:
    def __init__(self, _state, path=None):
        self.path = path or "/tmp/stub.sock"

    def start(self):
        return None

    def stop(self):
        return None


@pytest.fixture()
def runner(monkeypatch, tmp_path):
    monkeypatch.setattr(jr, "ControlServer", StubControl)
    config = Config(
        api_base_url="http://127.0.0.1:1",
        api_key="mbk_revoked",
        poll_interval_seconds=0.01,
        warm_model_id=None,
        runner_id="rnr_1",
        runner_name="office desktop",
        from_stored_credentials=True,
    )
    r = jr.JobRunner(config)
    monkeypatch.setattr(r, "_claim_control_socket", lambda: None)
    return r


def test_a_401_ends_the_loop(runner, monkeypatch):
    fake = FakeApi()
    monkeypatch.setattr(jr, "ApiClient", lambda *_a, **_k: fake)
    runner.run()
    assert fake.claims == 1
    assert runner.state.snapshot()["phase"] == "signed-out"


def test_it_says_which_credential_was_rejected(runner, monkeypatch, capsys):
    monkeypatch.setattr(jr, "ApiClient", lambda *_a, **_k: FakeApi())
    runner.run()
    out = capsys.readouterr().out
    assert "127.0.0.1:1" in out
    assert "signed out" in out.lower()
    assert "mechbench login" in out


def test_an_env_key_is_not_told_to_run_login(runner, monkeypatch, capsys):
    runner.config = Config(
        api_base_url="http://localhost:3000",
        api_key="mbk_env",
        poll_interval_seconds=0.01,
        warm_model_id=None,
        from_stored_credentials=False,
    )
    monkeypatch.setattr(jr, "ApiClient", lambda *_a, **_k: FakeApi())
    runner.run()
    out = capsys.readouterr().out
    assert "MECHBENCH_API_KEY" in out
    assert "mechbench login" not in out


def test_other_errors_still_retry(runner, monkeypatch):
    class Flaky(FakeApi):
        def claim_next_job(self):
            self.claims += 1
            if self.claims < 3:
                raise ApiError(503, "unavailable")
            raise KeyboardInterrupt

    flaky = Flaky()
    monkeypatch.setattr(jr, "ApiClient", lambda *_a, **_k: flaky)
    with pytest.raises(KeyboardInterrupt):
        runner.run()
    assert flaky.claims == 3
    assert runner.state.snapshot()["phase"] != "signed-out"
