from __future__ import annotations

import json
import shutil
import time
import tomllib
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import paths

BUDGET_NAME = "budget.toml"
USAGE_NAME = "model-usage.json"
DEFAULT_RECENT_DAYS = 3.0


@dataclass(frozen=True)
class Budget:
    keep_free_gb: float | None = None
    max_cache_gb: float | None = None
    recent_days: float = DEFAULT_RECENT_DAYS

    @property
    def is_set(self) -> bool:
        return self.keep_free_gb is not None or self.max_cache_gb is not None


def budget_path(root: Path | None = None) -> Path:
    return (root or paths.mechbench_dir()) / BUDGET_NAME


def load(root: Path | None = None) -> Budget:
    p = budget_path(root)
    try:
        data = tomllib.loads(p.read_text())
    except (OSError, tomllib.TOMLDecodeError):
        return Budget()
    def _num(key: str) -> float | None:
        v = data.get(key)
        return float(v) if isinstance(v, (int, float)) else None
    return Budget(
        keep_free_gb=_num("keep_free_gb"),
        max_cache_gb=_num("max_cache_gb"),
        recent_days=_num("recent_days") or DEFAULT_RECENT_DAYS,
    )


def save(budget: Budget, root: Path | None = None) -> Path:
    p = budget_path(root)
    lines = ["# Written by `mechbench budget`."]
    if budget.keep_free_gb is not None:
        lines.append(f"keep_free_gb = {budget.keep_free_gb}")
    if budget.max_cache_gb is not None:
        lines.append(f"max_cache_gb = {budget.max_cache_gb}")
    lines.append(f"recent_days = {budget.recent_days}")
    p.write_text("\n".join(lines) + "\n")
    return p


def clear(root: Path | None = None) -> None:
    with suppress(OSError):
        budget_path(root).unlink()


def usage_path(root: Path | None = None) -> Path:
    return (root or paths.mechbench_dir()) / USAGE_NAME


def repo_of(model_id: str) -> str:
    return model_id.split("@", 1)[0]


def record_use(model_id: str, root: Path | None = None,
               now: float | None = None) -> None:
    with suppress(Exception):
        p = usage_path(root)
        journal = _read_journal(p)
        journal[repo_of(model_id)] = now if now is not None else time.time()
        p.write_text(json.dumps(journal, indent=0, sort_keys=True))


def _read_journal(p: Path) -> dict[str, float]:
    try:
        data = json.loads(p.read_text())
    except (OSError, ValueError):
        return {}
    return {k: float(v) for k, v in data.items()
            if isinstance(v, (int, float))} if isinstance(data, dict) else {}


@dataclass(frozen=True)
class Candidate:
    kind: str
    name: str
    size_bytes: int
    last_used: float
    commits: tuple[str, ...] = ()
    path: Path | None = None


def checkpoint_candidates(root: Path | None = None) -> list[Candidate]:
    ckpt_root = root if root is not None else paths.checkpoints_dir()
    out: list[Candidate] = []
    if not ckpt_root.is_dir():
        return out
    for entry in sorted(ckpt_root.iterdir()):
        if not entry.is_dir():
            continue
        size = sum(f.stat().st_size for f in entry.rglob("*") if f.is_file())
        mark = entry / ".complete"
        stamp = mark if mark.exists() else entry
        label = entry.name
        with suppress(OSError):
            label = (entry / ".label").read_text().strip() or label
        out.append(Candidate(
            kind="checkpoint", name=label, size_bytes=size,
            last_used=stamp.stat().st_mtime, path=entry,
        ))
    return out


def model_candidates(inventory: Any, journal: dict[str, float]) -> list[Candidate]:
    out: list[Candidate] = []
    for repo in inventory.scan():
        stamps = [journal.get(repo.repo_id, 0.0)]
        stamps += [rev.last_modified.timestamp()
                   for rev in repo.revisions if rev.last_modified]
        out.append(Candidate(
            kind="model", name=repo.repo_id, size_bytes=repo.disk_bytes,
            last_used=max(stamps),
            commits=tuple(rev.commit for rev in repo.revisions),
        ))
    return out


@dataclass(frozen=True)
class Plan:
    deficit_bytes: int
    evictions: list[Candidate]
    short_bytes: int
    cache_bytes: int
    free_bytes: int


def plan(
    budget: Budget | None = None,
    *,
    protect: frozenset[str] | set[str] = frozenset(),
    root: Path | None = None,
    checkpoints_root: Path | None = None,
    inventory: Any = None,
    disk_usage: Callable[[str], Any] = shutil.disk_usage,
    now: float | None = None,
) -> Plan:
    b = budget if budget is not None else load(root)
    if not b.is_set:
        return Plan(0, [], 0, 0, 0)
    if inventory is None:
        try:
            from mechbench_compute import inventory as inventory_mod
            inventory = inventory_mod
        except ImportError:
            inventory = None

    models = (
        model_candidates(inventory, _read_journal(usage_path(root)))
        if inventory is not None else []
    )
    checkpoints = checkpoint_candidates(checkpoints_root)
    cache_bytes = sum(c.size_bytes for c in models + checkpoints)

    probe = _cache_volume(inventory)
    free_bytes = int(disk_usage(str(probe)).free)

    deficit = 0
    if b.keep_free_gb is not None:
        deficit = max(deficit, int(b.keep_free_gb * 1e9) - free_bytes)
    if b.max_cache_gb is not None:
        deficit = max(deficit, cache_bytes - int(b.max_cache_gb * 1e9))
    if deficit <= 0:
        return Plan(0, [], 0, cache_bytes, free_bytes)

    horizon = (now if now is not None else time.time()) - b.recent_days * 86400
    evictable = sorted(
        (c for c in models + checkpoints
         if c.name not in protect and c.last_used < horizon),
        key=lambda c: c.last_used,
    )
    chosen: list[Candidate] = []
    covered = 0
    for c in evictable:
        if covered >= deficit:
            break
        chosen.append(c)
        covered += c.size_bytes
    return Plan(deficit, chosen, max(0, deficit - covered),
                cache_bytes, free_bytes)


def _cache_volume(inventory: Any) -> Path:
    try:
        from mechbench_compute.hub import hf_hub_cache

        p = Path(hf_hub_cache())
    except Exception:  # noqa: BLE001
        p = Path.home()
    while not p.exists() and p != p.parent:
        p = p.parent
    return p


def sweep(
    budget: Budget | None = None,
    *,
    protect: frozenset[str] | set[str] = frozenset(),
    say: Callable[[str], None] = lambda _m: None,
    emit: Callable[[str, dict], None] | None = None,
    root: Path | None = None,
    checkpoints_root: Path | None = None,
    inventory: Any = None,
    disk_usage: Callable[[str], Any] = shutil.disk_usage,
    now: float | None = None,
) -> list[Candidate]:
    if inventory is None:
        try:
            from mechbench_compute import inventory as inventory_mod
            inventory = inventory_mod
        except ImportError:
            inventory = None
    todo = plan(
        budget, protect=protect, root=root, checkpoints_root=checkpoints_root,
        inventory=inventory, disk_usage=disk_usage, now=now,
    )
    if not todo.evictions and todo.short_bytes == 0:
        return []

    done: list[Candidate] = []
    for c in todo.evictions:
        try:
            if c.kind == "model" and inventory is not None:
                inventory.delete_revisions(list(c.commits))
            elif c.kind == "checkpoint" and c.path is not None:
                shutil.rmtree(c.path)
            else:
                continue
        except Exception as exc:  # noqa: BLE001
            say(f"could not evict {c.kind} {c.name}: {exc}")
            continue
        done.append(c)
        say(f"evicted {c.kind} {c.name} "
            f"({c.size_bytes / 1e9:.1f} GB, unused for "
            f"{_age_days(c.last_used, now):.0f} days)")
        if emit is not None:
            with suppress(Exception):
                emit("cache.evicted", {
                    "kind": c.kind, "name": c.name, "bytes": c.size_bytes,
                    "last_used": c.last_used,
                })
    if todo.short_bytes > 0:
        say(f"budget still exceeded by {todo.short_bytes / 1e9:.1f} GB — "
            f"everything else was used in the last "
            f"{(budget or load(root)).recent_days:.0f} days, and recent "
            f"beats the budget (evicting it would thrash).")
    return done


def _age_days(last_used: float, now: float | None) -> float:
    return max(0.0, ((now if now is not None else time.time()) - last_used) / 86400)


def describe(budget: Budget) -> str:
    if not budget.is_set:
        return "no budget set"
    parts = []
    if budget.keep_free_gb is not None:
        parts.append(f"keep {budget.keep_free_gb:g} GB of disk free")
    if budget.max_cache_gb is not None:
        parts.append(f"cap the cache at {budget.max_cache_gb:g} GB")
    return (" and ".join(parts) +
            f" (nothing used in the last {budget.recent_days:g} days is evicted)")


__all__ = [
    "Budget", "Candidate", "Plan", "budget_path", "checkpoint_candidates",
    "clear", "describe", "load", "model_candidates", "plan", "record_use",
    "repo_of", "save", "sweep", "usage_path",
]
