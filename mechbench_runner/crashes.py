from __future__ import annotations

import ctypes
import faulthandler
import json
import os
import signal
import sys
import sysconfig
import threading
import time
from collections import deque
from contextlib import suppress
from dataclasses import dataclass
from typing import IO

from . import paths

SYSTEM_REPORTS_ENV = "MECHBENCH_SYSTEM_CRASH_REPORTS"
ALLOW_APP_PYTHON_ENV = "MECHBENCH_ALLOW_APP_PYTHON"
NOTE_NAME = "last-crash.json"
TAIL_LINES = 40
MESSAGE_LIMIT = 2000

EXC_MASK_CRASH = 1 << 10
EXC_MASK_CORPSE_NOTIFY = 1 << 13
EXCEPTION_DEFAULT = 1

STOPS = frozenset({signal.SIGTERM, signal.SIGINT, signal.SIGHUP})

MEANING = {
    signal.SIGABRT: "an abort: native code refused to go on",
    signal.SIGSEGV: "a memory fault in native code",
    signal.SIGBUS: "a memory fault in native code",
    signal.SIGILL: "an illegal instruction in native code",
    signal.SIGFPE: "an arithmetic fault in native code",
    signal.SIGKILL: "killed outright, as the system does when memory runs out",
}


def _on(name: str) -> bool:
    return os.environ.get(name, "") not in ("", "0")


def interpreter_image() -> str:
    if sys.platform == "darwin":
        with suppress(OSError, AttributeError, UnicodeDecodeError):
            buf = ctypes.create_string_buffer(4096)
            if ctypes.CDLL(None).proc_pidpath(os.getpid(), buf, len(buf)) > 0:
                return buf.value.decode()
    return os.path.realpath(sys.executable)


def app_bundle_python() -> str | None:
    if sys.platform != "darwin":
        return None
    image = interpreter_image()
    if ".app/Contents/MacOS/" in image or sysconfig.get_config_var("PYTHONFRAMEWORK"):
        return image
    return None


def allowed_app_python() -> bool:
    return _on(ALLOW_APP_PYTHON_ENV)


def app_python_problem() -> str | None:
    image = app_bundle_python()
    if image is None:
        return None
    return (f"this Python runs as an app bundle ({image}), a python.org or Homebrew "
            f"framework build. macOS reports a crash in an app with a \"Python quit "
            f"unexpectedly\" dialog. The runner installs into a standalone Python: "
            f"`uv tool install --managed-python mechbench`")


def keep_from_crash_reporter() -> bool:
    if sys.platform != "darwin" or _on(SYSTEM_REPORTS_ENV):
        return False
    try:
        libc = ctypes.CDLL(None)
        task = ctypes.c_uint.in_dll(libc, "mach_task_self_").value
        setter = libc.task_set_exception_ports
        setter.argtypes = [ctypes.c_uint, ctypes.c_uint, ctypes.c_uint,
                           ctypes.c_int, ctypes.c_int]
        setter.restype = ctypes.c_int
        mask = EXC_MASK_CRASH | EXC_MASK_CORPSE_NOTIFY
        return setter(task, mask, 0, EXCEPTION_DEFAULT, 0) == 0
    except (OSError, AttributeError, ValueError):
        return False


def prepare_job_process(say=print) -> None:
    keep_from_crash_reporter()
    stream = sys.__stderr__
    if stream is not None:
        with suppress(AttributeError, OSError, ValueError, RuntimeError):
            faulthandler.enable(file=stream, all_threads=True)
    try:
        from mechbench_compute import backends
    except ImportError:
        backends = None
    keep_apart = getattr(backends, "keep_apart", None)
    if keep_apart is not None:
        keep_apart()
    problem = app_python_problem()
    if problem is not None:
        say(f"[runner] warning: {problem}")


class Tail:
    def __init__(self, stream: IO[bytes] | None, *, echo_fd: int = 2,
                 lines: int = TAIL_LINES) -> None:
        self._stream = stream
        self._echo_fd = echo_fd
        self._lines: deque[str] = deque(maxlen=lines)
        self._partial = b""
        self._thread: threading.Thread | None = None
        if stream is not None:
            self._thread = threading.Thread(target=self._pump, daemon=True,
                                            name="child-stderr")
            self._thread.start()

    def _pump(self) -> None:
        fd = self._stream.fileno()
        while True:
            try:
                chunk = os.read(fd, 65536)
            except OSError:
                break
            if not chunk:
                break
            with suppress(OSError):
                os.write(self._echo_fd, chunk)
            self._take(chunk)
        if self._partial:
            self._lines.append(self._partial.decode("utf-8", "replace"))
            self._partial = b""

    def _take(self, chunk: bytes) -> None:
        *done, self._partial = (self._partial + chunk).split(b"\n")
        for line in done:
            self._lines.append(line.decode("utf-8", "replace").rstrip("\r"))

    def close(self, timeout: float = 2.0) -> None:
        if self._thread is not None:
            self._thread.join(timeout)
            if self._thread.is_alive():
                return
        if self._stream is not None:
            with suppress(OSError):
                self._stream.close()

    def text(self) -> str:
        return "\n".join(line for line in self._lines if line.strip())


@dataclass(frozen=True)
class Crash:
    signum: int
    tail: str
    at: float

    @property
    def name(self) -> str:
        try:
            return signal.Signals(self.signum).name
        except ValueError:
            return f"signal {self.signum}"

    @property
    def stopped(self) -> bool:
        return self.signum in STOPS

    def job_message(self, limit: int = MESSAGE_LIMIT) -> str:
        meaning = MEANING.get(self.signum)
        died = f"{self.name}{f', {meaning}' if meaning else ''}"
        head = (f"the runner's process died of {died}, while it held this job; "
                f"the job could not report anything itself.")
        if not self.tail:
            return f"{head} It wrote nothing to stderr before it died."
        head = f"{head} Its last output:\n"
        tail = self.tail[-max(limit - len(head), 0):]
        if len(tail) < len(self.tail) and "\n" in tail:
            tail = tail.split("\n", 1)[1]
        return head + tail


def note_path():
    return paths.mechbench_dir() / NOTE_NAME


def record(returncode: int, tail: str) -> Crash | None:
    if returncode >= 0:
        return None
    crash = Crash(signum=-returncode, tail=tail[-4000:], at=time.time())
    with suppress(OSError):
        note_path().write_text(json.dumps(
            {"signal": crash.signum, "tail": crash.tail, "at": crash.at}))
    return crash


def take() -> Crash | None:
    path = note_path()
    try:
        body = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    with suppress(OSError):
        path.unlink()
    try:
        return Crash(signum=int(body["signal"]), tail=str(body.get("tail") or ""),
                     at=float(body.get("at") or 0.0))
    except (KeyError, TypeError, ValueError):
        return None
