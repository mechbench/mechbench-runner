from __future__ import annotations

import gc
import os
import re
import threading
import time
from collections.abc import Callable
from contextlib import suppress
from typing import Any

from .identity import run as _run

THERMAL = ("nominal", "fair", "serious", "critical")
DENY = ("mds_stores", "backupd", "photoanalysisd", "mediaanalysisd")
BUSY_CPU = 10.0
BIG_RSS_BYTES = 4 * 2**30
BIG_RSS_CPU = 5.0
CANARY_QUIET = 0.85
INTERVAL_SECONDS = 1.0
SETTLE_BOUND_SECONDS = 1.0
SETTLE_POLL_SECONDS = 0.025

Run = Callable[[list[str]], str]


def run(cmd: list[str]) -> str:
    return _run(cmd, timeout=5.0)


def thermal(sh: Run) -> dict[str, Any]:
    try:
        from Foundation import NSProcessInfo  # type: ignore[import-not-found]

        level = int(NSProcessInfo.processInfo().thermalState())
        return {"state": THERMAL[min(max(level, 0), 3)], "source": "NSProcessInfo"}
    except Exception:  # noqa: BLE001
        pass
    out = sh(["pmset", "-g", "therm"])
    limit = re.search(r"CPU_Speed_Limit\s*=\s*(\d+)", out)
    warning = re.search(r"thermal warning level\s*(?:is|=|:)?\s*(\d+)", out, re.I)
    state = "nominal"
    if limit and int(limit.group(1)) < 100:
        state = "serious" if int(limit.group(1)) < 70 else "fair"
    if warning and int(warning.group(1)) > 0:
        state = THERMAL[min(int(warning.group(1)), 3)]
    return {"state": state, "source": "pmset",
            "cpu_speed_limit": int(limit.group(1)) if limit else None}


def gpu_utilization(sh: Run) -> int | None:
    found = re.search(r'"Device Utilization %"\s*=\s*(\d+)',
                      sh(["ioreg", "-r", "-d", "1", "-c", "IOAccelerator"]))
    return int(found.group(1)) if found else None


def memory(sh: Run) -> dict[str, Any]:
    out: dict[str, Any] = {}
    free = re.search(r"free percentage:\s*(\d+)%", sh(["memory_pressure"]))
    if free:
        out["free_percent"] = int(free.group(1))
    swap = re.search(r"used\s*=\s*([\d.]+)M", sh(["sysctl", "vm.swapusage"]))
    if swap:
        out["swap_used_bytes"] = int(float(swap.group(1)) * 2**20)
    vm = sh(["vm_stat"])
    page = re.search(r"page size of (\d+) bytes", vm)
    size = int(page.group(1)) if page else 16384
    for label, key in (("Pageouts", "pageouts"), ("Swapouts", "swapouts"),
                       ("Pages occupied by compressor", "compressed_bytes")):
        m = re.search(rf"{label}:\s*(\d+)", vm)
        if m:
            out[key] = int(m.group(1)) * (size if key.endswith("_bytes") else 1)
    return out


def processes(sh: Run, me: int) -> list[dict[str, Any]]:
    procs: list[dict[str, Any]] = []
    for line in sh(["ps", "-Aceo", "pid=,pcpu=,rss=,comm="]).splitlines():
        parts = line.split(None, 3)
        if len(parts) < 4 or not parts[0].isdigit():
            continue
        pid = int(parts[0])
        if pid == me:
            continue
        try:
            cpu, rss = float(parts[1]), int(parts[2]) * 1024
        except ValueError:
            continue
        procs.append({"name": parts[3].strip(), "cpu": cpu, "rss_bytes": rss})
    procs.sort(key=lambda p: -p["cpu"])
    return procs


def busy(procs: list[dict[str, Any]]) -> list[str]:
    out: list[str] = []
    for p in procs:
        named = p["name"] in DENY and p["cpu"] >= BUSY_CPU
        big = p["rss_bytes"] >= BIG_RSS_BYTES and p["cpu"] >= BIG_RSS_CPU
        if (named or big) and p["name"] not in out:
            out.append(p["name"])
    return out


def power(sh: Run) -> dict[str, Any]:
    batt = sh(["pmset", "-g", "batt"])
    source = ("ac" if "AC Power" in batt else "battery" if "Battery Power" in batt
              else None)
    low = re.search(r"lowpowermode\s+(\d)", sh(["pmset", "-g"]))
    return {"source": source, "low_power": bool(low and low.group(1) == "1")}


def _span(values: list[float]) -> dict[str, float] | None:
    return {"min": min(values), "max": max(values)} if values else None


class Sampler:
    def __init__(self, sh: Run = run, interval: float = INTERVAL_SECONDS,
                 me: int | None = None) -> None:
        self._sh = sh
        self._interval = interval
        self._me = os.getpid() if me is None else me
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self.samples = 0
        self._thermal: list[str] = []
        self._speed: list[float] = []
        self._gpu: list[float] = []
        self._mem: dict[str, list[float]] = {}
        self._load: list[float] = []
        self._top: dict[str, dict[str, Any]] = {}
        self._busy: list[str] = []
        self._power: dict[str, Any] = {}

    def sample(self) -> None:
        t = thermal(self._sh)
        gpu = gpu_utilization(self._sh)
        mem = memory(self._sh)
        procs = processes(self._sh, self._me)
        try:
            load = os.getloadavg()[0]
        except OSError:
            load = None
        with self._lock:
            self.samples += 1
            self._thermal.append(t["state"])
            if t.get("cpu_speed_limit") is not None:
                self._speed.append(t["cpu_speed_limit"])
            if gpu is not None:
                self._gpu.append(gpu)
            for k, v in mem.items():
                self._mem.setdefault(k, []).append(v)
            if load is not None:
                self._load.append(round(load, 2))
            for p in procs[:3]:
                seen = self._top.get(p["name"])
                if seen is None or p["cpu"] > seen["cpu"]:
                    self._top[p["name"]] = dict(p)
            for name in busy(procs):
                if name not in self._busy:
                    self._busy.append(name)

    def _loop(self) -> None:
        while not self._stop.is_set():
            with suppress(Exception):
                self.sample()
            self._stop.wait(self._interval)

    def start(self) -> Sampler:
        try:
            self._power = power(self._sh)
        except Exception:  # noqa: BLE001
            self._power = {}
        self._thread = threading.Thread(target=self._loop, name="ambient", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=10.0)
        if self.samples == 0:
            with suppress(Exception):
                self.sample()

    def result(self) -> dict[str, Any]:
        with self._lock:
            worst = max(self._thermal, key=THERMAL.index) if self._thermal else None
            mem: dict[str, Any] = {}
            for k, v in self._mem.items():
                if k in ("pageouts", "swapouts"):
                    mem[f"{k}_during"] = int(max(v) - min(v))
                else:
                    mem[k] = _span(v)
            top = sorted(self._top.values(), key=lambda p: -p["cpu"])[:3]
            return {
                "samples": self.samples,
                "thermal": {"worst": worst, **({"cpu_speed_limit_min": min(self._speed)}
                                                if self._speed else {})},
                "gpu_utilization": _span(self._gpu),
                "memory": mem,
                "load": _span(self._load),
                "top": [{"name": p["name"], "cpu": p["cpu"], "rss_bytes": p["rss_bytes"]}
                        for p in top],
                "busy": list(self._busy),
                "power": dict(self._power),
            }


def _mlx() -> tuple[Callable[[], None] | None, Callable[[], int] | None]:
    try:
        import mlx.core as mx
    except Exception:  # noqa: BLE001
        return None, None

    def clear() -> None:
        gc.collect()
        mx.synchronize()
        mx.clear_cache()

    def read() -> int:
        return int(mx.get_active_memory()) + int(mx.get_cache_memory())
    return clear, read


def settle(clear: Callable[[], None] | None = None, read: Callable[[], int] | None = None, *,
           bound: float = SETTLE_BOUND_SECONDS, poll: float = SETTLE_POLL_SECONDS,
           clock: Callable[[], float] = time.monotonic,
           sleep: Callable[[float], None] = time.sleep) -> int:
    if clear is None and read is None:
        clear, read = _mlx()
    start = clock()
    if clear is not None:
        with suppress(Exception):
            clear()
    if read is not None:
        with suppress(Exception):
            first = now = last = read()
            while clock() - start < bound:
                sleep(poll)
                now = read()
                if now >= last:
                    break
                last = now
            if clear is not None and now < first:
                clear()
    return round((clock() - start) * 1000)


def _worst(ratios: dict[str, float] | None) -> float | None:
    return min(ratios.values()) if ratios else None


def ambient(counters: dict[str, Any], before: dict[str, float] | None,
            after: dict[str, float] | None,
            after_delay_ms: int | None = None) -> tuple[dict[str, Any], bool]:
    out: dict[str, Any] = {
        "canary_before": _worst(before),
        "canary_after": _worst(after),
        "canary_after_delay_ms": after_delay_ms,
        "canary": {"before": before, "after": after},
        **counters,
    }
    reasons: list[str] = []
    for when, value in (("before", out["canary_before"]), ("after", out["canary_after"])):
        if value is not None and value < CANARY_QUIET:
            reasons.append(f"the canary {when} the node ran at {value:.2f} of the idle baseline")
    worst = (counters.get("thermal") or {}).get("worst")
    if worst not in (None, "nominal"):
        reasons.append(f"thermal state {worst}")
    for name in counters.get("busy") or []:
        reasons.append(f"{name} was busy")
    if (counters.get("power") or {}).get("low_power"):
        reasons.append("low power mode was on")
    out["reasons"] = reasons
    return out, not reasons


MODEL_CLASSES = ("local", "mlx-local", "cuda", "tpu")


def model_bearing(spec: dict[str, Any]) -> set[str]:
    graph = spec.get("graph") if isinstance(spec, dict) else None
    nodes = graph.get("nodes") if isinstance(graph, dict) else None
    out: set[str] = set()
    for n in nodes or []:
        if not isinstance(n, dict):
            continue
        req = n.get("requirements")
        if isinstance(req, dict) and req.get("class") in MODEL_CLASSES:
            out.add(str(n.get("id")))
    return out


Report = Callable[[str, dict[str, Any]], None]


class NodeWatch:
    def __init__(self, bearing: set[str], report: Report, *,
                 baseline: Callable[[], dict[str, Any] | None] | None = None,
                 canary: Callable[[dict[str, Any] | None], dict[str, float] | None] | None = None,
                 sampler: Callable[[], Sampler] = Sampler,
                 settle: Callable[[], int] = settle) -> None:
        from . import microbench

        self._bearing = bearing
        self._report = report
        self._baseline = baseline or microbench.load_baseline
        self._canary = canary or microbench.canary
        self._sampler = sampler
        self._settle = settle
        self._open: dict[str, Any] | None = None

    def start(self, nid: str) -> None:
        self.close(canary_after=False)
        if nid not in self._bearing:
            return
        try:
            base = self._baseline()
            before = self._canary(base)
            self._open = {"nid": nid, "baseline": base, "before": before,
                          "sampler": self._sampler().start()}
        except Exception:  # noqa: BLE001
            self._open = None

    def _finish(self, nid: str | None, *, canary_after: bool) -> dict[str, Any] | None:
        held = self._open
        if held is None or (nid is not None and held["nid"] != nid):
            return None
        self._open = None
        try:
            held["sampler"].stop()
            after = delay = None
            if canary_after and held["baseline"] is not None:
                delay = self._settle()
                after = self._canary(held["baseline"])
            amb, quiet = ambient(held["sampler"].result(), held["before"], after, delay)
        except Exception:  # noqa: BLE001
            return None
        return {"ambient": amb, "quiet": quiet}

    def _send(self, nid: str, fields: dict[str, Any]) -> None:
        body = {k: v for k, v in fields.items() if v is not None}
        if body:
            with suppress(Exception):
                self._report(nid, body)

    def span(self, nid: str, measured: dict[str, Any]) -> None:
        self._send(nid, {**measured, **(self._finish(nid, canary_after=True) or {})})

    def done(self, nid: str) -> None:
        held = self._finish(nid, canary_after=True)
        if held:
            self._send(nid, held)

    def close(self, *, canary_after: bool = True) -> None:
        nid = self._open["nid"] if self._open is not None else None
        held = self._finish(None, canary_after=canary_after)
        if nid is not None and held:
            self._send(nid, held)
