from __future__ import annotations

import asyncio
import os
import subprocess
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from mechbench_runner import channel, service
from mechbench_runner.config import Config
from mechbench_runner.control import RunnerState

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
