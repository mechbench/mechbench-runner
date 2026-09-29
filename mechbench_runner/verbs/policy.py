from __future__ import annotations

import json
import pathlib
from collections.abc import Mapping
from typing import Any

from .core import (
    CONFIRMED,
    LIMIT,
    OFFSET,
    SEARCH,
    Arg,
    Ctx,
    Noun,
    Verb,
    VerbError,
    paged,
)

POLICY_ID = Arg("id", "The policy's id (`pol_…`).", required=True, positional=True)
BODY = Arg(
    "body",
    "The whole policy: a JSON file, or the JSON itself ({extensions: {install, allow, "
    "network}, upgrades: {compute}, gc: {unused_days}, pools}).",
    required=True,
)
REACH = Arg(
    "yes",
    "Make the change, not just name the runners it reaches.",
    type="bool",
)


def body_of(a: Mapping[str, Any]) -> Any:
    raw = a.get("body")
    if isinstance(raw, (dict, list)):
        return raw
    text = str(raw)
    p = pathlib.Path(text)
    if not text.lstrip().startswith(("{", "[")) and p.is_file():
        text = p.read_text()
    try:
        return json.loads(text)
    except ValueError as e:
        raise VerbError(f"body is neither a JSON file nor JSON: {e}") from None


def named(r: Mapping[str, Any]) -> dict[str, Any]:
    return {"id": r.get("id"), "name": r.get("name"), "connected": bool(r.get("connected"))}


def runners_under(ctx: Ctx, policy_id: str) -> list[dict[str, Any]]:
    rows = ctx.get("/runners")
    rows = rows if isinstance(rows, list) else []
    return [
        named(r) for r in rows
        if isinstance(r, Mapping) and ((r.get("policy") or {}).get("id") == policy_id)
    ]


def consent_line(action: str, reaches: list[dict[str, Any]]) -> str:
    if not reaches:
        return f"{action} reaches none of your runners"
    names = ", ".join(f"{r['name']} ({r['id']})" for r in reaches)
    return f"{action} reaches {len(reaches)} runner{'s' if len(reaches) != 1 else ''}: {names}"


def policy_list(ctx: Ctx, a: dict) -> Any:
    rows = ctx.get("/policies")
    rows = rows if isinstance(rows, list) else []
    s = str(a.get("search") or "").lower()
    if s:
        rows = [r for r in rows if s in str(r.get("name", "")).lower() or s in str(r.get("id", "")).lower()]
    return paged(rows, a)


def policy_create(ctx: Ctx, a: dict) -> Any:
    req: dict[str, Any] = {"name": a["name"], "body": body_of(a)}
    if a.get("org_id"):
        req["orgId"] = a["org_id"]
    return ctx.api("POST", "/policies", body=req)[0]


def policy_update(ctx: Ctx, a: dict) -> Any:
    req: dict[str, Any] = {"body": body_of(a)}
    if a.get("name"):
        req["name"] = a["name"]
    reaches = runners_under(ctx, str(a["id"]))
    line = consent_line(f"a new version of {a['id']}", reaches)
    if not a.get("yes"):
        return {"updated": False, "reaches": reaches, "consent": line, "would": req}
    out = ctx.api("PUT", f"/policies/{a['id']}", body=req)[0]
    return {"updated": True, "reaches": reaches, "consent": line, "policy": out}


def policy_apply(ctx: Ctx, a: dict) -> Any:
    runner, policy = str(a["runner"]), str(a["policy"])
    rows = ctx.get("/runners")
    rows = rows if isinstance(rows, list) else []
    reaches = [named(r) for r in rows if isinstance(r, Mapping) and r.get("id") == runner] or [
        {"id": runner, "name": runner, "connected": False}
    ]
    line = consent_line(f"putting it under {policy}", reaches)
    if not a.get("yes"):
        return {"applied": False, "reaches": reaches, "consent": line}
    out = ctx.api("PUT", f"/runners/{runner}/policy", body={"policyId": policy})[0]
    return {"applied": True, "reaches": reaches, "consent": line, "runner": out}


POLICY = Noun(
    "policy",
    "A policy: what the runners under it may install, whether they upgrade "
    "compute themselves, and whose jobs they serve. A new version reaches every "
    "runner under it at once.",
    (
        Verb(
            "policy",
            "list",
            "The policies you can see: your own, your orgs', the system's.",
            "GET /policies",
            (SEARCH, LIMIT, OFFSET),
            policy_list,
            "list",
            ("id", "name", "version", "editable"),
            effect="read",
        ),
        Verb(
            "policy",
            "read",
            "A policy with every version, newest first.",
            "GET /policies/:id",
            (POLICY_ID,),
            lambda ctx, a: ctx.get(f"/policies/{a['id']}"),
            "read",
            effect="read",
        ),
        Verb(
            "policy",
            "create",
            "Make a policy, at version 1; it reaches no runner until one is put under it.",
            "POST /policies",
            (
                Arg("name", "Its name, 1 to 80 characters.", required=True, positional=True),
                BODY,
                Arg("org_id", "An org's policy (you are its admin)."),
            ),
            policy_create,
            effect="draft",
        ),
        Verb(
            "policy",
            "update",
            "Give it a new version: names the runners it reaches, and changes "
            "nothing unless yes.",
            "PUT /policies/:id",
            (POLICY_ID, BODY, Arg("name", "Its new name."), REACH),
            policy_update,
            effect="outward",
            consent_when=CONFIRMED,
        ),
        Verb(
            "policy",
            "apply",
            "Put a runner under a policy: names the runner, and changes nothing unless yes.",
            "PUT /runners/:id/policy",
            (
                Arg("runner", "The runner's id.", required=True, positional=True),
                Arg("policy", "The policy's id (`pol_…`).", required=True, positional=True),
                REACH,
            ),
            policy_apply,
            effect="outward",
            consent_when=CONFIRMED,
        ),
    ),
    absent={
        "delete": "runners reference a policy by id and version; put them under another "
                  "(`policy apply`) instead",
        "history": "`policy read` carries every version, newest first",
    },
)
