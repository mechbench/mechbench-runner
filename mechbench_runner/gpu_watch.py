from __future__ import annotations

import base64
import gzip
import json
import os
import re
import statistics
import threading
import time
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path
from typing import Any

from . import gpu
from .paths import mechbench_dir

INTERVAL_ENV = "MECHBENCH_GPU_SAMPLE_SECONDS"
DEFAULT_INTERVAL = 1.0
IDLE_PERCENT = 1.0
SERIES_FORMAT = "mechbench.gpu-series/1"
COLUMNS = ("t", "utilization_percent", "memory_used_mib", "host_available_mib",
           "sm_clock_mhz", "temperature_c", "power_w", "reason_mask")

Source = Callable[[], dict[str, Any] | None]
Clock = Callable[[], float]


def interval_setting(env: dict[str, str] | None = None) -> float:
    raw = (env if env is not None else os.environ).get(INTERVAL_ENV)
    if raw is None or not raw.strip():
        return DEFAULT_INTERVAL
    try:
        return max(0.0, float(raw))
    except ValueError:
        return DEFAULT_INTERVAL


def host_available() -> int | None:
    try:
        text = Path("/proc/meminfo").read_text()
    except OSError:
        return None
    found = re.search(r"^MemAvailable:\s*(\d+)\s*kB", text, re.M)
    return int(found.group(1)) * 1024 if found else None


def series_dir() -> Path:
    d = mechbench_dir() / "gpu"
    d.mkdir(mode=0o700, parents=True, exist_ok=True)
    return d


def mib(n: int | None) -> int | None:
    return None if n is None else int(n // 2**20)


class JobGpu:
    def __init__(self, sample: Source, *, interval: float = DEFAULT_INTERVAL,
                 clock: Clock = time.monotonic,
                 available: Callable[[], int | None] = host_available) -> None:
        self._sample = sample
        self._interval = interval
        self._clock = clock
        self._available = available
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self.start_at = 0.0
        self.rows: list[dict[str, Any]] = []
        self.nodes: dict[str, list[float | None]] = {}

    @classmethod
    def for_machine(cls, env: dict[str, str] | None = None) -> JobGpu | None:
        interval = interval_setting(env)
        if interval <= 0:
            return None
        found = gpu.source()
        if found is None:
            return None
        return cls(found.state, interval=interval)

    def take(self) -> None:
        state = self._sample()
        if state is None:
            return
        row = {
            "t": round(self._clock() - self.start_at, 3),
            "utilization_percent": state.get("utilization_percent"),
            "memory_used_mib": mib(state.get("memory_used_bytes")),
            "host_available_mib": mib(self._available()),
            "sm_clock_mhz": state.get("sm_clock_mhz"),
            "temperature_c": state.get("temperature_c"),
            "power_w": state.get("power_w"),
            "reason_mask": state.get("reason_mask"),
        }
        with self._lock:
            self.rows.append(row)

    def _loop(self) -> None:
        while not self._stop.is_set():
            with suppress(Exception):
                self.take()
            self._stop.wait(self._interval)

    def start(self) -> JobGpu:
        self.start_at = self._clock()
        self._thread = threading.Thread(target=self._loop, name="job-gpu", daemon=True)
        self._thread.start()
        return self

    def now(self) -> float:
        return self._clock() - self.start_at

    def node_start(self, nid: str) -> None:
        with self._lock:
            self.nodes[nid] = [self.now(), None]

    def node_end(self, nid: str) -> None:
        with self._lock:
            held = self.nodes.get(nid)
            if held is not None and held[1] is None:
                held[1] = self.now()

    def node_summary(self, nid: str) -> dict[str, Any] | None:
        with self._lock:
            held = self.nodes.get(nid)
            if held is None:
                return None
            lo, hi = held[0] or 0.0, held[1] if held[1] is not None else self.now()
            rows = [r for r in self.rows if lo <= r["t"] <= hi]
        return {"seconds": round(hi - lo, 3), **summarize(rows, self._interval)}

    def stop(self) -> dict[str, Any]:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=10.0)
        with self._lock:
            rows = list(self.rows)
            nodes = {k: list(v) for k, v in self.nodes.items()}
        out = {"format": SERIES_FORMAT, "interval_seconds": self._interval,
               "seconds": round(self.now(), 3), "summary": summarize(rows, self._interval),
               "nodes": {k: {"start": v[0], "end": v[1]} for k, v in nodes.items()},
               "series": pack(rows)}
        return out


def summarize(rows: list[dict[str, Any]], interval: float) -> dict[str, Any]:
    util = [float(r["utilization_percent"]) for r in rows
            if r.get("utilization_percent") is not None]
    used = [r["memory_used_mib"] for r in rows if r.get("memory_used_mib") is not None]
    avail = [r["host_available_mib"] for r in rows if r.get("host_available_mib") is not None]
    clocks = [r["sm_clock_mhz"] for r in rows if r.get("sm_clock_mhz") is not None]
    temps = [r["temperature_c"] for r in rows if r.get("temperature_c") is not None]
    power = [r["power_w"] for r in rows if r.get("power_w") is not None]
    seen: list[str] = []
    for r in rows:
        for name in gpu.reasons(r.get("reason_mask")) or ():
            if name in gpu.THROTTLING and name not in seen:
                seen.append(name)
    out: dict[str, Any] = {"samples": len(rows)}
    if util:
        out.update({
            "busy_fraction": round(statistics.fmean(util) / 100, 4),
            "utilization_mean_percent": round(statistics.fmean(util), 2),
            "utilization_peak_percent": max(util),
            "idle_seconds": round(sum(1 for u in util if u < IDLE_PERCENT) * interval, 3),
        })
    if used:
        out["peak_memory_used_bytes"] = max(used) * 2**20
    if avail:
        out["min_host_available_bytes"] = min(avail) * 2**20
    if clocks:
        out["sm_clock_mhz"] = {"min": min(clocks), "max": max(clocks)}
    if temps:
        out["peak_temperature_c"] = max(temps)
    if power:
        out["power_w"] = {"mean": round(statistics.fmean(power), 1), "max": max(power)}
    out["throttling"] = seen
    return out


def pack(rows: list[dict[str, Any]]) -> str:
    columns = {c: [r.get(c) for r in rows] for c in COLUMNS}
    raw = json.dumps(columns, separators=(",", ":")).encode()
    return base64.b64encode(gzip.compress(raw, mtime=0)).decode()


def unpack(text: str) -> list[dict[str, Any]]:
    columns = json.loads(gzip.decompress(base64.b64decode(text)))
    n = len(columns.get("t") or [])
    return [{c: columns[c][i] for c in COLUMNS if c in columns} for i in range(n)]


def save(job_id: str, body: dict[str, Any]) -> Path:
    target = series_dir() / f"{job_id}.json"
    tmp = target.with_suffix(".tmp")
    tmp.write_text(json.dumps(body) + "\n")
    tmp.replace(target)
    return target


def object_path(result_path: str | None) -> str | None:
    if not result_path or result_path.count("/") < 3:
        return None
    return f"{result_path.rstrip('/')}/gpu-series"
