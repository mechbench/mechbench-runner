from __future__ import annotations

import argparse

import pytest

from mechbench_runner import cli_key, credentials, login
from mechbench_runner.api_client import ApiError
from mechbench_runner.config import Config
from mechbench_runner.credentials import StoredCredentials

RUNNER_KEY = "mbk_runner_aaaa_secret"
CLI_KEY = "mbk_cli_bbbb_secret"


def _store(cli: str | None = CLI_KEY) -> None:
    credentials.save(StoredCredentials(
        api_url="http://127.0.0.1:1", api_key=RUNNER_KEY, runner_id="rnr_1",
        name="box", cli_key=cli))


@pytest.fixture(autouse=True)
def _no_env_key(monkeypatch):
    monkeypatch.delenv("MECHBENCH_API_KEY", raising=False)
    monkeypatch.delenv("MECHBENCH_API_URL", raising=False)


class TestTwoKeys:
    def test_the_file_holds_both_and_reads_them_back(self):
        _store()
        got = credentials.load()
        assert got is not None
        assert (got.api_key, got.cli_key) == (RUNNER_KEY, CLI_KEY)

    def test_the_service_path_uses_the_runner_key(self):
        _store()
        assert Config.from_env().api_key == RUNNER_KEY

    def test_a_verb_calls_with_the_cli_key(self):
        from mechbench_runner import verbs_cli
        from mechbench_runner.verbs.core import Ctx

        _store()
        seen: list[str | None] = []

        class Fake:
            def __init__(self, config):
                seen.append(config.api_key)

            def __enter__(self):
                return self

            def __exit__(self, *_a):
                return None

            def call(self, method, route, *, query=None, body=None):
                return [], {}

        config = cli_key.for_verbs(Config.from_env())
        ns = argparse.Namespace(noun="support", verb="list", as_json=True)
        for name in ("status", "all", "limit", "offset", "search"):
            setattr(ns, name, None)
        verbs_cli.main(config, ns, Ctx(config, client=Fake))
        assert seen == [CLI_KEY]

    def test_mechbench_api_key_overrides_both(self, monkeypatch):
        _store()
        monkeypatch.setenv("MECHBENCH_API_KEY", "mbk_env_cccc_secret")
        config = Config.from_env()
        assert config.api_key == "mbk_env_cccc_secret"
        assert cli_key.for_verbs(config).api_key == "mbk_env_cccc_secret"

    def test_login_stores_the_cli_key_enrollment_mints(self, monkeypatch):
        monkeypatch.setattr(login, "register_runner", lambda *_a, **_k: {
            "runner": {"id": "rnr_9", "name": "box"},
            "apiKey": RUNNER_KEY, "cliKey": CLI_KEY})
        monkeypatch.setattr(login, "_offer_service", lambda: None)
        assert login.login(Config.from_env(), token="mbr_x") == 0
        got = credentials.load()
        assert got is not None and (got.api_key, got.cli_key) == (RUNNER_KEY, CLI_KEY)


class FakeClient:
    asked_with: list[str | None] = []
    answer: dict | ApiError = {"cliKey": CLI_KEY, "name": "cli on box"}

    def __init__(self, config):
        FakeClient.asked_with.append(config.api_key)

    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return None

    def request_cli_key(self):
        if isinstance(FakeClient.answer, ApiError):
            raise FakeClient.answer
        return FakeClient.answer


class TestAMachineEnrolledBefore:
    @pytest.fixture(autouse=True)
    def _fake(self, monkeypatch):
        FakeClient.asked_with = []
        FakeClient.answer = {"cliKey": CLI_KEY, "name": "cli on box"}
        monkeypatch.setattr(cli_key, "ApiClient", FakeClient)

    def test_heals_itself_once_with_its_runner_key(self, capsys):
        _store(cli=None)
        config = cli_key.for_verbs(Config.from_env())
        assert config.api_key == CLI_KEY
        assert FakeClient.asked_with == [RUNNER_KEY]
        got = credentials.load()
        assert got is not None and (got.api_key, got.cli_key) == (RUNNER_KEY, CLI_KEY)
        assert capsys.readouterr().err.count("stored its CLI key") == 1
        assert cli_key.for_verbs(Config.from_env()).api_key == CLI_KEY
        assert FakeClient.asked_with == [RUNNER_KEY]
        assert Config.from_env().api_key == RUNNER_KEY

    def test_a_key_issued_before_says_what_to_do(self, capsys):
        _store(cli=None)
        FakeClient.answer = ApiError(409, {"code": "CLI_KEY_ISSUED"})
        assert cli_key.for_verbs(Config.from_env()).api_key == RUNNER_KEY
        assert "mechbench login" in capsys.readouterr().err
        got = credentials.load()
        assert got is not None and got.cli_key is None
