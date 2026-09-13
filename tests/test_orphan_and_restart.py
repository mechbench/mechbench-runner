"""No orphans, and a restart that proves it restarted (task 000462).

The incident these come from: a supervisor was SIGKILLed by launchd while
waiting out a 40-minute job, its child was re-parented to pid 1, and that
orphan went on holding the control socket and answering `status` with a
compute version nobody had installed any more. Every later `restart`
reported success — it asked the service manager whether SOMETHING was
running, and something was — while each new supervisor's child died
on the held socket with exit 1, which both supervisors read as "come
back", forever, into a log nobody was watching.

Four properties close it: the supervisor always outlives its child, a
child notices being orphaned, a child that cannot have the socket stops
deliberately, and a restart is judged by a CHANGED pid.
"""

from __future__ import annotations

import os
import subprocess
import sys

import pytest

from mechbench_runner import service, supervisor
from mechbench_runner.exits import EXIT_CRASH, EXIT_OK
from mechbench_runner.supervisor import Supervisor


class TestTheSupervisorOutlivesItsChild:
    def test_the_grace_period_beats_the_service_managers_kill(self):
        # The race that made the orphan: both waits were 300s, so launchd
        # killed us while we were still waiting on the child.
        assert supervisor.STOP_GRACE < service.STOP_TIMEOUT_SECONDS

    def test_a_real_child_is_reaped_when_the_supervisor_returns(self):
        # A child that ignores SIGTERM, as the runner does while it
        # finishes a job. The supervisor must still not leave it behind.
        sup = Supervisor(argv=[
            sys.executable, "-c",
            "import signal,time\n"
            "signal.signal(signal.SIGTERM, lambda *a: None)\n"
            "time.sleep(60)\n",
        ])
        proc = subprocess.Popen(sup.child_argv)  # noqa: S603
        sup._child = proc
        # A one-second grace for the test; the module default is minutes.
        original = supervisor.STOP_GRACE
        supervisor.STOP_GRACE = 1.0
        try:
            sup._stop_child("test")
        finally:
            supervisor.STOP_GRACE = original
        assert proc.poll() is not None, "the child survived the supervisor"

    def test_every_exit_path_reaps(self, monkeypatch):
        """`run()` returning for ANY reason stops the child — the crash
        limit, an exception, a deliberate stop."""
        stopped: list[str] = []
        monkeypatch.setattr(Supervisor, "_stop_child",
                            lambda self, why: stopped.append(why))

        class Boom(Supervisor):
            def _supervise(self):
                raise RuntimeError("unhandled")

        sup = Boom(argv=["/bin/true"])
        monkeypatch.setattr(sup, "_install_signal_handlers", lambda: None)
        with pytest.raises(RuntimeError):
            sup.run()
        assert stopped, "an exception escaped without reaping the child"

    def test_the_child_is_told_who_owns_it(self, monkeypatch):
        seen: dict = {}

        class FakePopen:
            def __init__(self, argv, env=None):
                seen["env"] = env or {}
            def wait(self):
                return EXIT_OK
            def poll(self):
                return EXIT_OK

        monkeypatch.setattr(subprocess, "Popen", FakePopen)
        Supervisor(argv=["/bin/true"])._run_child()
        assert seen["env"][supervisor.SUPERVISOR_PID_ENV] == str(os.getpid())


class TestOrphanDetection:
    def test_unsupervised_is_not_orphaned(self, monkeypatch):
        monkeypatch.delenv(supervisor.SUPERVISOR_PID_ENV, raising=False)
        assert supervisor.supervisor_pid() is None
        assert supervisor.orphaned() is False

    def test_a_live_parent_is_not_orphaned(self, monkeypatch):
        monkeypatch.setenv(supervisor.SUPERVISOR_PID_ENV, str(os.getppid()))
        assert supervisor.orphaned() is False

    def test_a_vanished_parent_is_orphaned(self, monkeypatch):
        monkeypatch.setenv(supervisor.SUPERVISOR_PID_ENV, str(os.getppid() + 99999))
        assert supervisor.orphaned() is True

    def test_a_junk_value_is_not_supervised(self, monkeypatch):
        monkeypatch.setenv(supervisor.SUPERVISOR_PID_ENV, "not-a-pid")
        assert supervisor.supervisor_pid() is None
        assert supervisor.orphaned() is False

    def test_the_status_snapshot_carries_it(self, monkeypatch):
        from mechbench_runner.control import RunnerState

        monkeypatch.setenv(supervisor.SUPERVISOR_PID_ENV, str(os.getppid() + 99999))
        snap = RunnerState(version="1.2.3", api_url="http://x").snapshot()
        assert snap["orphaned"] is True and snap["supervised"] is True
        assert snap["pid"] == os.getpid()
        monkeypatch.delenv(supervisor.SUPERVISOR_PID_ENV)
        snap2 = RunnerState(version="1.2.3", api_url="http://x").snapshot()
        assert snap2["orphaned"] is False and snap2["supervised"] is False


class TestASecondRunnerStopsDeliberately:
    def test_a_live_holder_is_a_deliberate_exit_not_a_crash(self, monkeypatch, capsys):
        """Exit 0, not 1. Both supervisors restart on non-zero, so the old
        `SystemExit(<message>)` (which exits 1) was a hot loop."""
        from mechbench_runner import job_runner as jr

        monkeypatch.setattr(jr, "probe", lambda: {"pid": 4242})
        runner = jr.JobRunner.__new__(jr.JobRunner)
        with pytest.raises(SystemExit) as exc:
            jr.JobRunner._claim_control_socket(runner)
        assert exc.value.code == EXIT_OK
        assert exc.value.code != EXIT_CRASH
        # and it names the holder, so the person can go look
        assert "4242" in capsys.readouterr().err

    def test_a_dead_socket_is_adopted(self, monkeypatch, tmp_path, capsys):
        from mechbench_runner import job_runner as jr

        sock = tmp_path / "runner.sock"
        sock.write_text("")
        monkeypatch.setattr(jr, "probe", lambda: None)
        monkeypatch.setattr(jr, "socket_path", lambda: sock)
        runner = jr.JobRunner.__new__(jr.JobRunner)
        jr.JobRunner._claim_control_socket(runner)  # no raise
        assert not sock.exists()
        assert "stale" in capsys.readouterr().out


class TestRestartProvesItself:
    """`restart()` is judged by a CHANGED pid, never by liveness."""

    def _service(self, monkeypatch, pids, rc=0):
        """A service whose unit exists, whose restart command returns `rc`,
        and whose control socket reports `pids` in turn."""
        monkeypatch.setattr(service, "unit_path",
                            lambda: __import__("pathlib").Path("/tmp/unit.plist"))
        monkeypatch.setattr(service.Path, "exists", lambda self: True)
        monkeypatch.setattr(service, "is_macos", lambda: True)
        monkeypatch.setattr(service, "_run",
                            lambda cmd, check, timeout=30: subprocess.CompletedProcess(
                                cmd, rc, "", ""))
        monkeypatch.setattr(
            service, "status",
            lambda: service.ServiceStatus(True, True, True, service.unit_path(),
                                          "running"))
        seq = list(pids)
        monkeypatch.setattr(service, "serving_pid",
                            lambda: seq.pop(0) if len(seq) > 1 else seq[0])

    def test_a_changed_pid_is_success(self, monkeypatch):
        self._service(monkeypatch, [100, 200])
        st = service.restart(settle=5)
        assert st.running is True
        assert "200" in st.detail and "was 100" in st.detail

    def test_the_same_pid_is_not_success(self, monkeypatch):
        # The old bug: something was running, so it reported success.
        self._service(monkeypatch, [100])
        st = service.restart(settle=2)
        assert "still serving" in st.detail
        assert "--force" in st.detail

    def test_nothing_answering_is_reported(self, monkeypatch):
        self._service(monkeypatch, [100, None])
        st = service.restart(settle=2)
        assert "nothing is answering" in st.detail

    def test_a_missing_unit_says_so(self, monkeypatch):
        monkeypatch.setattr(service.Path, "exists", lambda self: False)
        st = service.restart(settle=1)
        assert st.installed is False and "install-service" in st.detail


class TestForceEscalates:
    """`--force` interrupts the job on the SERVER, then escalates by pid:
    SIGTERM means 'finish the job first' by contract, so politeness alone
    cannot make a forced restart happen."""

    def test_it_interrupts_then_kills_then_restarts(self, monkeypatch, capsys):
        from mechbench import cli

        killed: list[int] = []
        interrupted: list[str] = []
        restarts: list[int] = []

        class FakeApi:
            def __init__(self, config): pass
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def interrupt_job(self, job_id, message, timeout=None):
                interrupted.append(job_id)

        monkeypatch.setattr("mechbench_runner.api_client.ApiClient", FakeApi)
        monkeypatch.setattr("mechbench_runner.control.request",
                            lambda op, **kw: {"pid": 777, "job": {"id": "j_x"},
                                              "phase": "executing",
                                              "compute_version": "9.9.9"})
        # The first restart leaves pid 777 serving; after the kill, 888.
        serving = [777, 777, 888]
        monkeypatch.setattr("mechbench_runner.service.serving_pid",
                            lambda: serving[0] if len(serving) == 1 else serving.pop(0))

        def fake_restart(**kw):
            restarts.append(1)
            return service.ServiceStatus(True, True, True,
                                         __import__("pathlib").Path("/tmp/u"),
                                         "running (pid 888)")
        monkeypatch.setattr("mechbench_runner.service.restart", fake_restart)
        monkeypatch.setattr(os, "kill", lambda pid, sig: killed.append(pid))
        monkeypatch.setattr("time.sleep", lambda s: None)

        rc = cli.main(["restart", "--force"])
        assert rc == 0
        assert interrupted == ["j_x"], "the job was not handed back to the server"
        assert killed == [777], "the unyielding pid was not stopped"
        assert len(restarts) == 2, "it did not restart after escalating"

    def test_without_force_a_busy_runner_is_refused_with_the_reason(
            self, monkeypatch, capsys):
        from mechbench import cli

        monkeypatch.setattr("mechbench_runner.control.request",
                            lambda op, **kw: {"pid": 777, "job": {"id": "j_y"},
                                              "phase": "executing"})
        rc = cli.main(["restart"])
        assert rc == 1
        err = capsys.readouterr().err
        assert "j_y" in err and "finish it first" in err and "--force" in err
