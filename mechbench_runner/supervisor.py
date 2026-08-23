"""A parent that owns the runner's lifecycle (tasks 000295, 000296).

The awkward part of self-update is one constraint: **a process cannot
replace its own running code.** Everything the runner does alone —
write a marker, exit 75, upgrade at the next startup before importing
itself, re-exec — exists to work around it.

A parent has no such constraint. It upgrades the child's environment
while the child is not running, then starts a fresh one. Nothing
replaces itself, and rollback stops being a state machine spanning
restarts: the parent is watching, so it simply *knows* whether the new
version came up.

This is not a second supervisor. launchd and systemd still own *this*
process — `install-agent` points them here — and this owns exactly one
child. The same exit-code contract applies at both levels, which is
what keeps them from disagreeing:

    0   the child stopped deliberately   -> stop too, and stay stopped
    1   the child crashed                -> restart it, with backoff
    75  the child asked to come back     -> upgrade, then restart it

It also serves the case 000295 was filed for: a container or a bare box
with no init at all, where nothing else would restart the runner and
this process may be PID 1.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from types import FrameType

from .exits import EXIT_CRASH, EXIT_OK, EXIT_RESTART

#: Backoff between restarts of a crashing child.
BACKOFF_MIN = 1.0
BACKOFF_MAX = 60.0

#: Consecutive crashes before giving up. A supervisor that retries
#: forever turns a broken install into a hot loop nobody notices.
CRASH_LIMIT = 5

#: A child that survives this long is considered to have started, so its
#: crash counter resets. Shorter than a model download on purpose: what
#: is being detected is "fails immediately", not "fails eventually".
HEALTHY_SECONDS = 60.0

#: How long to let a child finish after SIGTERM before insisting.
STOP_GRACE = 300.0


class Supervisor:
    def __init__(self, argv: list[str] | None = None) -> None:
        #: What to run. `-m` rather than a console script: the script's
        #: location depends on PATH, and a service has almost none.
        self.child_argv = argv or [
            sys.executable, "-m", "mechbench_runner.cli", "run",
        ]
        self._child: subprocess.Popen[bytes] | None = None
        self._stopping = False
        #: True from an upgrade until the new child proves it starts.
        #: Recomputing this per iteration was a bug: the rollback needs
        #: two failures to fire, and by the second one an
        #: upgraded-this-iteration flag has already gone false.
        self._unproven_upgrade = False

    # -- lifecycle

    def run(self) -> int:
        self._install_signal_handlers()
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
                # The child meant it — signed out, or told to stop. A
                # supervisor that restarted this would defeat the whole
                # point of the contract.
                print("[supervisor] the runner stopped deliberately; exiting.")
                return EXIT_OK

            if code == EXIT_RESTART:
                print("[supervisor] the runner asked to be restarted.")
                self._settle_upgrade()
                backoff, crashes = BACKOFF_MIN, 0
                continue

            if lived >= HEALTHY_SECONDS:
                # It started and stayed up: whatever is wrong now, the
                # install is not it.
                crashes = 1
                self._settle_upgrade()
            else:
                crashes += 1
            print(
                f"[supervisor] the runner exited {code} after {lived:.0f}s "
                f"(failure {crashes} of {CRASH_LIMIT})"
            )

            if self._unproven_upgrade and crashes >= 2:
                # It came up before the upgrade and does not now. The
                # parent watched both, so this needs no marker to infer.
                if self._roll_back():
                    self._unproven_upgrade = False
                    backoff, crashes = BACKOFF_MIN, 0
                    continue

            if crashes >= CRASH_LIMIT:
                print(
                    f"[supervisor] giving up after {crashes} consecutive "
                    f"failures. Run `mechbench-runner doctor` to see why."
                )
                return EXIT_CRASH

            time.sleep(backoff)
            backoff = min(backoff * 2, BACKOFF_MAX)

        return EXIT_OK

    # -- the child

    def _run_child(self) -> int:
        self._child = subprocess.Popen(self.child_argv)  # noqa: S603
        try:
            return self._child.wait()
        except KeyboardInterrupt:
            return EXIT_OK
        finally:
            self._child = None
            self._reap_orphans()

    def _reap_orphans(self) -> None:
        """As PID 1 in a container, nothing else will."""
        try:
            while True:
                pid, _ = os.waitpid(-1, os.WNOHANG)
                if pid == 0:
                    return
        except ChildProcessError:
            return
        except OSError:
            return

    # -- updates

    def _take_pending_update(self) -> bool:
        """Upgrade before starting the child. True if anything changed.

        This is the whole simplification: the code being replaced is not
        running, so there is no marker to carry across a restart and no
        re-exec.
        """
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

        # Kept until the child proves it starts, so a rollback knows
        # where to go back to.
        st.stage = "verify"
        st.save()
        print(f"[supervisor] now on {install_mod.installed_versions()}")
        return True

    def _settle_upgrade(self) -> None:
        """The child came up, so the upgrade is proven; drop the marker."""
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

    # -- signals

    def _install_signal_handlers(self) -> None:
        def stop(signum: int, _frame: FrameType | None) -> None:
            self._stopping = True
            name = signal.Signals(signum).name
            child = self._child
            if child is None:
                return
            print(f"[supervisor] {name}; asking the runner to finish.")
            try:
                child.send_signal(signal.SIGTERM)
                child.wait(timeout=STOP_GRACE)
            except subprocess.TimeoutExpired:
                print("[supervisor] the runner did not stop; killing it.")
                child.kill()
            except (ProcessLookupError, OSError):
                pass

        signal.signal(signal.SIGINT, stop)
        signal.signal(signal.SIGTERM, stop)


def main() -> int:
    from .logs import excepthook_to_log
    from .logs import install as install_logs

    # Its own file, not the child's. Two processes appending to one log
    # would each rotate it out from under the other; and the split is
    # honest anyway — this file is lifecycle (starts, restarts,
    # upgrades, giving up) while runner.log is the work. Both are
    # bounded, which is the property that matters on a machine nobody
    # watches.
    install_logs(name="supervisor.log", max_bytes=1024 * 1024)
    excepthook_to_log()
    return Supervisor().run()
