from __future__ import annotations

import os
import sys
import threading
import time
from collections.abc import Callable
from contextlib import suppress

DEFAULT_STALL_SECONDS = 900.0

POLL_SECONDS = 15.0


class Watchdog:
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
        self._paused = False

    def stamp(self) -> None:
        with self._lock:
            self._last = time.monotonic()

    def pause(self) -> None:
        with self._lock:
            self._paused = True

    def resume(self) -> None:
        with self._lock:
            self._paused = False
            self._last = time.monotonic()

    def start(self) -> None:
        if self.stall_seconds <= 0:
            return
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
        os._exit(self.exit_code)  # noqa: SLF001
