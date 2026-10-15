from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from mechbench import cli
from mechbench_runner import crashes, service
from mechbench_runner.supervisor import Supervisor

FRAMEWORK = ("/Library/Frameworks/Python.framework/Versions/3.11/Resources/"
             "Python.app/Contents/MacOS/Python")
STANDALONE = ("/Users/u/.local/share/uv/python/cpython-3.14.7-macos-aarch64-none/"
              "bin/python3.14")
REPORTS = Path.home() / "Library" / "Logs" / "DiagnosticReports"
ROOT = Path(crashes.__file__).resolve().parents[1]


def aborting_child(stderr: str) -> list[str]:
    return [sys.executable, "-c",
            "import os, sys\n"
            "from mechbench_runner.crashes import keep_from_crash_reporter\n"
            "keep_from_crash_reporter()\n"
            f"sys.stderr.write({stderr!r}); sys.stderr.flush()\n"
            "os.abort()\n"]


@pytest.fixture()
def child_imports_this_tree(monkeypatch):
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join(
        [str(ROOT), *filter(None, [os.environ.get("PYTHONPATH")])]))


class TestAppBundlePython:
    def test_a_framework_build_runs_as_an_app_and_is_named(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "darwin")
        monkeypatch.setattr(crashes, "interpreter_image", lambda: FRAMEWORK)
        assert crashes.app_bundle_python() == FRAMEWORK
        problem = crashes.app_python_problem()
        assert FRAMEWORK in problem and "--managed-python" in problem

    def test_a_standalone_python_is_not_an_app(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "darwin")
        monkeypatch.setattr(crashes, "interpreter_image", lambda: STANDALONE)
        monkeypatch.setattr(crashes.sysconfig, "get_config_var", lambda _name: "")
        assert crashes.app_bundle_python() is None
        assert crashes.app_python_problem() is None

    def test_only_macos_has_app_bundles(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "linux")
        monkeypatch.setattr(crashes, "interpreter_image", lambda: FRAMEWORK)
        assert crashes.app_bundle_python() is None

    @pytest.mark.skipif(sys.platform != "darwin", reason="proc_pidpath is macOS's")
    def test_the_image_is_the_running_binary_not_the_venv_link(self):
        image = crashes.interpreter_image()
        assert os.path.isabs(image) and os.path.exists(image)
        assert (".app/Contents/MacOS/" in image) == bool(
            crashes.sysconfig.get_config_var("PYTHONFRAMEWORK"))


class TestCrashReporter:
    def test_the_switch_is_off_off_macos_and_when_the_developer_asks(self, monkeypatch):
        monkeypatch.setenv(crashes.SYSTEM_REPORTS_ENV, "1")
        assert crashes.keep_from_crash_reporter() is False
        monkeypatch.delenv(crashes.SYSTEM_REPORTS_ENV)
        monkeypatch.setattr(sys, "platform", "linux")
        assert crashes.keep_from_crash_reporter() is False

    @pytest.mark.skipif(sys.platform != "darwin", reason="ReportCrash is macOS's")
    def test_a_child_kept_from_the_crash_reporter_aborts_without_a_report_or_dialog(
            self):
        proc = subprocess.Popen([sys.executable, "-c", "import os; os.abort()"],
                                preexec_fn=crashes.keep_from_crash_reporter,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        assert proc.wait(timeout=60) == -signal.SIGABRT
        deadline = time.monotonic() + 6.0
        while time.monotonic() < deadline:
            for path in REPORTS.glob("*.ips"):
                try:
                    text = path.read_text(errors="replace")
                except OSError:
                    continue
                assert f'"pid" : {proc.pid},' not in text
            time.sleep(0.5)


class TestTail:
    def test_it_echoes_everything_and_keeps_the_last_lines(self, tmp_path):
        r, w = os.pipe()
        echo = tmp_path / "echo"
        with echo.open("wb") as sink:
            tail = crashes.Tail(os.fdopen(r, "rb"), echo_fd=sink.fileno(), lines=3)
            os.write(w, b"one\ntwo\nthr")
            os.write(w, b"ee\nfour\nfive without a newline")
            os.close(w)
            tail.close()
        assert tail.text() == "three\nfour\nfive without a newline"
        assert echo.read_bytes() == b"one\ntwo\nthree\nfour\nfive without a newline"


class TestTheNote:
    def test_a_death_by_signal_is_noted_once(self):
        crash = crashes.record(-signal.SIGABRT, "libc++abi: terminating")
        assert crash.name == "SIGABRT" and not crash.stopped
        taken = crashes.take()
        assert (taken.signum, taken.tail) == (signal.SIGABRT, "libc++abi: terminating")
        assert crashes.take() is None

    def test_an_exit_with_a_code_is_not_a_crash(self):
        assert crashes.record(1, "Traceback") is None
        assert crashes.take() is None

    def test_the_message_names_the_signal_and_keeps_the_end_of_a_long_tail(self):
        lines = [f"line {i:04d} " + "x" * 60 for i in range(200)]
        crash = crashes.Crash(signum=signal.SIGSEGV, tail="\n".join(lines), at=0.0)
        message = crash.job_message()
        assert len(message) <= crashes.MESSAGE_LIMIT
        assert message.startswith("the runner's process died of SIGSEGV, a memory")
        assert message.endswith(lines[-1])
        assert message.split("Its last output:\n", 1)[1].startswith("line ")

    def test_a_silent_death_says_so(self):
        crash = crashes.Crash(signum=signal.SIGKILL, tail="", at=0.0)
        assert crash.job_message().endswith(
            "It wrote nothing to stderr before it died.")


class TestTheSupervisorNotesItsChildsDeath:
    def test_an_abort_is_noted_with_the_signal_and_the_last_output(
            self, child_imports_this_tree, capfd):
        sup = Supervisor(
            argv=aborting_child("Fatal Python error: Aborted\nlast words\n"))
        assert sup._run_child() == -signal.SIGABRT  # noqa: SLF001
        crash = crashes.take()
        assert crash.name == "SIGABRT"
        assert crash.tail.endswith("Fatal Python error: Aborted\nlast words")
        assert "last words" in capfd.readouterr().err

    def test_a_stop_the_supervisor_asked_for_is_not_a_crash(
            self, child_imports_this_tree):
        sup = Supervisor(argv=aborting_child("bye\n"))
        sup._stopping = True  # noqa: SLF001
        assert sup._run_child() == -signal.SIGABRT  # noqa: SLF001
        assert crashes.take() is None

    def test_a_clean_exit_leaves_no_note(self):
        sup = Supervisor(argv=[sys.executable, "-c", "import sys; sys.exit(75)"])
        assert sup._run_child() == 75  # noqa: SLF001
        assert crashes.take() is None


class TestTheJobProcess:
    def test_it_keeps_backends_apart_and_names_an_app_python(self, monkeypatch):
        backends = pytest.importorskip("mechbench_compute.backends")
        if not hasattr(backends, "keep_apart"):
            pytest.skip("this mechbench-compute has no keep_apart")
        enabled = []
        monkeypatch.setattr(crashes.faulthandler, "enable",
                            lambda **kw: enabled.append(kw))
        monkeypatch.setattr(crashes, "keep_from_crash_reporter", lambda: True)
        monkeypatch.setattr(crashes, "app_python_problem", lambda: "an app bundle")
        monkeypatch.setattr(backends, "_apart", False)
        said = []
        crashes.prepare_job_process(say=said.append)
        assert backends._apart is True  # noqa: SLF001
        assert enabled and enabled[0]["all_threads"] is True
        assert said == ["[runner] warning: an app bundle"]


class TestInstallService:
    def test_an_app_python_is_refused_by_name(self, monkeypatch, capsys):
        monkeypatch.setattr(crashes, "app_python_problem",
                            lambda: f"this Python runs as an app bundle ({FRAMEWORK})")
        monkeypatch.delenv(crashes.ALLOW_APP_PYTHON_ENV, raising=False)
        monkeypatch.setattr(service, "install", lambda: pytest.fail("installed"))
        assert cli.main(["install-service"]) == 1
        err = capsys.readouterr().err
        assert FRAMEWORK in err and crashes.ALLOW_APP_PYTHON_ENV in err

    def test_the_developer_can_insist(self, monkeypatch):
        monkeypatch.setattr(crashes, "app_python_problem", lambda: "an app bundle")
        monkeypatch.setenv(crashes.ALLOW_APP_PYTHON_ENV, "1")
        monkeypatch.setattr(service, "install", lambda: SimpleNamespace(
            path="/x.plist", detail="loaded", loaded=True))
        monkeypatch.setattr(service, "linger_hint", lambda: None)
        assert cli.main(["install-service"]) == 0


class TestTheReleaseGate:
    def test_every_step_runs_kept_from_the_crash_reporter(self, monkeypatch):
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "release_gate", ROOT / "scripts" / "release.py")
        gate = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(gate)
        order: list[str] = []

        class StopError(Exception):
            pass

        def first_step(cmd, **_kw):
            order.append("step")
            raise StopError

        monkeypatch.setattr(crashes, "keep_from_crash_reporter",
                            lambda: order.append("kept"))
        monkeypatch.setattr(gate, "run", first_step)
        monkeypatch.setattr(gate, "check_changelog", lambda _v: None)
        monkeypatch.setattr(sys, "argv", ["release.py", "--check"])
        with pytest.raises(StopError):
            gate.main()
        assert order == ["kept", "step"]


class TestTailHeldOpen:
    def test_a_pipe_a_grandchild_still_holds_is_left_open_for_its_reader(self):
        r, w = os.pipe()
        stream = os.fdopen(r, "rb")
        tail = crashes.Tail(stream, echo_fd=os.open(os.devnull, os.O_WRONLY))
        os.write(w, b"still talking\n")
        tail.close(timeout=0.2)
        assert not stream.closed
        os.close(w)
        tail.close()
        assert stream.closed and tail.text() == "still talking"
