"""Installing the runner as a service (task 000293).

What is worth asserting here is the *policy* written into the unit,
because that policy is the whole feature: it is how the exit contract
in 000294 becomes behaviour. The install itself is a file write and two
subprocess calls, and is exercised against fakes rather than against the
machine running the tests.
"""

from __future__ import annotations

import plistlib
import subprocess
import sys

import pytest

from mechbench_runner import service


@pytest.fixture()
def fake_launchctl(monkeypatch):
    calls: list[list[str]] = []

    def run(cmd, *, check):  # noqa: ARG001
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(service, "_run", run)
    return calls


class TestPolicy:
    """The plist and the unit are where the exit contract becomes real."""

    def test_launchd_restarts_only_on_a_non_zero_exit(self):
        spec = service.launchd_plist()
        # The half that matters most: without SuccessfulExit=false, a
        # runner that exits 0 because it is signed out gets restarted
        # forever against the throttle.
        assert spec["KeepAlive"] == {"SuccessfulExit": False}

    def test_launchd_throttles(self):
        assert int(service.launchd_plist()["ThrottleInterval"]) >= 5

    def test_launchd_starts_at_login(self):
        assert service.launchd_plist()["RunAtLoad"] is True

    def test_systemd_says_the_same_thing_in_its_own_words(self):
        unit = service.systemd_unit()
        assert "Restart=on-failure" in unit
        assert f"RestartSec={service.THROTTLE_SECONDS}" in unit
        # Give up loudly rather than spinning.
        assert "StartLimitBurst" in unit

    def test_both_allow_a_job_to_finish_on_stop(self):
        assert int(service.launchd_plist()["ExitTimeOut"]) >= 60
        expected = f"TimeoutStopSec={service.STOP_TIMEOUT_SECONDS}"
        assert expected in service.systemd_unit()


class TestTheCommand:
    def test_it_pins_the_interpreter_not_the_path(self):
        args = service.program_arguments()
        # A service has no shell profile, so PATH is not ours to rely on;
        # and the interpreter pins the environment holding the deps.
        assert args[0] == sys.executable
        # `supervise`, not `run`: the supervisor owns the child and can
        # upgrade it while it is stopped (000295/000296).
        assert args[1:] == ["-m", "mechbench_runner.cli", "supervise"]

    def test_no_credential_is_written_into_the_unit(self):
        rendered = plistlib.dumps(service.launchd_plist()).decode()
        assert "mbk_" not in rendered
        assert "MECHBENCH_API_KEY" not in rendered
        assert "MECHBENCH_API_KEY" not in service.systemd_unit()


class TestInstall:
    def test_it_writes_and_loads(self, tmp_path, monkeypatch, fake_launchctl):
        path = tmp_path / "service.plist"
        monkeypatch.setattr(service, "unit_path", lambda: path)
        monkeypatch.setattr(service, "status", lambda: service.ServiceStatus(
            True, True, True, path, "running"))
        service.install()
        assert path.exists()
        verbs = [c[1] if len(c) > 1 else c[0] for c in fake_launchctl]
        assert "bootstrap" in verbs or "enable" in verbs

    def test_it_is_idempotent(self, tmp_path, monkeypatch, fake_launchctl):
        """Reinstalling over a loaded service must not leave the old
        command running beside the new one."""
        path = tmp_path / "service.plist"
        monkeypatch.setattr(service, "unit_path", lambda: path)
        monkeypatch.setattr(service, "status", lambda: service.ServiceStatus(
            True, True, True, path, "running"))
        service.install()
        if service.is_macos():
            verbs = [c[1] for c in fake_launchctl if len(c) > 1]
            assert verbs.index("bootout") < verbs.index("bootstrap")

    def test_uninstall_removes_the_file(self, tmp_path, monkeypatch, fake_launchctl):
        path = tmp_path / "service.plist"
        path.write_text("x")
        monkeypatch.setattr(service, "unit_path", lambda: path)
        st = service.uninstall()
        assert not path.exists()
        assert st.installed is False

    def test_uninstalling_nothing_says_so_rather_than_failing(
        self, tmp_path, monkeypatch, fake_launchctl
    ):
        monkeypatch.setattr(service, "unit_path", lambda: tmp_path / "absent.plist")
        assert "nothing" in service.uninstall().detail

    def test_status_of_an_absent_agent(self, tmp_path, monkeypatch):
        monkeypatch.setattr(service, "unit_path", lambda: tmp_path / "absent.plist")
        st = service.status()
        assert st.installed is False
        assert st.detail == "not installed"

    def test_kickstart_declines_when_nothing_is_installed(self, tmp_path, monkeypatch):
        monkeypatch.setattr(service, "unit_path", lambda: tmp_path / "absent.plist")
        assert service.kickstart() is False


class TestUnsupported:
    def test_an_unknown_platform_says_what_to_do_instead(self, monkeypatch):
        monkeypatch.setattr(service.sys, "platform", "plan9")
        with pytest.raises(service.UnsupportedPlatformError, match="supervise"):
            service.unit_path()


class TestUnsupportedIsHandledEverywhere:
    """The handlers name the class, so a rename that misses one turns
    "unsupported platform" into an AttributeError at the worst moment."""

    def test_login_catches_it(self, monkeypatch, capsys):
        from mechbench_runner import login as login_mod

        monkeypatch.setattr(service.sys, "platform", "plan9")
        monkeypatch.setattr(login_mod.sys.stdin, "isatty", lambda: False)
        login_mod._offer_service()  # noqa: SLF001
        assert "mechbench-runner run" in capsys.readouterr().out

    def test_the_cli_catches_it(self, monkeypatch, capsys):
        from mechbench_runner import cli

        monkeypatch.setattr(service.sys, "platform", "plan9")
        assert cli.main(["service-status"]) == 1
        assert "supervise" in capsys.readouterr().err


class TestTheOldAgentNamesStillWork:
    """`install-agent` and friends are the pre-0.5.0 spellings (000305).

    They are hidden from --help but must keep working: 0.4.0 is
    published, and a rename that breaks the command someone has in their
    notes is a rename that gets reverted.
    """

    def test_every_alias_maps_to_the_same_action(self):
        from mechbench_runner.cli import SERVICE_COMMANDS

        assert SERVICE_COMMANDS["install-agent"] == SERVICE_COMMANDS["install-service"]
        assert (
            SERVICE_COMMANDS["uninstall-agent"]
            == SERVICE_COMMANDS["uninstall-service"]
        )
        assert SERVICE_COMMANDS["agent-status"] == SERVICE_COMMANDS["service-status"]

    def test_an_alias_still_dispatches(self, monkeypatch, capsys):
        from mechbench_runner import cli

        monkeypatch.setattr(service.sys, "platform", "plan9")
        assert cli.main(["agent-status"]) == 1
        assert "supervise" in capsys.readouterr().err

    def test_the_aliases_are_hidden_from_help(self, capsys):
        from mechbench_runner import cli

        with pytest.raises(SystemExit):
            cli.main(["--help"])
        out = capsys.readouterr().out
        assert "install-service" in out
        assert "install-agent" not in out


class TestInstallReportsSettledState:
    """Install must not report a race as a failure.

    `bootstrap` returns before launchd has spawned anything, so reading
    status immediately says "loaded, not currently running" — printed
    right after someone answers "yes, start it automatically", that reads
    as though it did not work. It did; the read was just too early.
    """

    def test_it_waits_for_running(self, tmp_path, monkeypatch):
        path = tmp_path / "a.plist"
        seq = [
            service.ServiceStatus(True, True, False, path, "loaded, not running"),
            service.ServiceStatus(True, True, False, path, "loaded, not running"),
            service.ServiceStatus(True, True, True, path, "running (pid 1)"),
        ]
        monkeypatch.setattr(service, "status", lambda: seq.pop(0) if seq else seq0)
        seq0 = service.ServiceStatus(True, True, True, path, "running (pid 1)")
        st = service._settled_status(attempts=5, pause=0)  # noqa: SLF001
        assert st.running is True

    def test_it_gives_up_rather_than_hanging(self, tmp_path, monkeypatch):
        path = tmp_path / "a.plist"
        never = service.ServiceStatus(True, True, False, path, "loaded, not running")
        monkeypatch.setattr(service, "status", lambda: never)
        st = service._settled_status(attempts=3, pause=0)  # noqa: SLF001
        assert st.running is False

    def test_an_unloaded_service_returns_at_once(self, tmp_path, monkeypatch):
        # Nothing to wait for: it is not going to start on its own.
        path = tmp_path / "a.plist"
        calls = []

        def st():
            calls.append(1)
            return service.ServiceStatus(True, False, False, path, "not loaded")

        monkeypatch.setattr(service, "status", st)
        service._settled_status(attempts=9, pause=0)  # noqa: SLF001
        assert len(calls) == 1
