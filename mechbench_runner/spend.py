"""What a job spends with external providers (task 000338, epic 000334).

Two objects, both owned by the runner because the runner is what
survives a job:

`SpendLedger` wraps the compute `Budget` the executor chains every
remote node under. The cap comes from the run (`spec.budgetUsd`);
without one the ledger still counts, because the number belongs on the
job board either way. Spend rides along with progress reports — the
running TOTAL, never a delta, so a dropped report costs nothing.

`SharedLimiter` is the machine's rate limiter: one set of buckets per
(provider, model, key scope) shared by every node and job here, since a
conversation with three remote participants on one key drains one
account. It persists to `~/.mechbench/limits.json`, so a restart does
not forget that the account was throttled ten seconds ago.

Neither ever holds a credential. The limiter's scope key is a
credential FINGERPRINT — `providers.limiter.scope_for`, a short hash —
so two keys for one provider get different buckets without this file
ever containing a secret.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Any


class SpendLedger:
    """The job's running total against its cap."""

    def __init__(self, cap_usd: float | None = None) -> None:
        from mechbench_compute.providers import Budget

        # No declared cap is not "no limit" in spirit — every remote
        # node still carries its own — so the job budget is unbounded
        # only in the sense that the nodes are what bind it.
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
        """Has spend moved enough to be worth another report? A job
        that has bought nothing reports nothing — most jobs are local,
        and a stream of `spentUsd: 0` would be noise."""
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
    """The machine's rate limiter, persisted across restarts.

    Wraps compute's `TokenBucketLimiter` (the bucket math and the
    header learning live there, task 000344) and adds two things a
    long-lived process needs: one instance for every job in this
    process, and state that survives a restart — a runner that
    restarts into a 429 window must not walk straight back into it.
    """

    def __init__(self, path: Path | None = None, *, save_every: float = 5.0) -> None:
        from mechbench_compute.providers.registry import TokenBucketLimiter

        self.path = path
        self._inner = TokenBucketLimiter()
        self._lock = threading.RLock()
        self._save_every = save_every
        self._last_save = 0.0
        self.load()

    # --- the Limiter interface (delegated, then persisted) -----------------

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
        # A hold is exactly the state worth surviving a restart.
        self.save()

    def release(self, provider: str, model: str, scope: str, currency: str,
                amount: float) -> None:
        self._inner.release(provider, model, scope, currency, amount)

    # --- what `mechbench status` shows -------------------------------------

    def snapshot(self) -> dict[str, Any]:
        """Buckets and holds, as numbers a person can read."""
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

    # --- persistence ---------------------------------------------------------

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
                # Holds are stored as WALL-clock deadlines: monotonic
                # time restarts with the process, and "held for another
                # nine seconds" has to mean nine more seconds.
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
                # Persistence is an optimization; losing it costs one
                # window of over-eagerness, never the job.
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
