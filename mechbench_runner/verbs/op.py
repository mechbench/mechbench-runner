from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from urllib.parse import quote

from .core import LIMIT, OFFSET, SEARCH, Arg, Ctx, Noun, Verb, VerbError, paged, unwrap

COLUMNS = ("address", "summary", "source")
KIND_ARG = Arg(
    "kind",
    "A kind (`direction/vector`, or an extension's `<owner>/<project>/kinds/<family>/<leaf>`), "
    "or an object's path, read as its kind.",
    required=True,
    positional=True,
)


def op_route(address: str) -> str:
    return "/ops/" + quote(address.strip("/"), safe="/@:")


def brief(entry: Mapping[str, Any]) -> dict[str, Any]:
    src = entry.get("source") or {}
    out = dict(entry)
    out["source"] = (
        "core" if src.get("kind") == "core" else f"{src.get('address')}@{src.get('version')}"
    )
    return out


def index(ctx: Ctx, query: Mapping[str, Any]) -> list[dict[str, Any]]:
    data = ctx.get("/ops", **{k: v for k, v in query.items() if v is not None})
    return [brief(e) for e in (unwrap(data, "ops") or []) if isinstance(e, Mapping)]


def op_list(ctx: Ctx, a: dict) -> Any:
    ops = index(
        ctx,
        {"reads": a.get("reads"), "emits": a.get("emits"), "owner": a.get("owner"), "q": a.get("search")},
    )
    return paged(ops, a)


def is_kind(text: str) -> bool:
    parts = text.strip("/").split("/")
    return len(parts) == 2 or (len(parts) == 5 and parts[2] == "kinds")


def kind_of(ctx: Ctx, target: str) -> str:
    if is_kind(target):
        return target
    meta = ctx.get("/objects/~meta", path=target)
    kind = meta.get("kind") if isinstance(meta, Mapping) else None
    if not kind:
        raise VerbError(f"{target} has no kind to follow")
    return str(kind)


def ancestry(kind: str) -> list[str]:
    try:
        from mechbench_compute.lexicon import kinds

        return list(kinds.ancestry(kind)) or [kind]
    except Exception:  # noqa: BLE001
        return [kind]


def ports_taking(entry: Mapping[str, Any], kind: str) -> list[str]:
    lineage = set(ancestry(kind)) | {"collection"}
    ports = entry.get("inputs") or []
    hit = [p["name"] for p in ports if lineage & set(p.get("kinds") or [])]
    return hit or [p["name"] for p in ports]


def op_next(ctx: Ctx, a: dict) -> Any:
    kind = kind_of(ctx, str(a["kind"]))
    ops = index(ctx, {"reads": kind, "owner": a.get("owner"), "q": a.get("search")})
    for e in ops:
        e["ports"] = ports_taking(e, kind)
    out = paged(ops, a)
    out["kind"] = kind
    return out


OP = Noun(
    "op",
    "An operation: core's and the extensions' you would use, in one index, "
    "by the kinds they read and emit.",
    (
        Verb(
            "op",
            "list",
            "Operations, core's in the lexicon's order then extensions' by address: "
            "those that read a kind, emit one, are an owner's, or match a search.",
            "GET /ops",
            (
                Arg("reads", "Only ops with a port that takes this kind or an ancestor of it."),
                Arg("emits", "Only ops whose output is this kind or one extending it."),
                Arg("owner", "Only this handle's extensions' ops; core's are left out."),
                SEARCH,
                LIMIT,
                OFFSET,
            ),
            op_list,
            "list",
            COLUMNS,
            effect="read",
        ),
        Verb(
            "op",
            "read",
            "One operation: its params, ports, output, needs, example and where it "
            "comes from; an extension's with its declaration and the pin a protocol "
            "naming it records.",
            "GET /ops/:address",
            (
                Arg(
                    "address",
                    "`family/leaf`, or `<owner>/<project>/ops/<family>/<leaf>` "
                    "(`@<n>` or `@sha256:<hex>` for a version).",
                    required=True,
                    positional=True,
                ),
            ),
            lambda ctx, a: ctx.get(op_route(str(a["address"]))),
            "read",
            effect="read",
        ),
        Verb(
            "op",
            "next",
            "What can follow a kind: the ops whose ports take it or an ancestor, "
            "each with the ports that do, in the order the lexicon declares. Given "
            "an object's path, its kind's.",
            "GET /ops",
            (KIND_ARG, Arg("owner", "Only this handle's extensions' ops."), SEARCH, LIMIT, OFFSET),
            op_next,
            "list",
            ("address", "ports", "summary"),
            effect="read",
        ),
    ),
    absent={
        "create": "an op is declared in an extension's package; `extension push` publishes it",
        "update": "an op changes with its extension's next version; `extension push`",
        "delete": "an extension's version is withdrawn (`extension withdraw`), and core's "
                  "ops leave with a compute release",
        "history": "an extension's op changes with its versions (`extension history`); "
                   "core's with compute's releases",
    },
)
