from __future__ import annotations

import io
import subprocess

import pytest

from mechbench_runner import api_client, login, redact
from mechbench_runner.config import ApiUrlError, Config, check_api_url


class TestRedact:
    def test_values_given_and_remembered_are_masked_longest_first(self):
        redact.remember("runner-key-remembered-1")
        text = redact.redact("a plain-value-0001 b runner-key-remembered-1 c",
                             {"p": {"k": "plain-value-0001", "short": "abc"}})
        assert text == "a [redacted] b [redacted] c"

    def test_short_values_are_not_masked(self):
        assert redact.redact("abc", {"k": "abc"}) == "abc"

    @pytest.mark.parametrize("secret", [
        "hf_abcdefgh12345678", "mbk_abcdefgh1234", "mbr_abcdefgh1234",
        "sk-ant-api03-abcdefghijklmnop", "ghp_abcdefghijklmnop1234",
        "AKIAABCDEFGHIJKLMNOP", "xoxb-1234567890-abc",
    ])
    def test_known_key_shapes_are_masked(self, secret):
        assert secret not in redact.redact(f"failed with {secret} here")

    def test_a_bearer_header_is_masked(self):
        assert redact.redact("Authorization: Bearer abc.def-ghi_jkl") == \
            "Authorization: Bearer [redacted]"

    def test_secret_valued_environment_is_masked(self, monkeypatch):
        monkeypatch.setenv("SOME_PROVIDER_API_KEY", "env-held-secret-999")
        assert "env-held-secret" not in redact.redact("got env-held-secret-999")


class TestChildEnv:
    def test_keys_and_tokens_stay_with_the_runner(self):
        env = redact.child_env({
            "PATH": "/usr/bin", "HOME": "/h", "MECHBENCH_API_KEY": "mbk_x",
            "HF_TOKEN": "hf_x", "OPENAI_API_KEY": "sk-x", "AWS_SECRET_ACCESS_KEY": "s",
            "GITHUB_TOKEN": "g", "UV_INDEX_PRIVATE_PASSWORD": "kept",
            "TOKENIZERS_PARALLELISM": "false", "MECHBENCH_API_URL": "https://x",
        })
        assert env == {"PATH": "/usr/bin", "HOME": "/h",
                       "UV_INDEX_PRIVATE_PASSWORD": "kept",
                       "TOKENIZERS_PARALLELISM": "false",
                       "MECHBENCH_API_URL": "https://x"}

    def test_the_package_manager_runs_without_the_runner_key(self, monkeypatch):
        import importlib

        from mechbench_runner import install

        seen = {}
        real = importlib.reload(install)._run
        monkeypatch.setenv("MECHBENCH_API_KEY", "mbk_should_not_pass")

        def fake(cmd, **kw):
            seen.update(kw)
            return subprocess.CompletedProcess(cmd, 0, "", "")

        monkeypatch.setattr(subprocess, "run", fake)
        real(["uv", "--version"], timeout=1.0)
        assert "MECHBENCH_API_KEY" not in seen["env"]
        assert "PATH" in seen["env"]


class TestApiUrl:
    @pytest.mark.parametrize("url", [
        "https://api.mechbench.ai", "http://localhost:3000", "http://127.0.0.1:3000",
        "http://[::1]:3000",
    ])
    def test_https_or_a_local_http(self, url):
        assert check_api_url(url) == url

    @pytest.mark.parametrize("url", [
        "http://api.mechbench.ai", "ftp://api.mechbench.ai", "api.mechbench.ai",
        "https://user:pw@api.mechbench.ai", "http://localhost.evil.example",
    ])
    def test_anything_else_is_refused(self, url):
        with pytest.raises(ApiUrlError):
            check_api_url(url)

    def test_the_environment_is_checked(self, monkeypatch):
        monkeypatch.setenv("MECHBENCH_API_KEY", "mbk_k")
        monkeypatch.setenv("MECHBENCH_API_URL", "http://api.example.com")
        with pytest.raises(ApiUrlError):
            Config.from_env()

    def test_a_stored_http_credential_is_ignored(self, monkeypatch, capsys):
        from mechbench_runner import credentials

        monkeypatch.delenv("MECHBENCH_API_KEY", raising=False)
        monkeypatch.delenv("MECHBENCH_API_URL", raising=False)
        credentials.save(credentials.StoredCredentials(
            api_url="http://api.example.com", api_key="mbk_stored"))
        config = Config.from_env()
        assert config.api_key is None
        assert "ignoring the stored credential" in capsys.readouterr().err


class TestUploadGrant:
    @pytest.mark.parametrize("url", [
        "https://mb-results.s3.us-east-1.amazonaws.com/k?sig=1",
        "https://s3.us-east-1.amazonaws.com/mb-results/k?sig=1",
        "http://127.0.0.1:9000/k",
    ])
    def test_s3_or_local(self, url):
        assert api_client.check_upload_url(url) == url

    @pytest.mark.parametrize("url", [
        "http://mb-results.s3.us-east-1.amazonaws.com/k",
        "https://evil.example/k", "https://amazonaws.com.evil.example/k",
        "https://evilamazonaws.com/k",
    ])
    def test_anywhere_else_is_refused_before_a_byte_is_sent(self, url, monkeypatch):
        sent = []
        monkeypatch.setattr(api_client.httpx, "put", lambda *a, **k: sent.append(a))
        with pytest.raises(RuntimeError, match="refused an upload grant"):
            api_client.ApiClient.upload_to_grant(
                {"upload": {"url": url, "headers": {}}}, b"x")
        assert sent == []


class TestLogin:
    def test_a_dash_reads_the_token_from_stdin(self, monkeypatch):
        monkeypatch.setattr("sys.stdin", io.StringIO("mbr_fromstdin\n"))
        assert login.read_token("-") == "mbr_fromstdin"

    def test_the_environment_supplies_an_omitted_token(self, monkeypatch):
        monkeypatch.setenv("MECHBENCH_REGISTRATION_TOKEN", "mbr_fromenv")
        assert login.read_token(None) == "mbr_fromenv"
        assert login.read_token("mbr_given") == "mbr_given"

    @pytest.mark.parametrize(("uri", "ok"), [
        ("https://mechbench.ai/connect?code=AB-CD", True),
        ("https://evil.example/connect?code=AB-CD", False),
        ("http://mechbench.ai/connect", False),
        ("javascript:alert(1)", False),
        ("file:///etc/passwd", False),
    ])
    def test_only_a_link_on_the_apis_website_is_opened(self, uri, ok):
        assert login.same_web_origin(uri, "https://api.mechbench.ai") is ok

    def test_a_foreign_link_is_printed_and_never_opened(self, monkeypatch, capsys):
        import webbrowser

        opened = []
        monkeypatch.setattr(webbrowser, "open", opened.append)
        monkeypatch.setattr(api_client, "start_device_auth", lambda *a, **k: {
            "verificationUri": "https://evil.example/x", "deviceCode": "d",
            "intervalSeconds": 0, "expiresAt": "2000-01-01T00:00:00Z"})
        monkeypatch.setattr("sys.stdin.isatty", lambda: True, raising=False)
        monkeypatch.setattr("builtins.input", lambda *_a: "")
        config = Config(api_base_url="https://api.mechbench.ai", api_key=None,
                        poll_interval_seconds=1.0, warm_model_id=None)
        assert login.login(config) == 1
        assert opened == []
        assert "not opened for you" in capsys.readouterr().out
