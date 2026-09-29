from __future__ import annotations

import json
import pathlib

import httpx
import pytest

from mechbench_runner import policy as pol
from mechbench_runner.api_client import ApiClient, ApiError
from mechbench_runner.config import Config
from mechbench_runner.policy import (
    POLICY_MISMATCH,
    Admit,
    PolicyHolder,
    check_installs,
    policy_admits,
)

ROOT = pathlib.Path(__file__).resolve().parent.parent
VENDORED = ROOT / "mechbench_runner" / "policy_cases.json"
MODELS = ROOT.parent / "mechbench-models" / "src" / "policy_cases.json"
CASES = json.loads(VENDORED.read_text())

PERSONAL = {
    "extensions": {"install": "mine", "allow": [], "network": "none",
                   "require_approved": False},
    "upgrades": {"compute": "auto"},
    "gc": {"unused_days": 30},
    "pools": [],
}
LOCKED = {**PERSONAL, "extensions": {**PERSONAL["extensions"], "install": "locked"}}


def test_the_vendored_cases_are_the_models_copy():
    if not MODELS.exists():
        pytest.skip("no mechbench-models checkout beside this one")
    assert VENDORED.read_bytes() == MODELS.read_bytes(), (
        "mechbench_runner/policy_cases.json is stale: run\n"
        "  cp ../mechbench-models/src/policy_cases.json mechbench_runner/"
    )


def test_there_are_cases():
    assert len(CASES) >= 31


@pytest.mark.parametrize("case", CASES, ids=lambda c: c["name"])
def test_the_twin_agrees_with_every_case(case):
    got = policy_admits(case["policy"], case["extension"], case["context"])
    assert got.ok is case["ok"]
    assert got.reason == case.get("reason")


class FakeApi:
    def __init__(self, *versions: tuple[str, int, dict]) -> None:
        self.versions = list(versions)
        self.fetches = 0
        self.failed: list[tuple[str, str]] = []
        self.released: list[tuple[str, str, str]] = []
        self.down = False
        self.name: str | None = None

    def fetch_policy(self) -> dict:
        self.fetches += 1
        if self.down:
            raise ConnectionError("unreachable")
        pid, ver, body = self.versions[min(self.fetches, len(self.versions)) - 1]
        got = {"policyId": pid, "version": ver, "body": body}
        if self.name is not None:
            got["name"] = self.name
        return got

    def whoami(self) -> dict:
        return {"runner": {"id": "rnr_1", "userId": "u_alice"},
                "account": {"userId": "u_alice", "handle": "alice", "displayName": None},
                "scopeLabel": "alice"}

    def fail_job(self, job_id: str, message: str) -> None:
        self.failed.append((job_id, message))

    def release_job(self, job_id: str, code: str, message: str) -> None:
        self.released.append((job_id, code, message))


@pytest.fixture()
def holder(tmp_path):
    return PolicyHolder(tmp_path / "policy.json")


def claim(version: int = 1, install=None, **extra) -> dict:
    job = {"id": "j_1", "userId": "u_alice",
           "policy": {"id": "pol_personal", "version": version}, **extra}
    if install is not None:
        job["install"] = install
    return job


EXT = {"name": "bob/tools", "owner": "u_bob", "state": "verified",
       "visibility": "public", "needs": []}


class TestTheHeldCopy:
    def test_startup_fetches_and_mirrors_it(self, holder, capsys):
        api = FakeApi(("pol_personal", 1, PERSONAL))
        holder.start(api)
        assert holder.held.version == 1
        assert not holder.stale
        mirrored = json.loads(holder.path.read_text())
        assert mirrored == {"id": "pol_personal", "version": 1, "body": PERSONAL}
        assert "policy pol_personal v1 applied" in capsys.readouterr().out

    def test_a_restart_with_the_api_down_starts_from_the_mirror(self, holder, capsys):
        holder.start(FakeApi(("pol_personal", 3, LOCKED)))
        again = PolicyHolder(holder.path)
        api = FakeApi(("pol_personal", 3, LOCKED))
        api.down = True
        again.start(api)
        assert again.held == holder.held
        assert again.stale
        assert "from the last run" in capsys.readouterr().out

    def test_the_mirrored_copy_is_confirmed_once_the_api_answers(self, holder):
        holder.start(FakeApi(("pol_personal", 3, LOCKED)))
        again = PolicyHolder(holder.path)
        api = FakeApi(("pol_personal", 3, LOCKED))
        api.down = True
        again.start(api)
        api.down = False
        again._retry_at = 0.0
        assert again.ensure_current(api)
        assert not again.stale

    def test_a_failed_refresh_waits_before_asking_again(self, holder):
        api = FakeApi(("pol_personal", 1, PERSONAL))
        api.down = True
        holder.start(api)
        assert not holder.ensure_current(api)
        assert api.fetches == 1

    def test_a_new_version_named_by_the_api_asks_at_once(self, holder):
        api = FakeApi(("pol_personal", 1, PERSONAL))
        api.down = True
        holder.start(api)
        api.down = False
        api.versions = [("pol_personal", 2, LOCKED)]
        assert holder.note({"id": "pol_personal", "version": 2})
        assert holder.ensure_current(api)
        assert holder.held.version == 2

    def test_a_frame_naming_the_held_version_changes_nothing(self, holder):
        api = FakeApi(("pol_personal", 1, PERSONAL))
        holder.start(api)
        assert not holder.note({"id": "pol_personal", "version": 1})
        assert holder.ensure_current(api)
        assert api.fetches == 1

    def test_a_frame_naming_another_version_refetches(self, holder, capsys):
        api = FakeApi(("pol_personal", 1, PERSONAL), ("pol_team", 4, LOCKED))
        holder.start(api)
        assert holder.note({"id": "pol_team", "version": 4})
        assert holder.stale
        assert holder.ensure_current(api)
        assert (holder.held.id, holder.held.version) == ("pol_team", 4)
        assert json.loads(holder.path.read_text())["version"] == 4
        assert "policy pol_team v4 applied" in capsys.readouterr().out

    def test_a_malformed_frame_is_ignored(self, holder):
        holder.start(FakeApi(("pol_personal", 1, PERSONAL)))
        assert not holder.note({"id": "pol_personal"})
        assert not holder.note(None)
        assert not holder.stale

    def test_a_401_is_not_swallowed(self, holder):
        class Revoked(FakeApi):
            def fetch_policy(self):
                raise ApiError(401, {"code": "UNAUTHORIZED"})

        holder.note({"id": "pol_personal", "version": 1})
        with pytest.raises(ApiError):
            holder.ensure_current(Revoked())


class TestTheDoubleCheck:
    def test_with_no_install_list_a_matching_claim_is_a_no_op(self, holder):
        api = FakeApi(("pol_personal", 1, PERSONAL))
        holder.start(api)
        assert check_installs(api, holder, claim(1)) == []
        assert api.released == []
        assert api.fetches == 1

    def test_an_older_held_version_refreshes_and_goes_on(self, holder):
        api = FakeApi(("pol_personal", 1, PERSONAL), ("pol_personal", 2, PERSONAL))
        holder.start(api)
        assert check_installs(api, holder, claim(2)) == []
        assert api.fetches == 2
        assert holder.held.version == 2
        assert api.released == []

    def test_a_mismatch_that_survives_the_refresh_releases_the_claim(
            self, holder, capsys):
        api = FakeApi(("pol_personal", 1, PERSONAL))
        holder.start(api)
        admits = check_installs(api, holder, claim(2))
        assert [a.ok for a in admits] == [False]
        assert api.released == [(
            "j_1",
            POLICY_MISMATCH,
            "The claim was made under pol_personal v2; "
            "this runner holds pol_personal v1.",
        )]
        assert api.failed == []
        assert "job j_1 released: POLICY_MISMATCH" in capsys.readouterr().out

    def test_the_log_names_the_policy_when_the_api_does(self, holder, capsys):
        api = FakeApi(("pol_personal", 1, PERSONAL))
        api.name = "personal"
        holder.start(api)
        assert holder.held.name == "personal"
        assert json.loads(holder.path.read_text())["name"] == "personal"
        assert PolicyHolder(holder.path).load().name == "personal"
        check_installs(api, holder, claim(2))
        out = capsys.readouterr().out
        assert "policy personal (pol_personal) v1 applied" in out
        assert api.released[0][2].endswith("this runner holds personal (pol_personal) v1.")

    def test_an_api_without_release_is_failed_as_before(self, holder):
        class Older(FakeApi):
            def release_job(self, job_id, code, message):
                raise ApiError(404, {"code": "NOT_FOUND"})

        api = Older(("pol_personal", 1, LOCKED))
        holder.start(api)
        check_installs(api, holder, claim(1, install=[EXT]))
        assert api.failed == [
            ("j_1", f"{POLICY_MISMATCH}: The policy is locked: it installs nothing.")]

    def test_an_unreachable_policy_releases_a_mismatched_claim(self, holder):
        api = FakeApi(("pol_personal", 1, PERSONAL))
        holder.start(api)
        api.down = True
        check_installs(api, holder, claim(2))
        assert api.released and api.released[0][1] == POLICY_MISMATCH

    def test_a_claim_without_a_policy_is_not_compared(self, holder):
        api = FakeApi(("pol_personal", 1, PERSONAL))
        holder.start(api)
        job = claim(1)
        job["policy"] = None
        assert check_installs(api, holder, job) == []

    def test_an_admitted_install_is_not_released(self, holder):
        api = FakeApi(("pol_personal", 1, PERSONAL))
        holder.start(api)
        admits = check_installs(api, holder, claim(1, install=[EXT]))
        assert admits == [Admit(True)]
        assert api.released == []

    def test_a_refused_install_releases_with_the_rule_s_words(self, holder):
        api = FakeApi(("pol_personal", 1, LOCKED))
        holder.start(api)
        admits = check_installs(api, holder, claim(1, install=[EXT]))
        assert admits == [Admit(False, "The policy is locked: it installs nothing.")]
        assert api.released == [
            ("j_1", POLICY_MISMATCH, "The policy is locked: it installs nothing.")]

    def test_the_context_is_the_runner_s_owner_and_the_job_s_creator(self, holder):
        api = FakeApi(("pol_personal", 1, PERSONAL))
        holder.start(api)
        draft = {**EXT, "state": "draft"}
        admits = check_installs(api, holder, claim(1, install=[draft]))
        assert admits == [Admit(False, "Under mine, bob/tools must be verified or "
                                       "your own draft; it is draft.")]

    def test_no_held_policy_installs_nothing(self, holder):
        api = FakeApi(("pol_personal", 1, PERSONAL))
        api.down = True
        job = claim(1, install=[EXT])
        job["policy"] = None
        admits = check_installs(api, holder, job)
        assert [a.ok for a in admits] == [False]
        assert len(api.released) == 1


def test_the_client_fetches_the_held_policy():
    seen: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req.url.path)
        return httpx.Response(200, json={"policyId": "pol_personal", "version": 1,
                                         "body": PERSONAL})

    api = ApiClient(Config(api_base_url="http://127.0.0.1:1", api_key="mbk_test",
                           poll_interval_seconds=0.01, warm_model_id=None,
                           runner_id="r_mine"))
    api._client.close()
    api._client = httpx.Client(base_url="http://127.0.0.1:1",
                               transport=httpx.MockTransport(handler))
    assert api.fetch_policy()["version"] == 1
    api.close()
    assert seen == ["/runners/me/policy"]


def test_the_default_mirror_lives_in_the_runner_s_state_dir():
    holder = PolicyHolder()
    assert holder.path == pol.paths.policy_path()
    assert holder.path.name == "policy.json"


def test_the_client_releases_a_job_with_the_claim_token():
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(200, json={"ok": True, "status": "queued"})

    api = ApiClient(Config(api_base_url="http://127.0.0.1:1", api_key="mbk_test",
                           poll_interval_seconds=0.01, warm_model_id=None,
                           runner_id="r_mine"))
    api._client.close()
    api._client = httpx.Client(base_url="http://127.0.0.1:1",
                               transport=httpx.MockTransport(handler))
    api.claim_tokens["j_1"] = "tok"
    api.release_job("j_1", POLICY_MISMATCH, "The policy is locked: it installs nothing.")
    api.close()
    assert [r.url.path for r in seen] == ["/jobs/j_1/release"]
    assert seen[0].headers["x-claim-token"] == "tok"
    assert json.loads(seen[0].content) == {
        "code": POLICY_MISMATCH, "message": "The policy is locked: it installs nothing."}
    assert "j_1" not in api.claim_tokens
