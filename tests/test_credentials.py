"""Stored credentials and the login flow (task 000284)."""

from __future__ import annotations

import os
import stat

import pytest

from mechbench_runner import credentials, machine
from mechbench_runner.config import Config
from mechbench_runner.credentials import StoredCredentials
from mechbench_runner.login import web_url


@pytest.fixture()
def store(tmp_path):
    return tmp_path / "config.toml"


def test_round_trip(store):
    creds = StoredCredentials(
        api_url="https://api.mechbench.ai",
        api_key="mbk_abc12def_secret",
        runner_id="rnr_x",
        name="office desktop",
        registered_at="2026-08-22T12:00:00Z",
    )
    credentials.save(creds, store)
    assert credentials.load(store) == creds


def test_written_0600_and_not_readable_by_others(store):
    credentials.save(StoredCredentials(api_url="http://x", api_key="mbk_k"), store)
    mode = stat.S_IMODE(os.stat(store).st_mode)
    assert mode == 0o600, oct(mode)


def test_no_temp_file_left_behind(store):
    credentials.save(StoredCredentials(api_url="http://x", api_key="mbk_k"), store)
    assert not (store.with_name(store.name + ".tmp")).exists()


def test_missing_file_reads_as_signed_out(store):
    assert credentials.load(store) is None


def test_garbage_reads_as_signed_out_not_a_traceback(store):
    store.write_text("this is not toml {{{")
    assert credentials.load(store) is None


def test_incomplete_table_reads_as_signed_out(store):
    store.write_text('[runner]\napi_url = "http://x"\n')  # no key
    assert credentials.load(store) is None


def test_awkward_values_survive_the_hand_rolled_writer(store):
    # The writer is ours, so the escaping is ours to get wrong.
    awkward = 'benji\'s "box" \\ back\\slash'
    credentials.save(
        StoredCredentials(api_url="http://x", api_key="mbk_k", name=awkward), store
    )
    assert credentials.load(store).name == awkward


def test_clear(store):
    credentials.save(StoredCredentials(api_url="http://x", api_key="mbk_k"), store)
    assert credentials.clear(store) is True
    assert credentials.clear(store) is False
    assert credentials.load(store) is None


class TestConfigPrecedence:
    """The environment owns the credential entirely when it supplies one."""

    def test_env_key_wins_and_ignores_the_stored_pair(self, store, monkeypatch):
        credentials.save(
            StoredCredentials(api_url="https://api.mechbench.ai", api_key="mbk_stored"),
            store,
        )
        monkeypatch.setattr(credentials, "config_path", lambda: store)
        monkeypatch.setenv("MECHBENCH_API_KEY", "mbk_from_env")
        monkeypatch.setenv("MECHBENCH_API_URL", "http://localhost:3000")
        config = Config.from_env()
        assert config.api_key == "mbk_from_env"
        # Crucially NOT the stored URL: a production key must not be
        # posted to localhost because only one half was overridden.
        assert config.api_base_url == "http://localhost:3000"
        assert config.from_stored_credentials is False

    def test_stored_pair_used_when_the_env_is_silent(self, store, monkeypatch):
        credentials.save(
            StoredCredentials(
                api_url="https://api.mechbench.ai",
                api_key="mbk_stored",
                runner_id="rnr_1",
                name="laptop",
            ),
            store,
        )
        monkeypatch.setattr(credentials, "config_path", lambda: store)
        monkeypatch.delenv("MECHBENCH_API_KEY", raising=False)
        config = Config.from_env()
        assert config.api_key == "mbk_stored"
        assert config.api_base_url == "https://api.mechbench.ai"
        assert config.runner_id == "rnr_1"
        assert config.runner_name == "laptop"
        assert config.from_stored_credentials is True

    def test_no_credentials_explains_itself(self, store, monkeypatch):
        monkeypatch.setattr(credentials, "config_path", lambda: store)
        monkeypatch.delenv("MECHBENCH_API_KEY", raising=False)
        config = Config.from_env()
        assert config.api_key is None
        with pytest.raises(RuntimeError, match="not signed in"):
            config.require_api_key()


class TestMachine:
    def test_default_name_drops_the_mdns_suffix(self, monkeypatch):
        monkeypatch.setattr(machine, "hostname", lambda: "studio.local")
        assert machine.default_name() == "studio"

    def test_default_name_survives_a_hostile_hostname(self, monkeypatch):
        monkeypatch.setattr(machine, "hostname", lambda: "x" * 300)
        assert len(machine.default_name()) <= 80

    def test_platform_is_answerable_without_a_compute_backend(self):
        # The point of machine.py: this must work on the machine that
        # cannot import mechbench_compute at all.
        assert "python" in machine.describe_platform()


class TestWebUrl:
    def test_prod(self):
        assert web_url("https://api.mechbench.ai") == "https://mechbench.ai/settings/runners"

    def test_dev(self):
        assert web_url("http://localhost:3000") == "http://localhost:5173/settings/runners"

    def test_unknown_host_guesses_by_dropping_the_api_label(self):
        assert web_url("https://api.example.test") == "https://example.test/settings/runners"
