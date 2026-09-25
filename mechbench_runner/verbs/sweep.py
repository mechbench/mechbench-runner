from __future__ import annotations

import json
import pathlib
import time
from collections.abc import Mapping
from typing import Any

from .core import Ctx, VerbError
from .run_diff import read_json

TERMINAL = ("done", "done_with_missing", "failed", "cancelled", "interrupted")
BODY_KEYS = ("params", "inputs", "members", "grid", "label", "keep", "budgetUsd")


def as_ref(v: Any) -> Any:
    return {"$ref": {"bench": v}} if isinstance(v, str) else v


def refs(inputs: Mapping[str, Any] | None) -> dict[str, Any]:
    return {k: as_ref(v) for k, v in (inputs or {}).items()}


def values_of(v: Any, name: str) -> list[Any]:
    if isinstance(v, list):
        return v
    if not isinstance(v, str):
        return [v]
    text = v.strip()
    if text.startswith("["):
        got = read_json(text, f"grid {name}")
        if not isinstance(got, list):
            raise VerbError(f"grid {name} is not a list")
        return got
    out: list[Any] = []
    for part in text.split(","):
        try:
            out.append(json.loads(part))
        except ValueError:
            out.append(part)
    return out


def read_file(path: str) -> dict[str, Any]:
    text = pathlib.Path(path).read_text()
    if path.endswith((".yaml", ".yml")):
        try:
            import yaml
        except ImportError:
            raise VerbError(
                f"{path}: reading YAML needs PyYAML (pip install pyyaml); "
                "or write the sweep as JSON"
            ) from None
        data = yaml.safe_load(text)
    else:
        data = read_json(text, path)
    if isinstance(data, list):
        return {"members": data}
    if not isinstance(data, dict):
        raise VerbError(
            f"{path} is a list of members or a sweep, not {type(data).__name__}"
        )
    unknown = sorted(set(data) - set(BODY_KEYS))
    if unknown:
        raise VerbError(
            f"{path}: a sweep takes {', '.join(BODY_KEYS)}; not {', '.join(unknown)}"
        )
    return dict(data)


def member_of(m: Any, where: str) -> dict[str, Any]:
    if not isinstance(m, dict) or set(m) - {"params", "inputs"}:
        raise VerbError(f"{where}: a member is {{params, inputs}}")
    out: dict[str, Any] = {}
    if m.get("params"):
        out["params"] = dict(m["params"])
    if m.get("inputs"):
        out["inputs"] = refs(m["inputs"])
    return out


def sweep_body(a: Mapping[str, Any]) -> dict[str, Any]:
    body = read_file(a["file"]) if a.get("file") else {}
    members = read_json(a.get("members"), "members")
    if members is not None:
        body["members"] = members
    params = {**(body.get("params") or {}), **(a.get("params") or {})}
    inputs = {**(body.get("inputs") or {}), **(a.get("inputs") or {})}
    grid = dict(body.get("grid") or {})
    grid_params = {
        **(grid.get("params") or {}),
        **{k: values_of(v, k) for k, v in (a.get("grid") or {}).items()},
    }
    grid_inputs = {
        **{k: [as_ref(x) for x in xs] for k, xs in (grid.get("inputs") or {}).items()},
        **{
            k: [as_ref(x) for x in values_of(v, k)]
            for k, v in (a.get("grid_inputs") or {}).items()
        },
    }
    out: dict[str, Any] = {}
    if params:
        out["params"] = params
    if inputs:
        out["inputs"] = refs(inputs)
    if body.get("members") is not None:
        if not isinstance(body["members"], list):
            raise VerbError("members is a list of {params, inputs}")
        out["members"] = [
            member_of(m, f"member {i}") for i, m in enumerate(body["members"])
        ]
    if grid_params or grid_inputs:
        out["grid"] = {
            **({"params": grid_params} if grid_params else {}),
            **({"inputs": grid_inputs} if grid_inputs else {}),
        }
    if ("members" in out) == ("grid" in out):
        raise VerbError(
            "a sweep takes members (a file or members) or a grid (--grid), not "
            + ("both" if "members" in out else "neither")
        )
    label = a.get("label") or body.get("label")
    keep = a.get("keep") or body.get("keep")
    budget = a.get("budget") if a.get("budget") is not None else body.get("budgetUsd")
    if label:
        out["label"] = label
    if keep:
        out["keep"] = keep
    if budget is not None:
        out["budgetUsd"] = budget
    return out


def sweep_rows(ctx: Ctx, sweep_id: str) -> list[dict[str, Any]]:
    data, _ = ctx.api(
        "GET", "/runs", query={"sweep": sweep_id, "limit": 500, "view": "summary"}
    )
    return list(data) if isinstance(data, list) else []


def run_sweep(ctx: Ctx, a: dict) -> Any:
    body = sweep_body(a)
    runs, _ = ctx.api("POST", f"/protocols/{a['protocol']}/sweeps", body=body)
    runs = list(runs) if isinstance(runs, list) else []
    sweep_id = next((r.get("sweepId") for r in runs if r.get("sweepId")), None)
    if not a.get("wait") or not sweep_id:
        return {"sweepId": sweep_id, "runs": runs}
    deadline = time.monotonic() + float(a.get("timeout") or 3600)
    while True:
        rows = sweep_rows(ctx, sweep_id)
        left = deadline - time.monotonic()
        finished = bool(rows) and all(r.get("jobStatus") in TERMINAL for r in rows)
        if finished or left <= 0:
            return {"sweepId": sweep_id, "finished": finished, "runs": rows}
        time.sleep(min(10.0, max(left, 0.0)))
