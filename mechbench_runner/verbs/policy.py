from __future__ import annotations

import json
import pathlib
from collections.abc import Mapping
from typing import Any

from ..policy import (
    ADMIT_OPTIONS,
    ADMIT_SOURCE_WORDS,
    ADMIT_SOURCES,
    ORG_POLICY_ID,
    SERVE_OPTIONS,
    SERVE_SOURCE_WORDS,
    SERVE_SOURCES,
    PolicyOption,
    default_policy_for,
    is_current,
    migrate_policy,
    policy_option_of,
)
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
    "A whole policy to start from: a JSON file, or the JSON itself ({jobs: {serve, allow}, "
    "extensions: {admit, allow, network}, upgrades: {compute}, gc: {unused_days}}). The "
    "other arguments change it.",
)


def _option_names(options: tuple[PolicyOption, ...]) -> str:
    return "; ".join(f"`{o.name}` ({', '.join(o.sources) or 'nothing'})" for o in options)


SERVE = Arg(
    "serve",
    "Whose protocols its runners run: an option, " + _option_names(SERVE_OPTIONS)
    + "; or sources joined by commas, from " + ", ".join(SERVE_SOURCES) + ".",
)
ADMIT = Arg(
    "admit",
    "Whose extensions its runners install: an option, " + _option_names(ADMIT_OPTIONS)
    + "; or sources joined by commas, from " + ", ".join(ADMIT_SOURCES) + ".",
)
SERVE_ALLOW = Arg(
    "serve_allow",
    "The jobs `listed` serves, the whole list: each entry `owner=<user id>`, "
    "`org=<org id>` and/or `project=<project id>`, joined by commas. Repeat for each "
    "entry; `none` empties it.",
    type="strs",
)
ADMIT_ALLOW = Arg(
    "admit_allow",
    "The extensions `listed` admits, the whole list: each entry `owner=<user id>`, "
    "`org=<org id>` and/or `extension=<address>`, joined by commas. Repeat for each "
    "entry; `none` empties it.",
    type="strs",
)
NETWORK = Arg(
    "network",
    "none refuses any extension that declares a network need; declared lets one install.",
    choices=("none", "declared"),
)
UPGRADES = Arg("upgrades", "Whether its runners upgrade compute themselves.",
               choices=("auto", "hold"))
UNUSED_DAYS = Arg("unused_days", "Uninstall an extension no job has named for this many days.",
                  type="int")
REACH = Arg(
    "yes",
    "Make the change, not just name the runners it reaches.",
    type="bool",
)
SHAPE = (BODY, SERVE, ADMIT, SERVE_ALLOW, ADMIT_ALLOW, NETWORK, UPGRADES, UNUSED_DAYS)


def body_of(a: Mapping[str, Any]) -> Any:
    raw = a.get("body")
    if raw is None:
        return None
    if isinstance(raw, (dict, list)):
        return raw
    text = str(raw)
    p = pathlib.Path(text)
    if not text.lstrip().startswith(("{", "[")) and p.is_file():
        text = p.read_text()
    try:
        got = json.loads(text)
    except ValueError as e:
        raise VerbError(f"body is neither a JSON file nor JSON: {e}") from None
    if not is_current(got):
        raise VerbError("body is not a policy in the current shape: one names "
                        "jobs.serve and extensions.admit")
    return got


def machine_of(row: Mapping[str, Any]) -> str:
    return "org" if row.get("orgId") or row.get("id") == ORG_POLICY_ID else "user"


MACHINE_WORDS = {"user": "a person's machine", "org": "an org's machine"}


def sources_of(text: str, options: tuple[PolicyOption, ...], known: tuple[str, ...],
               axis: str, kind: str) -> list[str]:
    said = text.strip()
    named = [o for o in options if o.name.lower() == said.lower()]
    if named:
        fit = [o for o in named if kind in o.machines]
        if not fit:
            other = ", ".join(f"`{o.name}`" for o in options if kind in o.machines)
            raise VerbError(f"{axis} `{said}` is not offered on {MACHINE_WORDS[kind]}; "
                            f"it offers {other}")
        return list(fit[0].sources)
    if said.lower() in ("", "none", "nothing", "nobody"):
        return []
    got = [w.strip() for w in said.split(",") if w.strip()]
    unknown = [w for w in got if w not in known]
    if unknown:
        raise VerbError(f"{axis} {', '.join(unknown)}: neither an option ("
                        + ", ".join(f"`{o.name}`" for o in options if kind in o.machines)
                        + ") nor a source (" + ", ".join(known) + ")")
    if len(set(got)) != len(got):
        raise VerbError(f"{axis} names a source twice")
    return got


def allow_of(entries: Any, fields: tuple[str, ...], axis: str) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for text in entries or []:
        if str(text).strip().lower() == "none":
            continue
        entry: dict[str, str] = {}
        for part in str(text).split(","):
            key, eq, value = part.strip().partition("=")
            if not eq or key not in fields or not value.strip():
                raise VerbError(f"{axis} entry {text!r}: each part is "
                                + ", ".join(f"{f}=<…>" for f in fields))
            entry[key] = value.strip()
        out.append(entry)
    return out


def shaped(base: Any, a: Mapping[str, Any], kind: str) -> dict[str, Any]:
    body = migrate_policy(base)
    jobs, ext = dict(body["jobs"]), dict(body["extensions"])
    if a.get("serve") is not None:
        jobs["serve"] = sources_of(str(a["serve"]), SERVE_OPTIONS, SERVE_SOURCES, "serve", kind)
    if a.get("admit") is not None:
        ext["admit"] = sources_of(str(a["admit"]), ADMIT_OPTIONS, ADMIT_SOURCES, "admit", kind)
    if a.get("serve_allow") is not None:
        jobs["allow"] = allow_of(a["serve_allow"], ("owner", "org", "project"), "serve_allow")
    if a.get("admit_allow") is not None:
        ext["allow"] = allow_of(a["admit_allow"], ("owner", "org", "extension"), "admit_allow")
    if a.get("network"):
        ext["network"] = a["network"]
    body = {**body, "jobs": jobs, "extensions": ext}
    if a.get("upgrades"):
        body["upgrades"] = {"compute": a["upgrades"]}
    if a.get("unused_days") is not None:
        body["gc"] = {"unused_days": int(a["unused_days"])}
    return body


def axis_words(sources: list[str], options: tuple[PolicyOption, ...],
               words: Mapping[str, str], kind: str) -> dict[str, Any]:
    option = policy_option_of(options, sources, kind)
    if option is not None:
        return {"option": option.name, "sources": list(option.sources),
                "means": option.description}
    return {"option": "Custom", "sources": list(sources),
            "means": [words[s] for s in sources if s in words]}


def shown(row: Mapping[str, Any]) -> dict[str, Any]:
    body = migrate_policy(row.get("body"))
    kind = machine_of(row)
    return {
        "machines": MACHINE_WORDS[kind],
        "serve": axis_words(body["jobs"]["serve"], SERVE_OPTIONS, SERVE_SOURCE_WORDS, kind),
        "serveAllow": body["jobs"]["allow"],
        "admit": axis_words(body["extensions"]["admit"], ADMIT_OPTIONS, ADMIT_SOURCE_WORDS,
                            kind),
        "admitAllow": body["extensions"]["allow"],
        "network": body["extensions"]["network"],
        "upgrades": body["upgrades"]["compute"],
        "unusedDays": body["gc"]["unused_days"],
    }


def listed_row(row: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(row, Mapping):
        return row
    s = shown(row)
    return {**row, "serve": s["serve"]["option"] if s["serve"]["option"] != "Custom"
            else "Custom: " + (", ".join(s["serve"]["sources"]) or "nothing"),
            "admit": s["admit"]["option"] if s["admit"]["option"] != "Custom"
            else "Custom: " + (", ".join(s["admit"]["sources"]) or "nothing")}


def policy_read(ctx: Ctx, a: dict) -> Any:
    got = ctx.get(f"/policies/{a['id']}")
    if not isinstance(got, Mapping):
        return got
    return {**got, "shown": shown(got)}


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
    rows = [listed_row(r) for r in rows] if isinstance(rows, list) else []
    s = str(a.get("search") or "").lower()
    if s:
        rows = [r for r in rows if s in str(r.get("name", "")).lower() or s in str(r.get("id", "")).lower()]
    return paged(rows, a)


def policy_create(ctx: Ctx, a: dict) -> Any:
    kind = "org" if a.get("org_id") else "user"
    base = body_of(a)
    if base is None:
        base = default_policy_for(kind)
    req: dict[str, Any] = {"name": a["name"], "body": shaped(base, a, kind)}
    if a.get("org_id"):
        req["orgId"] = a["org_id"]
    return ctx.api("POST", "/policies", body=req)[0]


def policy_update(ctx: Ctx, a: dict) -> Any:
    current = ctx.get(f"/policies/{a['id']}")
    current = current if isinstance(current, Mapping) else {}
    base = body_of(a)
    req: dict[str, Any] = {"body": shaped(base if base is not None else current.get("body"),
                                          a, machine_of(current))}
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
    "A policy: whose protocols the runners under it run (serve), whose extensions "
    "they install (admit), whether they upgrade compute themselves, and when they "
    "forget what they installed. A new version reaches every runner under it at once.",
    (
        Verb(
            "policy",
            "list",
            "The policies you can see: your own, your orgs', the system's.",
            "GET /policies",
            (SEARCH, LIMIT, OFFSET),
            policy_list,
            "list",
            ("id", "name", "version", "serve", "admit", "editable"),
            effect="read",
        ),
        Verb(
            "policy",
            "read",
            "A policy with every version, newest first, and its two axes as the options "
            "the Runners page shows (or Custom, with the sources).",
            "GET /policies/:id",
            (POLICY_ID,),
            policy_read,
            "read",
            effect="read",
        ),
        Verb(
            "policy",
            "create",
            "Make a policy, at version 1, from the machine's default (a person's: serve "
            "me only, admit mine; an org's: serve the org, admit mine and my org's "
            "approved) or body, changed by the other arguments; it reaches no runner "
            "until one is put under it.",
            "POST /policies",
            (
                Arg("name", "Its name, 1 to 80 characters.", required=True, positional=True),
                *SHAPE,
                Arg("org_id", "An org's policy (you are its admin), for the org's machines."),
            ),
            policy_create,
            effect="draft",
        ),
        Verb(
            "policy",
            "update",
            "Give it a new version, the current one (or body) changed by the other "
            "arguments: names the runners it reaches, and changes nothing unless yes.",
            "PUT /policies/:id",
            (POLICY_ID, *SHAPE, Arg("name", "Its new name."), REACH),
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
