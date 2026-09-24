from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Any


class SpendLedger:
    def __init__(self, cap_usd: float | None = None) -> None:
        from mechbench_compute.providers import Budget

        self.cap_usd = float(cap_usd) if cap_usd else None
        self.budget = Budget(cap_usd=self.cap_usd if self.cap_usd else float("inf"))
        self._reported = -1.0

    @property
    def spent_usd(self) -> float:
        return round(self.budget.spent_usd, 6)

    @property
    def calls(self) -> int:
        return self.budget.calls

    def changed(self, *, epsilon: float = 1e-6) -> bool:
        return self.spent_usd > 0 and self.spent_usd > self._reported + epsilon

    def mark_reported(self) -> None:
        self._reported = self.spent_usd

    def describe(self) -> str:
        if self.cap_usd:
            return f"${self.spent_usd:.4f} of ${self.cap_usd:.2f}"
        return f"${self.spent_usd:.4f}"

    def to_wire(self) -> dict[str, Any]:
        out: dict[str, Any] = {"spent_usd": self.spent_usd, "calls": self.calls}
        if self.cap_usd:
            out["cap_usd"] = self.cap_usd
        return out


class SharedLimiter:
    def __init__(self, path: Path | None = None, *, save_every: float = 5.0) -> None:
        from mechbench_compute.providers.registry import TokenBucketLimiter

        self.path = path
        self._inner = TokenBucketLimiter()
        self._lock = threading.RLock()
        self._save_every = save_every
        self._last_save = 0.0
        self.load()

    def acquire(self, provider: str, model: str, scope: str, currency: str,
                amount: float) -> float:
        waited = self._inner.acquire(provider, model, scope, currency, amount)
        if waited:
            self._maybe_save()
        return waited

    def observe(self, provider: str, model: str, scope: str, limits: Any) -> None:
        self._inner.observe(provider, model, scope, limits)
        self._maybe_save()

    def penalize(self, provider: str, model: str, scope: str,
                 retry_after: float) -> None:
        self._inner.penalize(provider, model, scope, retry_after)
        self.save()

    def release(self, provider: str, model: str, scope: str, currency: str,
                amount: float) -> None:
        self._inner.release(provider, model, scope, currency, amount)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            now = time.monotonic()
            buckets = []
            for (provider, model, scope, currency), b in self._inner._buckets.items():
                b.refill(now)
                buckets.append({
                    "provider": provider, "model": model, "scope": scope,
                    "currency": currency, "available": round(b.tokens, 2),
                    "capacity": round(b.capacity, 2),
                })
            holds = [
                {"provider": provider, "scope": scope,
                 "seconds": round(until - now, 1)}
                for (provider, scope), until in self._inner._holds.items()
                if until > now
            ]
            return {"buckets": buckets, "holds": holds,
                    "waited_seconds": round(self._inner.waited_seconds, 2)}

    def _maybe_save(self) -> None:
        now = time.monotonic()
        if now - self._last_save >= self._save_every:
            self.save()

    def save(self) -> None:
        if self.path is None:
            return
        with self._lock:
            now = time.monotonic()
            wall = time.time()
            state = {
                "version": 1,
                "saved_at": wall,
                "buckets": [
                    {"key": list(key), "tokens": round(b.tokens, 3),
                     "capacity": b.capacity, "per_second": b.per_second}
                    for key, b in self._inner._buckets.items()
                ],
                "holds": [
                    {"key": list(key), "until": wall + (until - now)}
                    for key, until in self._inner._holds.items() if until > now
                ],
            }
            self._last_save = now
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                fd, tmp = tempfile.mkstemp(dir=str(self.path.parent),
                                           prefix=".limits-", suffix=".json")
                with os.fdopen(fd, "w") as fh:
                    json.dump(state, fh)
                os.replace(tmp, self.path)
            except OSError:
                pass

    def load(self) -> None:
        if self.path is None or not self.path.exists():
            return
        try:
            state = json.loads(self.path.read_text())
        except (OSError, ValueError):
            return
        from mechbench_compute.providers.registry import Bucket

        now = time.monotonic()
        wall = time.time()
        with self._lock:
            for entry in state.get("buckets", []):
                key = tuple(entry["key"])
                self._inner._buckets[key] = Bucket(
                    capacity=float(entry["capacity"]),
                    per_second=float(entry["per_second"]),
                    tokens=float(entry["tokens"]), updated=now)
            for entry in state.get("holds", []):
                remaining = float(entry["until"]) - wall
                if remaining > 0:
                    self._inner._holds[tuple(entry["key"])] = now + remaining
