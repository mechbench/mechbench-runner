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
CPU_MATMUL_N = 512
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


def _torch_device() -> Any:
    import torch

    return torch.device("cuda" if torch.cuda.is_available() else "cpu")


def _torch_sync(device: Any) -> None:
    import torch

    if device.type == "cuda":
        torch.cuda.synchronize(device)


def _torch_copy() -> tuple[Callable[[], None], dict[str, Any]]:
    import torch

    device = _torch_device()
    a = torch.zeros(COPY_BYTES // 4, dtype=torch.float32, device=device)

    def step() -> None:
        torch.add(a, 1.0)
        _torch_sync(device)
    return step, {"shape": {"variant": f"{COPY_BYTES // 2**20}MiB"}, "dtype": "float32",
                  "bytes": 2 * COPY_BYTES, "flops": None}


def _torch_matmul() -> tuple[Callable[[], None], dict[str, Any]]:
    import torch

    device = _torch_device()
    n = MATMUL_N if device.type == "cuda" else CPU_MATMUL_N
    a = torch.randn(n, n, device=device).to(torch.bfloat16)
    b = torch.randn(n, n, device=device).to(torch.bfloat16)

    def step() -> None:
        torch.matmul(a, b)
        _torch_sync(device)
    return step, {"shape": {"variant": f"{n}x{n}x{n}"}, "dtype": "bfloat16",
                  "bytes": 3 * n * n * 2, "flops": 2 * n**3}


TORCH_SETUPS: dict[str, Callable[[], tuple[Callable[[], None], dict[str, Any]]]] = {
    "memcopy": _torch_copy,
    "matmul": _torch_matmul,
}


def setups(backend: str = "mlx") -> dict[str, Callable[[], tuple[Callable[[], None], dict[str, Any]]]]:
    return TORCH_SETUPS if backend == "torch" else SETUPS


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


def measure(name: str, *, repeats: int = 20, warmup_seconds: float = 0.5,
            backend: str = "mlx") -> dict[str, Any]:
    step, facts = setups(backend)[name]()
    warm = warm_up(step, seconds=warmup_seconds)
    runs = [timed(step) for _ in range(repeats)]
    return {**facts, **summary(runs), "warmup_seconds": warm}


def for_budget(name: str, budget_seconds: float, backend: str = "mlx") -> float:
    step, _ = setups(backend)[name]()
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
                  fingerprint: str, path: Path | None = None, backend: str = "mlx") -> Path:
    target = path or baseline_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    body = {
        "chip": identity.get("chip"),
        "fingerprint": fingerprint,
        **({"backend": backend} if backend != "mlx" else {}),
        "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        **{n: {"seconds": m["seconds"], "shape": m["shape"], "spread": m["spread"]}
           for n, m in measured.items()},
    }
    tmp = target.with_suffix(".tmp")
    tmp.write_text(json.dumps(body, indent=2) + "\n")
    tmp.replace(target)
    return target


def canary(baseline: dict[str, Any] | None, budget_seconds: float = 0.3,
           measure_one: Callable[[str, float], float] | None = None,
           ) -> dict[str, float] | None:
    if baseline is None:
        return None
    if measure_one is None:
        backend = str(baseline.get("backend") or "mlx")

        def measure_one(name: str, budget: float) -> float:
            return for_budget(name, budget, backend)
    out: dict[str, float] = {}
    for name in NAMES:
        seconds = measure_one(name, budget_seconds / len(NAMES))
        out[name] = round(float(baseline[name]["seconds"]) / seconds, 3) if seconds > 0 else 0.0
    return out
