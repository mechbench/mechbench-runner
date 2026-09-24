"""`run diff`: two runs, or two nodes of them, compared record by record.

The comparison is compute's `records/diff`, the same function a protocol
node runs, so the verb and the operation cannot disagree. What the verb
adds is the fetching: each side is a run (its id or its job's) and a
node, or an object path, read with its envelope so the two results'
provenance is compared too.

It runs where the command runs, not in the API: the API is one small
instance, and a comparison reads two whole collections. A run is not
queued for it either, since a comparison asked at the command line is a
question, not a result to keep; a protocol that should keep one names
`records/diff` as a node.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from .core import Ctx, VerbError

#: How long a string is shown before it is abbreviated, unless `full`.
SHOWN = 160


def split_list(value: Any) -> list[str] | None:
    """A comma-separated string or a list, as a list; None when empty."""
    if value is None:
        return None
    parts = value.split(",") if isinstance(value, str) else list(value)
    out = [str(p).strip() for p in parts if str(p).strip()]
    return out or None


def locate(ctx: Ctx, side: str, node: str | None) -> dict[str, Any]:
    """Where one side's result is: an object path as given, or a finished
    run's result path and the node."""
    if "/" in side:
        return {"path": side}
    if not node:
        raise VerbError(f"{side} is a run: name its node (node, or node_b)")
    from .run import finished

    row = finished(ctx, side)
    base = row.get("resultPath")
    if not base:
        raise VerbError(f"run {side} has no result path")
    return {"run": row.get("jobId") or side, "node": node, "path": f"{base}/{node}"}


def fetch_side(ctx: Ctx, where: Mapping[str, Any]) -> tuple[Any, Any]:
    """The payload and its provenance (None for an object stored bare)."""
    obj = ctx.bench().fetch_envelope(where["path"])
    if isinstance(obj, dict) and "payload" in obj and "provenance" in obj:
        return obj["payload"], obj["provenance"]
    return obj, None


def read_json(value: Any, name: str) -> Any:
    """A json argument: parsed when it came as text (the command line)."""
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except ValueError as e:
        raise VerbError(f"{name} is not JSON: {e}") from None


def params_of(a: Mapping[str, Any]) -> dict[str, Any]:
    """The verb's arguments as `records/diff`'s parameters."""
    return {
        "key": split_list(a.get("key")) or "id",
        "fields": split_list(a.get("fields")),
        "exclude": split_list(a.get("exclude")),
        "exclude_moving": not a.get("include_moving"),
        "allow": read_json(a.get("allow"), "allow"),
        "by": split_list(a.get("by")),
    }


def abbreviate(value: Any, width: int = SHOWN) -> Any:
    if isinstance(value, str) and len(value) > width:
        half = width // 2
        return f"{value[:half]} … {value[-half:]} ({len(value)} chars)"
    if isinstance(value, dict):
        return {k: abbreviate(v, width) for k, v in value.items()}
    if isinstance(value, list):
        return [abbreviate(v, width) for v in value]
    return value


def show_extension(change: Mapping[str, Any]) -> dict[str, Any]:
    """An `extends` change as where the old text stopped and what follows."""
    old, new = change["a"], change["b"]
    return {
        "relation": "extends",
        "a_chars": len(old),
        "b_chars": len(new),
        "a_ends": old[-60:],
        "added": new[len(old) :],
    }


def run_diff(ctx: Ctx, a: dict) -> dict[str, Any]:
    from mechbench_compute.ops.records.diff import diff_collections

    first = locate(ctx, a["a"], a.get("node"))
    second = locate(ctx, a["b"], a.get("node_b") or a.get("node"))
    pa, prov_a = fetch_side(ctx, first)
    pb, prov_b = fetch_side(ctx, second)
    out = diff_collections(pa, pb, params_of(a), provenance=(prov_a, prov_b))
    summary = dict(out["diff"])
    for side, where, prov in (("a", first, prov_a), ("b", second, prov_b)):
        produced = (prov or {}).get("produced_by") or {}
        summary[side] = {**where, **summary[side], "compute": produced.get("version")}
    records = out["items"]
    limit = a.get("limit")
    limit = 20 if limit is None else int(limit)
    shown = records if limit < 0 else records[:limit]
    if not a.get("full"):
        shown = [
            {
                **r,
                "fields": {
                    p: abbreviate(show_extension(c))
                    if c["relation"] == "extends"
                    else abbreviate(c)
                    for p, c in r["fields"].items()
                },
            }
            if "fields" in r
            else r
            for r in shown
        ]
    summary["shown"] = shown
    summary["not_shown"] = len(records) - len(shown)
    return summary
