"""Noticing that this process is alive but stuck.

Neither launchd nor a plain systemd unit can tell a wedged forward pass
from a slow one — the process is running either way, so nothing outside
will ever restart it. The runner therefore has to notice its own stall
and exit non-zero, letting the platform bring it back. Self-termination
is strictly simpler than an external health check, and it is the reason
this lives inside the runner rather than in anything watching it.

The stamp is deliberately not "the loop went round". A model download is
a legitimate half-hour of doing nothing else, so the download callbacks
stamp too: what is being measured is *progress*, not iterations.
"""

from __future__ import annotations

import os
import sys
import threading
import time
from collections.abc import Callable
from contextlib import suppress

#: Generous on purpose. The longest legitimate quiet period is a model
#: download, and those stamp as they go, so anything past this is a
#: process that has stopped making progress rather than a slow one.
DEFAULT_STALL_SECONDS = 900.0

#: How often to look. Cheap; the check is a subtraction.
POLL_SECONDS = 15.0


class Watchdog:
    """Stamps progress from the work thread; kills the process without it."""

    def __init__(
        self,
        *,
        stall_seconds: float = DEFAULT_STALL_SECONDS,
        on_stall: Callable[[float], None] | None = None,
        exit_code: int = 1,
    ) -> None:
        self.stall_seconds = stall_seconds
        self.on_stall = on_stall
        self.exit_code = exit_code
        self._last = time.monotonic()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        #: Set while something legitimately long is happening and the
        #: normal threshold does not apply.
        self._paused = False

    def stamp(self) -> None:
        """Progress happened. Called from the job thread, must be cheap."""
        with self._lock:
            self._last = time.monotonic()

    def pause(self) -> None:
        """Stop counting — for a wait whose length we genuinely cannot bound."""
        with self._lock:
            self._paused = True

    def resume(self) -> None:
        with self._lock:
            self._paused = False
            self._last = time.monotonic()

    def start(self) -> None:
        if self.stall_seconds <= 0:
            return  # disabled
        self._thread = threading.Thread(
            target=self._run, name="watchdog", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def idle_seconds(self) -> float:
        with self._lock:
            return 0.0 if self._paused else time.monotonic() - self._last

    def _run(self) -> None:
        while not self._stop.wait(POLL_SECONDS):
            idle = self.idle_seconds()
            if idle < self.stall_seconds:
                continue
            self._die(idle)

    def _die(self, idle: float) -> None:
        if self.on_stall is not None:
            # Reporting is a courtesy; it must not stop us dying.
            with suppress(Exception):
                self.on_stall(idle)
        print(
            f"[runner] no progress in {idle:.0f}s; exiting so the supervisor "
            f"can restart a healthy one.",
            file=sys.stderr,
        )
        with suppress(Exception):
            sys.stderr.flush()
            sys.stdout.flush()
        # os._exit, not sys.exit: sys.exit from a thread only ends the
        # thread, and the point is that the rest of the process is stuck
        # and will not unwind.
        os._exit(self.exit_code)  # noqa: SLF001
