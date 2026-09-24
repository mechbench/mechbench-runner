from __future__ import annotations

import sys
import threading
import time
from pathlib import Path
from types import TracebackType
from typing import TextIO

from .paths import mechbench_dir

DEFAULT_MAX_BYTES = 8 * 1024 * 1024
DEFAULT_KEEP = 3


def log_dir() -> Path:
    d = mechbench_dir() / "logs"
    d.mkdir(mode=0o700, parents=True, exist_ok=True)
    return d


class RotatingWriter:
    def __init__(
        self,
        path: Path,
        *,
        max_bytes: int = DEFAULT_MAX_BYTES,
        keep: int = DEFAULT_KEEP,
        mirror: TextIO | None = None,
    ) -> None:
        self.path = path
        self.max_bytes = max_bytes
        self.keep = keep
        self.mirror = mirror
        self._lock = threading.Lock()
        self._bol = True
        self._fh = path.open("a", encoding="utf-8")
        self._size = path.stat().st_size if path.exists() else 0

    def _stamp(self, text: str) -> str:
        out: list[str] = []
        for seg in text.splitlines(keepends=True):
            if self._bol and seg != "\n":
                out.append(time.strftime("%Y-%m-%d %H:%M:%S "))
            out.append(seg)
            self._bol = seg.endswith("\n")
        return "".join(out)

    def write(self, text: str) -> int:
        stamped = self._stamp(text)
        with self._lock:
            self._fh.write(stamped)
            self._fh.flush()
            self._size += len(stamped.encode("utf-8", "replace"))
            if self._size >= self.max_bytes:
                self._rotate()
        if self.mirror is not None:
            try:
                self.mirror.write(text)
                self.mirror.flush()
            except (ValueError, OSError):
                self.mirror = None
        return len(text)

    def flush(self) -> None:
        with self._lock:
            if not self._fh.closed:
                self._fh.flush()

    def isatty(self) -> bool:
        return False

    def close(self) -> None:
        with self._lock:
            if not self._fh.closed:
                self._fh.close()

    def _rotate(self) -> None:
        self._fh.close()
        oldest = self.path.with_suffix(self.path.suffix + f".{self.keep}")
        oldest.unlink(missing_ok=True)
        for n in range(self.keep - 1, 0, -1):
            src = self.path.with_suffix(self.path.suffix + f".{n}")
            if src.exists():
                src.rename(self.path.with_suffix(self.path.suffix + f".{n + 1}"))
        if self.path.exists():
            self.path.rename(self.path.with_suffix(self.path.suffix + ".1"))
        self._fh = self.path.open("a", encoding="utf-8")
        self._size = 0


_installed: RotatingWriter | None = None


def install(
    *,
    name: str = "runner.log",
    max_bytes: int = DEFAULT_MAX_BYTES,
    keep: int = DEFAULT_KEEP,
    mirror: bool | None = None,
) -> RotatingWriter:
    global _installed  # noqa: PLW0603
    if _installed is not None:
        return _installed
    if mirror is None:
        mirror = bool(getattr(sys.stdout, "isatty", lambda: False)())
    writer = RotatingWriter(
        log_dir() / name,
        max_bytes=max_bytes,
        keep=keep,
        mirror=sys.stdout if mirror else None,
    )
    sys.stdout = writer  # type: ignore[assignment]
    sys.stderr = writer  # type: ignore[assignment]
    _installed = writer
    return writer


def uninstall() -> None:
    global _installed  # noqa: PLW0603
    if _installed is None:
        return
    sys.stdout = sys.__stdout__
    sys.stderr = sys.__stderr__
    _installed.close()
    _installed = None


def excepthook_to_log() -> None:
    def hook(
        exc_type: type[BaseException],
        exc: BaseException,
        tb: TracebackType | None,
    ) -> None:
        import traceback

        traceback.print_exception(exc_type, exc, tb, file=sys.stderr)
        sys.stderr.flush()

    sys.excepthook = hook
