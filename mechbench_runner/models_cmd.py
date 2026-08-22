"""`mechbench-runner models` — what is cached, and what pruning buys.

Thin on purpose: the inventory lives in `mechbench_compute.inventory`
because the web UI wants the same answer, and neither side should be
reading the HuggingFace cache layout itself.
"""

from __future__ import annotations

import sys


def run(*, prune: bool = False, delete: list[str] | None = None) -> int:
    try:
        from mechbench_compute import inventory
    except ImportError as exc:
        print(f"mechbench-compute is not installed ({exc})", file=sys.stderr)
        return 1

    if delete:
        return _delete(inventory, delete)

    repos = inventory.scan()
    if not repos:
        print("No models cached yet.")
        return 0

    for repo in repos:
        print(f"{repo.repo_id}  {inventory.format_bytes(repo.disk_bytes)}")
        for rev in repo.revisions:
            refs = ", ".join(rev.refs) if rev.refs else "—"
            when = (
                rev.last_modified.strftime("%Y-%m-%d")
                if rev.last_modified
                else "unknown"
            )
            print(
                f"    {rev.short}  {inventory.format_bytes(rev.size_bytes):>9}"
                f"  frees {inventory.format_bytes(rev.reclaimable_bytes):>9}"
                f"  {when}  {refs}"
            )
        print()

    total = sum(r.disk_bytes for r in repos)
    reclaimable = sum(r.reclaimable_bytes for r in repos)
    superseded = [(r.repo_id, rev) for r in repos for rev in r.superseded]
    print(f"{len(repos)} models, {inventory.format_bytes(total)} on disk.")

    if not superseded:
        print("Nothing is superseded.")
        return 0

    # The two numbers that matter, and the reason they differ. Revisions
    # of one repository share their blobs, so the apparent size of a
    # superseded revision is mostly weights its siblings also use.
    apparent = sum(rev.size_bytes for _, rev in superseded)
    print(
        f"{len(superseded)} revisions are not pointed at by any ref. They look "
        f"like {inventory.format_bytes(apparent)}, but they share their weights "
        f"with the revisions that are kept, so deleting them all returns "
        f"{inventory.format_bytes(reclaimable)}."
    )

    if not prune:
        print("\nRun with --prune to delete them.")
        return 0

    print()
    return _delete(inventory, [rev.commit for _, rev in superseded])


def _delete(inventory, commits: list[str]) -> int:
    try:
        freed = inventory.delete_revisions(commits)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(
        f"Deleted {len(commits)} revision{'s' if len(commits) != 1 else ''}; "
        f"{inventory.format_bytes(freed)} returned."
    )
    return 0
