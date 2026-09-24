from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from contextlib import suppress
from types import FrameType

from .exits import EXIT_CRASH, EXIT_OK, EXIT_RESTART
from .service import STOP_TIMEOUT_SECONDS

BACKOFF_MIN = 1.0
BACKOFF_MAX = 60.0

CRASH_LIMIT = 5

HEALTHY_SECONDS = 60.0

STOP_GRACE = float(STOP_TIMEOUT_SECONDS) - 60.0

SUPERVISOR_PID_ENV = "MECHBENCH_SUPERVISOR_PID"


def supervisor_pid() -> int | None:
    raw = os.environ.get(SUPERVISOR_PID_ENV, "")
    return int(raw) if raw.isdigit() else None


def orphaned() -> bool:
    parent = supervisor_pid()
    return parent is not None and os.getppid() != parent


class Supervisor:
    def __init__(self, argv: list[str] | None = None) -> None:
        self.child_argv = argv or [
            sys.executable, "-m", "mechbench.cli", "run",
        ]
        self._child: subprocess.Popen[bytes] | None = None
        self._stopping = False
        self._unproven_upgrade = False

    def run(self) -> int:
        self._install_signal_handlers()
        try:
            return self._supervise()
        finally:
            self._stop_child("the supervisor is exiting")

    def _supervise(self) -> int:
        backoff = BACKOFF_MIN
        crashes = 0

        while not self._stopping:
            if self._take_pending_update():
                self._unproven_upgrade = True

            started = time.monotonic()
            code = self._run_child()
            lived = time.monotonic() - started

            if self._stopping:
                return EXIT_OK

            if code == EXIT_OK:
                print("[supervisor] the runner stopped deliberately; exiting.")
                return EXIT_OK

            if code == EXIT_RESTART:
                print("[supervisor] the runner asked to be restarted.")
                self._settle_upgrade()
                backoff, crashes = BACKOFF_MIN, 0
                continue

            if lived >= HEALTHY_SECONDS:
                crashes = 1
                self._settle_upgrade()
            else:
                crashes += 1
            print(
                f"[supervisor] the runner exited {code} after {lived:.0f}s "
                f"(failure {crashes} of {CRASH_LIMIT})"
            )

            if self._unproven_upgrade and crashes >= 2:
                if self._roll_back():
                    self._unproven_upgrade = False
                    backoff, crashes = BACKOFF_MIN, 0
                    continue

            if crashes >= CRASH_LIMIT:
                print(
                    f"[supervisor] giving up after {crashes} consecutive "
                    f"failures. Run `mechbench doctor` to see why."
                )
                return EXIT_CRASH

            time.sleep(backoff)
            backoff = min(backoff * 2, BACKOFF_MAX)

        return EXIT_OK

    def _run_child(self) -> int:
        env = {**os.environ, SUPERVISOR_PID_ENV: str(os.getpid())}
        self._child = subprocess.Popen(self.child_argv, env=env)  # noqa: S603
        try:
            return self._child.wait()
        except KeyboardInterrupt:
            return EXIT_OK
        finally:
            self._child = None
            self._reap_orphans()

    def _reap_orphans(self) -> None:
        # external: a container with no init — as PID 1 only we reap orphans
        try:
            while True:
                pid, _ = os.waitpid(-1, os.WNOHANG)
                if pid == 0:
                    return
        except ChildProcessError:
            return
        except OSError:
            return

    def _take_pending_update(self) -> bool:
        from . import install as install_mod
        from . import updater

        st = updater.load()
        if st is None or st.stage not in {"requested", "verify"}:
            return False

        where = install_mod.detect()
        if not where.upgradable:
            print(f"[supervisor] cannot upgrade here — {where.advice}")
            updater.clear()
            return False

        print(f"[supervisor] upgrading {st.previous} -> {st.target}")
        ok, tail = install_mod.run_upgrade(where, st.target)
        if not ok:
            print(f"[supervisor] upgrade failed: {tail[:300]}")
            updater.clear()
            return False

        st.stage = "verify"
        st.save()
        print(f"[supervisor] now on {install_mod.installed_versions()}")
        return True

    def _settle_upgrade(self) -> None:
        if not self._unproven_upgrade:
            return
        self._unproven_upgrade = False
        from . import updater

        updater.clear()

    def _roll_back(self) -> bool:
        from . import install as install_mod
        from . import updater

        st = updater.load()
        updater.clear()
        if st is None or not st.previous:
            return False
        print(f"[supervisor] rolling back to {st.previous}")
        ok, tail = install_mod.run_upgrade(install_mod.detect(), st.previous)
        if not ok:
            print(f"[supervisor] rollback failed: {tail[:200]}")
        return ok

    def _stop_child(self, why: str) -> None:
        child = self._child
        if child is None or child.poll() is not None:
            return
        print(f"[supervisor] {why}; asking the runner to finish "
              f"(up to {STOP_GRACE:.0f}s).")
        try:
            child.send_signal(signal.SIGTERM)
            child.wait(timeout=STOP_GRACE)
        except subprocess.TimeoutExpired:
            print("[supervisor] the runner did not stop; killing it.")
            with suppress(ProcessLookupError, OSError):
                child.kill()
            with suppress(subprocess.TimeoutExpired):
                child.wait(timeout=10)
        except (ProcessLookupError, OSError):
            pass

    def _install_signal_handlers(self) -> None:
        def stop(signum: int, _frame: FrameType | None) -> None:
            self._stopping = True
            self._stop_child(signal.Signals(signum).name)

        signal.signal(signal.SIGINT, stop)
        signal.signal(signal.SIGTERM, stop)


def main() -> int:
    from .logs import excepthook_to_log
    from .logs import install as install_logs

    install_logs(name="supervisor.log", max_bytes=1024 * 1024)
    excepthook_to_log()
    return Supervisor().run()
