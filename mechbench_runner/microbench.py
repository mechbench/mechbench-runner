from __future__ import annotations

import json
import statistics
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import paths

COPY_BYTES = 256 * 2**20
MATMUL_N = 4096
NAMES = ("memcopy", "matmul")


def baseline_path() -> Path:
    return paths.mechbench_dir() / "calibration" / "baseline.json"


def _copy() -> tuple[Callable[[], None], dict[str, Any]]:
    import mlx.core as mx

    a = mx.zeros((COPY_BYTES // 4,), dtype=mx.float32)
    mx.eval(a)

    def step() -> None:
        mx.eval(a + 1.0)
    return step, {"shape": {"variant": f"{COPY_BYTES // 2**20}MiB"}, "dtype": "float32",
                  "bytes": 2 * COPY_BYTES, "flops": None}


def _matmul() -> tuple[Callable[[], None], dict[str, Any]]:
    import mlx.core as mx

    a = mx.random.normal((MATMUL_N, MATMUL_N)).astype(mx.bfloat16)
    b = mx.random.normal((MATMUL_N, MATMUL_N)).astype(mx.bfloat16)
    mx.eval(a, b)

    def step() -> None:
        mx.eval(a @ b)
    return step, {"shape": {"variant": f"{MATMUL_N}x{MATMUL_N}x{MATMUL_N}"}, "dtype": "bfloat16",
                  "bytes": 3 * MATMUL_N * MATMUL_N * 2, "flops": 2 * MATMUL_N**3}


SETUPS: dict[str, Callable[[], tuple[Callable[[], None], dict[str, Any]]]] = {
    "memcopy": _copy,
    "matmul": _matmul,
}


def timed(step: Callable[[], None]) -> float:
    t = time.perf_counter()
    step()
    return time.perf_counter() - t


def spread(seconds: list[float]) -> float:
    if len(seconds) < 2:
        return 0.0
    q = statistics.quantiles(seconds, n=4, method="inclusive")
    return q[2] - q[0]


def summary(seconds: list[float]) -> dict[str, Any]:
    med = statistics.median(seconds)
    return {
        "seconds": med,
        "repeats": len(seconds),
        "spread": spread(seconds),
        "min_seconds": min(seconds),
        "max_seconds": max(seconds),
    }


def warm_up(step: Callable[[], None], *, at_least: int = 3,
            seconds: float = 0.5) -> list[float]:
    warm: list[float] = []
    end = time.perf_counter() + seconds
    while len(warm) < at_least or time.perf_counter() < end:
        warm.append(timed(step))
    return warm


def measure(name: str, *, repeats: int = 20, warmup_seconds: float = 0.5) -> dict[str, Any]:
    step, facts = SETUPS[name]()
    warm = warm_up(step, seconds=warmup_seconds)
    runs = [timed(step) for _ in range(repeats)]
    return {**facts, **summary(runs), "warmup_seconds": warm}


def for_budget(name: str, budget_seconds: float) -> float:
    step, _ = SETUPS[name]()
    step()
    runs: list[float] = []
    end = time.perf_counter() + budget_seconds
    while not runs or time.perf_counter() < end:
        runs.append(timed(step))
    return statistics.median(runs)


def load_baseline(path: Path | None = None) -> dict[str, Any] | None:
    try:
        raw = json.loads((path or baseline_path()).read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None
    if not all(isinstance(raw.get(n), dict) and raw[n].get("seconds") for n in NAMES):
        return None
    return raw


def save_baseline(measured: dict[str, dict[str, Any]], identity: dict[str, Any],
                  fingerprint: str, path: Path | None = None) -> Path:
    target = path or baseline_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    body = {
        "chip": identity.get("chip"),
        "fingerprint": fingerprint,
        "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        **{n: {"seconds": m["seconds"], "shape": m["shape"], "spread": m["spread"]}
           for n, m in measured.items()},
    }
    tmp = target.with_suffix(".tmp")
    tmp.write_text(json.dumps(body, indent=2) + "\n")
    tmp.replace(target)
    return target


def canary(baseline: dict[str, Any] | None, budget_seconds: float = 0.3,
           measure_one: Callable[[str, float], float] = for_budget,
           ) -> dict[str, float] | None:
    if baseline is None:
        return None
    out: dict[str, float] = {}
    for name in NAMES:
        seconds = measure_one(name, budget_seconds / len(NAMES))
        out[name] = round(float(baseline[name]["seconds"]) / seconds, 3) if seconds > 0 else 0.0
    return out
