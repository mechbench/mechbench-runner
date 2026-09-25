from __future__ import annotations

import json
import pathlib
from typing import Any

from ..endings import with_notice
from .core import (
    ACK,
    CONFIRMED,
    FULL,
    LIMIT,
    OFFSET,
    SEARCH,
    WIDER,
    YES,
    Arg,
    Ctx,
    Noun,
    Verb,
    VerbError,
    delete,
    history,
    listing,
    page,
)

PATH = Arg("path", "Its path: owner/project/…", required=True, positional=True)


def object_list(ctx: Ctx, a: dict) -> Any:
    return listing(
        ctx,
        "/objects",
        {"prefix": a.get("prefix"), "kind": a.get("kind"), **page(a)},
        unwrap="objects",
    )


def object_read(ctx: Ctx, a: dict) -> Any:
    if a.get("full"):
        return with_notice(ctx.bench().fetch(a["path"]))
    return with_notice(ctx.get("/objects/~meta", path=a["path"]))


def object_items(ctx: Ctx, a: dict) -> Any:
    fields = a.get("fields")
    if isinstance(fields, (list, tuple)):
        fields = ",".join(fields)
    where = a.get("where")
    if isinstance(where, str):
        where = [where]
    return ctx.get(
        "/objects/~items",
        path=a["path"],
        fields=fields,
        where=list(where) if where else None,
        sort=a.get("sort"),
        order=a.get("order"),
        offset=a.get("offset"),
        limit=a.get("limit"),
        lines=a.get("lines"),
        chars=a.get("chars"),
        count=1 if a.get("count") else None,
        header=1 if a.get("header") else None,
    )


def object_write(ctx: Ctx, a: dict) -> Any:
    payload = a.get("payload")
    if payload is None and a.get("file"):
        payload = json.loads(pathlib.Path(a["file"]).read_text())
    if payload is None:
        raise VerbError("write needs a payload (MCP) or a FILE of JSON")
    return ctx.bench().emit(a["path"], payload, inputs=list(a.get("inputs") or ()))


def object_update(ctx: Ctx, a: dict) -> Any:
    body = {"visibility": a["visibility"], "prefix": bool(a.get("prefix"))}
    return ctx.api("PATCH", f"/objects/{a['path']}", body=body)[0]


def object_history(ctx: Ctx, a: dict) -> Any:
    target = a["path"]
    if "/" in target:
        return ctx.get("/history/object/~at", path=target)
    return history(ctx, "object", target)


OBJECT = Noun(
    "object",
    "A stored object: a result, a corpus, a table.",
    (
        Verb(
            "object",
            "list",
            "Objects under a prefix, newest first.",
            "GET /objects",
            (
                Arg("prefix", "owner, owner/project, or deeper (default: yours)."),
                Arg("kind", "Only this kind."),
                SEARCH,
                LIMIT,
                OFFSET,
            ),
            object_list,
            "list",
            ("path", "kind", "sizeBytes", "createdAt"),
            effect="read",
        ),
        Verb(
            "object",
            "read",
            "Its header (kind, size, hash, item count); with full, its payload.",
            "GET /objects/~meta",
            (PATH, FULL),
            object_read,
            "read",
            effect="read",
        ),
        Verb(
            "object",
            "items",
            "A page of a collection's items, read on the server: fields (dot "
            "paths, comma-separated; items come back flat) of those passing "
            "every where (PATH OP VALUE, OP = != < <= > >= ~); lines/chars cut "
            "strings; count or header alone.",
            "GET /objects/~items",
            (
                PATH,
                Arg(
                    "fields",
                    "Dot paths to answer, comma-separated (id,coords.prompt,text); "
                    "each item comes back flat, keyed by them.",
                ),
                Arg(
                    "where",
                    "PATH OP VALUE, OP one of = != < <= > >= ~ (contains); "
                    "repeat for AND (coords.prompt=flash).",
                    type="strs",
                ),
                Arg("sort", "A dot path to sort by."),
                Arg("order", "asc or desc.", choices=("asc", "desc")),
                OFFSET,
                LIMIT,
                Arg("lines", "Cut strings to their first N lines.", type="int"),
                Arg("chars", "Cut every string to N characters.", type="int"),
                Arg("count", "Only how many there are and how many pass.", type="bool"),
                Arg("header", "Only the object without its items.", type="bool"),
            ),
            object_items,
            "items",
            effect="read",
        ),
        Verb(
            "object",
            "write",
            "Store a payload (JSON) at a path, with its lineage inputs.",
            "PUT /objects/:path",
            (
                PATH,
                Arg(
                    "file",
                    "A JSON file holding the payload.",
                    positional=True,
                    local=True,
                ),
                Arg("payload", "The payload itself (MCP).", type="json"),
                Arg(
                    "inputs",
                    "A path it was computed from.",
                    type="strs",
                    flag="--input",
                ),
            ),
            object_write,
            effect="draft",
        ),
        Verb(
            "object",
            "update",
            "Change who can read it (everything under it with prefix).",
            "PATCH /objects/:path",
            (
                PATH,
                Arg(
                    "visibility",
                    "Who can read it.",
                    required=True,
                    choices=("private", "org", "public"),
                ),
                Arg("prefix", "Everything under the path.", type="bool"),
            ),
            object_update,
            effect="outward",
            consent_when=WIDER,
        ),
        Verb(
            "object",
            "delete",
            "Delete it (everything under it with prefix): a dry run unless yes.",
            "DELETE /objects/:path",
            (PATH, Arg("prefix", "Everything under the path.", type="bool"), YES, ACK),
            lambda ctx, a: delete(ctx, a["path"], a, prefix=bool(a.get("prefix"))),
            effect="delete",
            consent_when=CONFIRMED,
        ),
        Verb(
            "object",
            "history",
            "Its audit log, by path or id; readable after it is deleted.",
            "GET /history/object/~at",
            (PATH,),
            object_history,
            "read",
            effect="read",
        ),
    ),
    absent={"create": "write is its create: a path is written, not minted"},
)
