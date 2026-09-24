from __future__ import annotations

import asyncio
import json
import os
import signal
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from mechbench_runner import channel, install, login, service, updater
from mechbench_runner.api_client import ApiClient, ApiError
from mechbench_runner.config import Config
from mechbench_runner.control import ControlServer, RunnerState
from mechbench_runner.exits import EXIT_OK
from mechbench_runner.paths import mechbench_dir as real_mechbench_dir

SERVER_PING_SECONDS = 25
API_BODY_CAP_BYTES = 64 * 1024 * 1024


def _config() -> Config:
    return Config(
        api_base_url="https://api.example.invalid", api_key="k",
        poll_interval_seconds=0.01, warm_model_id=None, runner_id=None,
    )


class TestTheChannelDial:
    def test_wss_trusts_certifi_because_a_uv_or_python_org_python_has_no_ca_roots(self):
        ctx = channel._ssl_context("wss://api.example.invalid/runners/channel")
        assert ctx is not None
        assert ctx.cert_store_stats()["x509_ca"] > 0

    def test_the_library_keepalive_is_off_because_the_server_pings(self, monkeypatch):
        import websockets

        seen: dict[str, object] = {}

        def recording_connect(url, *a, **kw):
            seen.update(kw)
            raise RuntimeError("stop here")

        monkeypatch.setattr(websockets, "connect", recording_connect)
        ch = channel.LiveChannel(
            _config(), RunnerState(version="t", api_url="https://api.example.invalid"))
        with pytest.raises(RuntimeError):
            asyncio.run(ch._session())
        assert seen["ping_interval"] is None

    def test_silence_outlasts_the_servers_ping_cadence(self):
        assert channel.SILENCE_TIMEOUT_SECONDS > 2 * SERVER_PING_SECONDS


class TestLaunchd:
    def test_the_process_type_is_standard_because_background_runs_on_efficiency_cores(self):
        assert service.launchd_plist()["ProcessType"] == "Standard"

    def test_restart_is_kickstart_k_with_room_for_a_slow_relaunch(self, monkeypatch):
        calls: list[tuple[list[str], float]] = []
        monkeypatch.setattr(service, "unit_path", lambda: Path("/tmp/unit.plist"))
        monkeypatch.setattr(service.Path, "exists", lambda self: True)
        monkeypatch.setattr(service, "is_macos", lambda: True)

        def fake_run(cmd, check, timeout=30):
            calls.append((cmd, timeout))
            return subprocess.CompletedProcess(cmd, 0, "", "")

        monkeypatch.setattr(service, "_run", fake_run)
        monkeypatch.setattr(
            service, "status",
            lambda: service.ServiceStatus(True, True, True, service.unit_path(), "running"))
        pids = [100, 200]
        monkeypatch.setattr(service, "serving_pid",
                            lambda: pids.pop(0) if len(pids) > 1 else pids[0])
        service.restart(settle=5)
        cmd, timeout = calls[0]
        assert cmd[:3] == ["launchctl", "kickstart", "-k"]
        assert timeout > 30


class TestTheJobRunner:
    @pytest.fixture
    def jr(self):
        pytest.importorskip("mechbench_compute")
        from mechbench_runner import job_runner

        return job_runner

    def test_presigned_threshold_is_under_the_api_body_cap(self, jr):
        assert 0 < jr.PRESIGN_THRESHOLD_BYTES < API_BODY_CAP_BYTES

    def test_a_shutdown_ends_a_long_sleep_within_a_second_despite_pep_475(self, jr):
        runner = SimpleNamespace(_shutdown=False)
        threading.Timer(0.2, lambda: setattr(runner, "_shutdown", True)).start()
        started = time.monotonic()
        jr.JobRunner._sleep(runner, 30)
        assert time.monotonic() - started < 1.5

    def test_caffeinate_is_tied_to_this_pid_so_it_cannot_outlive_the_runner(
            self, jr, monkeypatch):
        launched: list[list[str]] = []
        monkeypatch.setattr(jr.shutil, "which", lambda _name: "/usr/bin/caffeinate")
        monkeypatch.setattr(jr.subprocess, "Popen",
                            lambda argv, **_kw: launched.append(argv) or object())
        runner = SimpleNamespace(_caffeinate=None)
        jr.JobRunner._hold_awake(runner)
        assert launched == [["caffeinate", "-i", "-w", str(os.getpid())]]


def _mock_client(handler) -> ApiClient:
    api = ApiClient(Config(api_base_url="http://127.0.0.1:1", api_key="mbk_test",
                           poll_interval_seconds=0.01, warm_model_id=None))
    api._client = httpx.Client(base_url="http://127.0.0.1:1",
                               headers={"authorization": "Bearer mbk_test"},
                               transport=httpx.MockTransport(handler))
    return api


class TestTheApiWire:
    def test_the_claim_advertises_remote_so_provider_only_jobs_are_offered_here(self):
        seen: dict[str, str] = {}

        def handler(req: httpx.Request) -> httpx.Response:
            seen["capabilities"] = req.url.params["capabilities"]
            return httpx.Response(204)
        assert _mock_client(handler).claim_next_job() is None
        assert set(seen["capabilities"].split(",")) >= {"mlx-local", "pure", "remote"}

    def test_the_direct_completion_is_cbor_with_its_hash_in_a_header(self):
        seen: dict[str, object] = {}

        def handler(req: httpx.Request) -> httpx.Response:
            seen.update(ctype=req.headers.get("content-type"),
                        hash=req.headers.get("x-content-hash"), body=req.content)
            return httpx.Response(200, json={"ok": True})
        _mock_client(handler).complete_job_cbor("j_1", b"\xa0", "sha256:00")
        assert seen == {"ctype": "application/cbor", "hash": "sha256:00",
                        "body": b"\xa0"}

    def test_a_finalize_declares_what_did_not_run_because_the_api_never_holds_the_bytes(
            self):
        seen: dict[str, object] = {}

        def handler(req: httpx.Request) -> httpx.Response:
            seen.update(json.loads(req.content))
            return httpx.Response(200, json={"ok": True})
        missing = {"gen": {"reason": "boom", "source": "error"}}
        _mock_client(handler).complete_job_uploaded(
            "j_1", "sha256:00", kind="run/result", missing=missing)
        assert seen == {"uploaded": True, "contentHash": "sha256:00",
                        "kind": "run/result", "missing": missing}

    def test_a_verb_query_repeats_lists_drops_none_and_hands_back_the_headers(self):
        seen: dict[str, object] = {}

        def handler(req: httpx.Request) -> httpx.Response:
            seen["query"] = req.url.params.multi_items()
            return httpx.Response(200, json=[], headers={"x-next-offset": "3"})
        _, headers = _mock_client(handler).call(
            "GET", "/objects/p/items",
            query={"where": ["a=1", "b>2"], "limit": 5, "full": True,
                   "search": None, "count": False})
        assert sorted(seen["query"]) == [("full", "1"), ("limit", "5"),
                                         ("where", "a=1"), ("where", "b>2")]
        assert headers["x-next-offset"] == "3"


class TestTheChannelHello:
    def test_the_hello_carries_what_the_api_needs_to_offer_an_update(self):
        hello = channel.LiveChannel(
            _config(), RunnerState(version="t", api_url="https://api.example.invalid")
        )._hello()
        assert set(hello["packages"]) >= {"mechbench", "mechbench-compute",
                                          "mechbench-schema"}
        assert isinstance(hello["selfUpdatable"], bool)
        assert hello["installMethod"]


class TestTheSocketPermissionsAreTheAuth:
    def test_an_existing_mechbench_dir_is_tightened_to_0700(
            self, monkeypatch, tmp_path):
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        (tmp_path / ".mechbench").mkdir(mode=0o755)
        (tmp_path / ".mechbench").chmod(0o755)
        d = real_mechbench_dir()
        assert stat.S_IMODE(d.stat().st_mode) == 0o700

    def test_the_control_socket_is_0600(self):
        import tempfile

        d = Path(tempfile.mkdtemp(prefix="mbr-", dir="/tmp"))
        server = ControlServer(RunnerState(version="t", api_url="http://localhost:3000"),
                               path=d / "runner.sock")
        server.start()
        try:
            assert stat.S_IMODE((d / "runner.sock").stat().st_mode) == 0o600
        finally:
            server.stop()
            for f in d.iterdir():
                f.unlink(missing_ok=True)
            d.rmdir()


class TestAServiceHasNoLoginPath:
    def test_a_venv_upgrades_with_this_interpreters_pip_not_one_on_path(self, tmp_path):
        (tmp_path / "pyvenv.cfg").write_text("home = /x\n")
        i = install.detect(str(tmp_path))
        assert i.method == "venv"
        assert i.upgrade[:3] == [sys.executable, "-m", "pip"]

    def test_installers_are_sought_where_uv_and_homebrew_put_them(self):
        assert {"~/.local/bin", "/opt/homebrew/bin"} <= set(install._SEARCH)


class TestNoCredentialInTheUnit:
    def test_neither_unit_carries_a_key_because_a_unit_file_lands_in_backups(self):
        plist = service.launchd_plist()
        assert "EnvironmentVariables" not in plist
        assert "MECHBENCH_API_KEY" not in json.dumps(plist, default=str)
        assert "MECHBENCH_API_KEY" not in service.systemd_unit()


class TestTheUpgradeSelfCheck:
    def test_it_fails_when_the_front_door_the_unit_execs_cannot_import(
            self, monkeypatch):
        import mechbench

        monkeypatch.setitem(sys.modules, "mechbench.cli", None)
        monkeypatch.delattr(mechbench, "cli", raising=False)
        assert any("import failed" in p for p in updater._self_check())


class TestLogin:
    def test_a_login_kicks_the_installed_service_because_it_exited_deliberately(
            self, monkeypatch):
        kicked: list[bool] = []
        monkeypatch.setattr(service, "status", lambda: service.ServiceStatus(
            True, True, False, Path("/tmp/unit"), "loaded, not currently running"))
        monkeypatch.setattr(service, "kickstart", lambda: kicked.append(True) or True)
        login._offer_service()
        assert kicked == [True]

    def test_the_approval_url_is_flushed_because_a_piped_stdout_is_block_buffered(
            self, monkeypatch):
        from mechbench_runner import api_client

        printed: list[tuple[str, dict]] = []
        def record(*a, **kw):
            printed.append((" ".join(map(str, a)), kw))
        monkeypatch.setattr(login, "print", record, raising=False)
        monkeypatch.setattr(api_client, "start_device_auth", lambda *a, **kw: {
            "deviceCode": "d", "verificationUri": "https://example.invalid/approve",
            "intervalSeconds": 0})
        monkeypatch.setattr(api_client, "poll_device_auth",
                            lambda *a, **kw: {"status": "denied"})
        monkeypatch.setattr(sys, "stdin", SimpleNamespace(isatty=lambda: False))
        monkeypatch.setattr(time, "sleep", lambda _s: None)
        assert login.login(_config()) == 1
        url_lines = [kw for text, kw in printed if "example.invalid/approve" in text]
        assert url_lines and all(kw.get("flush") for kw in url_lines)


class TestExitsTheSupervisorLeavesStopped:
    @pytest.fixture
    def runner(self, monkeypatch):
        pytest.importorskip("mechbench_compute")
        from mechbench_runner import job_runner as jr

        class StubControl:
            def __init__(self, _state, path=None):
                self.path = path or "/tmp/stub.sock"

            def start(self):
                return None

            def stop(self):
                return None

        monkeypatch.setattr(jr, "ControlServer", StubControl)
        saved = {s: signal.getsignal(s) for s in (signal.SIGINT, signal.SIGTERM)}
        r = jr.JobRunner(Config(
            api_base_url="http://127.0.0.1:1", api_key="mbk_test",
            poll_interval_seconds=0.01, warm_model_id=None,
            from_stored_credentials=True))
        monkeypatch.setattr(r, "_claim_control_socket", lambda: None)
        yield jr, r
        for s, h in saved.items():
            signal.signal(s, h)

    def _api(self, jr, monkeypatch, claim):
        class Api:
            def __enter__(self):
                return self

            def __exit__(self, *_exc):
                return None

            def list_jobs(self):
                return []

            def claim_next_job(self):
                return claim()
        monkeypatch.setattr(jr, "ApiClient", lambda *_a, **_k: Api())

    def test_a_machine_that_is_not_signed_in_exits_0(self, runner):
        _, r = runner
        r.config = Config(api_base_url="http://127.0.0.1:1", api_key=None,
                          poll_interval_seconds=0.01, warm_model_id=None)
        assert r.run() == EXIT_OK

    def test_a_revoked_key_exits_0(self, runner, monkeypatch):
        jr, r = runner

        def revoked():
            raise ApiError(401, {"code": "UNAUTHORIZED"})
        self._api(jr, monkeypatch, revoked)
        assert r.run() == EXIT_OK

    def test_sigterm_is_a_deliberate_stop_that_exits_0(self, runner, monkeypatch):
        jr, r = runner

        class StillRunningError(BaseException):
            pass

        def unhandled(*_a):
            raise StillRunningError
        claims: list[int] = []

        def terminated():
            claims.append(1)
            if len(claims) > 1:
                raise StillRunningError
            os.kill(os.getpid(), signal.SIGTERM)
            return None
        signal.signal(signal.SIGTERM, unhandled)
        self._api(jr, monkeypatch, terminated)
        assert r.run() == EXIT_OK


class TestTheJobRunnerHandsComputeWhatItNeeds:
    @pytest.fixture
    def jr(self, monkeypatch):
        pytest.importorskip("mechbench_compute")
        from mechbench_runner import job_runner

        class StubControl:
            def __init__(self, _state, path=None):
                self.path = path or "/tmp/stub.sock"

            def start(self):
                return None

            def stop(self):
                return None

        monkeypatch.setattr(job_runner, "ControlServer", StubControl)
        return job_runner

    def test_the_bench_gets_the_stored_credential_since_it_reads_only_the_env(
            self, jr, monkeypatch):
        from mechbench_compute import bench

        seen: dict[str, object] = {}
        monkeypatch.setattr(bench, "configure", lambda **kw: seen.update(kw))
        r = jr.JobRunner(_config())
        r._configure_bench()
        assert seen == {"api_url": "https://api.example.invalid", "api_key": "k"}

    def test_claim_delivered_secrets_reach_compute_and_are_disposed(
            self, jr, monkeypatch):
        r = jr.JobRunner(_config())
        got: dict[str, object] = {}

        def fake_run(spec, *, on_progress=None, secrets=None, **_kw):
            got["secrets"] = secrets
            got["copy"] = dict(secrets or {})
            got["in_spec"] = "integrations" in json.dumps(spec.extra, default=str)
            got["in_env"] = any("sk-secret" in v for v in os.environ.values())
            return {"protocol": "layer_ablation"}

        class Api:
            claim_tokens: dict[str, str] = {}

            def report_progress(self, *_a, **_k):
                return None

            def declare_preparing(self, *_a, **_k):
                return None

            def report_preparing_step(self, *_a, **_k):
                return None

            def complete_job_cbor(self, *_a, **_k):
                return None

        monkeypatch.setattr(r._executor, "run", fake_run)
        monkeypatch.setattr(r, "_hold_awake", lambda: None)
        monkeypatch.setattr(jr, "dump_canonical", lambda _p: b"\xa0")
        job = {"id": "j_s", "protocolKind": "layer_ablation",
               "spec": {"prompt": "hi", "modelId": "m@r"},
               "integrations": {"anthropic": {"token": "sk-secret"}}}
        r._handle(Api(), job)
        assert got["copy"] == {"anthropic": {"token": "sk-secret"}}
        assert got["in_spec"] is False and got["in_env"] is False
        assert got["secrets"] == {}
        assert "integrations" not in job
