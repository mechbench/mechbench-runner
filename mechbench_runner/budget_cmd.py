"""`mechbench budget` — show, set, or enforce the model-cache budget.

The policy machinery lives in budget.py (the runner's sweep uses the
same code); this is only its face. Showing the budget always shows the
consequence too: what a sweep would evict right now, so setting a
number is never a leap of faith.
"""

from __future__ import annotations

import time
from dataclasses import replace

from . import budget as budget_mod


def run(
    *,
    keep_free: float | None = None,
    max_cache: float | None = None,
    recent_days: float | None = None,
    off: bool = False,
    do_sweep: bool = False,
) -> int:
    if off:
        budget_mod.clear()
        print("Budget removed. The cache will grow until the disk is full.")
        return 0

    current = budget_mod.load()
    if keep_free is not None or max_cache is not None or recent_days is not None:
        updated = current
        if keep_free is not None:
            updated = replace(updated, keep_free_gb=keep_free or None)
        if max_cache is not None:
            updated = replace(updated, max_cache_gb=max_cache or None)
        if recent_days is not None:
            updated = replace(updated, recent_days=recent_days)
        budget_mod.save(updated)
        current = updated

    print(f"Budget: {budget_mod.describe(current)}")
    if not current.is_set:
        print(
            "  `mechbench budget --keep-free 100` keeps 100 GB of disk free;\n"
            "  `mechbench budget --max 300` caps the cache at 300 GB."
        )
        return 0

    todo = budget_mod.plan(current)
    print(
        f"Cache: {todo.cache_bytes / 1e9:.1f} GB; "
        f"{todo.free_bytes / 1e9:.0f} GB of disk free."
    )
    if todo.deficit_bytes <= 0:
        print("Within budget; nothing to evict.")
        return 0

    now = time.time()
    print(f"Over budget by {todo.deficit_bytes / 1e9:.1f} GB. "
          f"{'Evicting' if do_sweep else 'A sweep would evict'}:")
    for c in todo.evictions:
        age = (now - c.last_used) / 86400
        print(f"    {c.kind}  {c.name}  {c.size_bytes / 1e9:.1f} GB  "
              f"last used {age:.0f} days ago")
    if todo.short_bytes > 0:
        print(
            f"    …and would still be {todo.short_bytes / 1e9:.1f} GB over — "
            f"the rest was used within {current.recent_days:g} days, and "
            f"recent beats the budget."
        )
    if not do_sweep:
        print("Run `mechbench budget --sweep` to evict now; "
              "the runner also sweeps before each job.")
        return 0

    evicted = budget_mod.sweep(current, say=print)
    freed = sum(c.size_bytes for c in evicted)
    print(f"Evicted {len(evicted)} item{'s' if len(evicted) != 1 else ''}; "
          f"{freed / 1e9:.1f} GB returned.")
    return 0
