from __future__ import annotations

import json
import time
from collections.abc import Mapping
from typing import Any

from .core import Ctx, VerbError

SHOWN = 160
WAIT_SECONDS = 120.0
POLL_SECONDS = 2.0
FINISHED = ("done", "done_with_missing")
ENDED = ("failed", "cancelled")


def split_list(value: Any) -> list[str] | None:
    if value is None:
        return None
    parts = value.split(",") if isinstance(value, str) else list(value)
    out = [str(p).strip() for p in parts if str(p).strip()]
    return out or None


def locate(ctx: Ctx, side: str, node: str | None) -> dict[str, Any]:
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
    obj = ctx.bench().fetch_envelope(where["path"])
    if isinstance(obj, dict) and "payload" in obj and "provenance" in obj:
        return obj["payload"], obj["provenance"]
    return obj, None


def read_json(value: Any, name: str) -> Any:
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except ValueError as e:
        raise VerbError(f"{name} is not JSON: {e}") from None


def read_map(value: Any, name: str) -> dict[str, Any] | None:
    got = read_json(value, name)
    if got is None:
        return None
    if not isinstance(got, Mapping):
        raise VerbError(f"{name} is NAME=VALUE pairs, or a JSON object")
    return dict(got)


def params_of(a: Mapping[str, Any]) -> dict[str, Any]:
    params = {
        "key": split_list(a.get("key")) or "id",
        "fields": split_list(a.get("fields")),
        "exclude": split_list(a.get("exclude")),
        "exclude_moving": not a.get("include_moving"),
        "allow": read_json(a.get("allow"), "allow"),
        "by": split_list(a.get("by")),
    }
    tolerance = read_json(a.get("tolerance"), "tolerance")
    if tolerance is not None:
        params["tolerance"] = tolerance
    if a.get("k") is not None:
        params["k"] = float(a["k"])
    noise_for = read_map(a.get("noise_for"), "noise_for")
    if noise_for:
        params["noise_for"] = noise_for
    return params


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
    old, new = change["a"], change["b"]
    return {
        "relation": "extends",
        "a_chars": len(old),
        "b_chars": len(new),
        "a_ends": old[-60:],
        "added": new[len(old) :],
    }


def shaped(
    out: Mapping[str, Any], sides: Mapping[str, Mapping[str, Any]], a: Mapping[str, Any]
) -> dict[str, Any]:
    summary = dict(out["diff"])
    for side, where in sides.items():
        summary[side] = {**where, **summary[side]}
    records = list(out.get("items") or [])
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


def route_body(a: Mapping[str, Any]) -> dict[str, Any]:
    params = {k: v for k, v in params_of(a).items() if v is not None}
    body: dict[str, Any] = {"a": a["a"], "b": a["b"], "params": params}
    for name, field in (
        ("node", "node"),
        ("node_b", "nodeB"),
        ("noise", "noise"),
        ("into", "into"),
    ):
        if a.get(name) is not None:
            body[field] = a[name]
    return body


def kept(ctx: Ctx, a: Mapping[str, Any]) -> dict[str, Any]:
    job = ctx.api("POST", "/runs/diff", body=route_body(a))[0]
    deadline = time.monotonic() + WAIT_SECONDS
    while job.get("status") not in FINISHED + ENDED and time.monotonic() < deadline:
        time.sleep(POLL_SECONDS)
        job = ctx.get(f"/jobs/{job['id']}")
    where = (job.get("spec") or {}).get("diff") or {}
    result = job.get("result") or where.get("result")
    if job.get("status") in ENDED:
        why = job.get("errorMessage") or "no reason given"
        raise VerbError(f"the diff {job['id']} {job['status']}: {why}")
    if job.get("status") not in FINISHED:
        return {
            "finished": False,
            "job": job["id"],
            "status": job.get("status"),
            "result": result,
            "placement": job.get("placement"),
            "note": "the diff is a job on one of your runners: ask again with the same "
            "arguments to keep waiting for it, or read its result when it is done",
        }
    payload, _ = fetch_side(ctx, {"path": result})
    sides = {
        side: {k: v for k, v in (where.get(side) or {}).items() if k != "hash"}
        for side in ("a", "b")
    }
    return {"job": job["id"], "result": result, **shaped(payload, sides, a)}


def run_diff(ctx: Ctx, a: dict) -> dict[str, Any]:
    if a.get("into") is not None:
        return kept(ctx, a)
    from mechbench_compute.ops.records.diff import diff_collections

    first = locate(ctx, a["a"], a.get("node"))
    second = locate(ctx, a["b"], a.get("node_b") or a.get("node"))
    pa, prov_a = fetch_side(ctx, first)
    pb, prov_b = fetch_side(ctx, second)
    noise = fetch_side(ctx, {"path": a["noise"]})[0] if a.get("noise") else None
    out = diff_collections(
        pa, pb, params_of(a), provenance=(prov_a, prov_b), noise=noise
    )
    summary = shaped(out, {"a": first, "b": second}, a)
    for side, prov in (("a", prov_a), ("b", prov_b)):
        produced = (prov or {}).get("produced_by") or {}
        summary[side] = {**summary[side], "compute": produced.get("version")}
    return summary
