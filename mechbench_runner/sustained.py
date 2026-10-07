from __future__ import annotations

import statistics
import threading
import time
from collections.abc import Callable
from contextlib import suppress
from typing import Any

from . import gpu

DEFAULT_MINUTES = 5.0
SAMPLE_SECONDS = 1.0
CHUNK_SECONDS = 0.5
BURST_SECONDS = 10.0
STEADY_SECONDS = 60.0
SLOW_FRACTION = 0.9
ROLLING = 5

Clock = Callable[[], float]
Sample = Callable[[], dict[str, Any] | None]


class GpuSampler:
    def __init__(self, sample: Sample, *, interval: float = SAMPLE_SECONDS,
                 clock: Clock = time.monotonic) -> None:
        self._sample = sample
        self._interval = interval
        self._clock = clock
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self.start_at = 0.0
        self.samples: list[dict[str, Any]] = []

    def _loop(self) -> None:
        while not self._stop.is_set():
            with suppress(Exception):
                got = self._sample()
                if got is not None:
                    with self._lock:
                        self.samples.append({"t": round(self._clock() - self.start_at, 3), **got})
            self._stop.wait(self._interval)

    def start(self) -> GpuSampler:
        self.start_at = self._clock()
        self._thread = threading.Thread(target=self._loop, name="gpu-sampler", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> list[dict[str, Any]]:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=15.0)
        with self._lock:
            return list(self.samples)


def run_load(step: Callable[[], None], timed: Callable[[Callable[[], None]], float], *,
             seconds: float, start_at: float, clock: Clock = time.monotonic,
             chunk_seconds: float = CHUNK_SECONDS) -> list[dict[str, float]]:
    one = timed(step)
    per_chunk = max(1, int(chunk_seconds / one)) if one > 0 else 1

    def chunk() -> None:
        for _ in range(per_chunk):
            step()

    chunks: list[dict[str, float]] = []
    end = start_at + seconds
    while clock() < end:
        began = clock() - start_at
        elapsed = timed(chunk)
        chunks.append({"t": round(began + elapsed / 2, 3), "step_seconds": elapsed / per_chunk})
    return chunks


def window(chunks: list[dict[str, float]], lo: float, hi: float) -> list[float]:
    return [c["step_seconds"] for c in chunks if lo <= c["t"] < hi]


def analyze(chunks: list[dict[str, float]], samples: list[dict[str, Any]], flops: float,
            ) -> dict[str, Any]:
    if not chunks:
        return {"chunks": 0}
    duration = chunks[-1]["t"]
    first = chunks[0]["t"]
    burst = window(chunks, first, first + BURST_SECONDS) or [chunks[0]["step_seconds"]]
    steady_from = max(duration - STEADY_SECONDS, first + BURST_SECONDS) \
        if duration > 3 * BURST_SECONDS else duration * 2 / 3
    steady = window(chunks, steady_from, float("inf")) or [chunks[-1]["step_seconds"]]
    burst_s = statistics.median(burst)
    steady_s = statistics.median(steady)
    slowed_at = None
    for i in range(len(chunks) - ROLLING + 1):
        rolling = statistics.median(c["step_seconds"] for c in chunks[i:i + ROLLING])
        if burst_s / rolling < SLOW_FRACTION:
            slowed_at = chunks[i]["t"]
            break
    throttle_began = next((s["t"] for s in samples if s.get("throttling")), None)
    slowdown_began = next((s["t"] for s in samples
                           if set(s.get("throttling") or ()) & gpu.SLOWDOWN), None)
    ratio = burst_s / steady_s if steady_s > 0 else None
    if samples and any(s.get("reasons") is not None for s in samples):
        throttled = "yes" if throttle_began is not None or (ratio or 1) < SLOW_FRACTION else "no"
    else:
        throttled = "yes" if (ratio or 1) < SLOW_FRACTION else "unknown"
    return {
        "chunks": len(chunks),
        "duration_seconds": duration,
        "burst": burst,
        "steady": steady,
        "burst_flops_per_second": flops / burst_s,
        "steady_flops_per_second": flops / steady_s,
        "steady_over_burst": ratio,
        "slowed_at_seconds": slowed_at,
        "throttle_began_seconds": throttle_began,
        "slowdown_began_seconds": slowdown_began,
        "throttled": throttled,
        "gpu": gpu.summarize(samples),
    }


def series(chunks: list[dict[str, float]], samples: list[dict[str, Any]], flops: float,
           ) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for s in samples:
        near = min(chunks, key=lambda c: abs(c["t"] - s["t"])) if chunks else None
        out.append({
            "t": s["t"],
            "tflops": round(flops / near["step_seconds"] / 1e12, 3) if near else None,
            "sm_clock_mhz": s.get("sm_clock_mhz"),
            "temperature_c": s.get("temperature_c"),
            "power_w": s.get("power_w"),
            "throttling": s.get("throttling"),
        })
    if not samples:
        out = [{"t": c["t"], "tflops": round(flops / c["step_seconds"] / 1e12, 3)}
               for c in chunks[::max(1, len(chunks) // 300)]]
    return out


def sustained(step: Callable[[], None], timed: Callable[[Callable[[], None]], float], *,
              flops: float, minutes: float, sample: Sample | None,
              clock: Clock = time.monotonic, interval: float = SAMPLE_SECONDS,
              chunk_seconds: float = CHUNK_SECONDS) -> dict[str, Any]:
    sampler = GpuSampler(sample, interval=interval, clock=clock).start() if sample else None
    start_at = sampler.start_at if sampler else clock()
    try:
        chunks = run_load(step, timed, seconds=minutes * 60, start_at=start_at, clock=clock,
                          chunk_seconds=chunk_seconds)
    finally:
        samples = sampler.stop() if sampler else []
    return {**analyze(chunks, samples, flops), "series": series(chunks, samples, flops)}
