"""The object noun: a stored object, by its path."""

from __future__ import annotations

import json
import pathlib
from typing import Any

from ..endings import with_notice
from .core import (
    ACK,
    FULL,
    LIMIT,
    OFFSET,
    SEARCH,
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

# --- object -------------------------------------------------------------------

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
    return ctx.get(
        "/objects/~items",
        path=a["path"],
        offset=a.get("offset"),
        limit=a.get("limit"),
        sort=a.get("sort"),
        order=a.get("order"),
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
        ),
        Verb(
            "object",
            "read",
            "Its header (kind, size, hash, item count); with full, its payload.",
            "GET /objects/~meta",
            (PATH, FULL),
            object_read,
            "read",
        ),
        Verb(
            "object",
            "items",
            "A page of a collection's items.",
            "GET /objects/~items",
            (
                PATH,
                OFFSET,
                LIMIT,
                Arg("sort", "A dot path to sort by."),
                Arg("order", "asc or desc.", choices=("asc", "desc")),
            ),
            object_items,
            "read",
        ),
        Verb(
            "object",
            "write",
            "Store a payload (JSON) at a path, with its lineage inputs.",
            "PUT /objects/:path",
            (
                PATH,
                Arg("file", "A JSON file holding the payload.", positional=True),
                Arg("payload", "The payload itself (MCP).", type="json"),
                Arg(
                    "inputs",
                    "A path it was computed from.",
                    type="strs",
                    flag="--input",
                ),
            ),
            object_write,
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
        ),
        Verb(
            "object",
            "delete",
            "Delete it (everything under it with prefix): a dry run unless yes.",
            "DELETE /objects/:path",
            (PATH, Arg("prefix", "Everything under the path.", type="bool"), YES, ACK),
            lambda ctx, a: delete(ctx, a["path"], a, prefix=bool(a.get("prefix"))),
        ),
        Verb(
            "object",
            "history",
            "Its audit log, by path or id; readable after it is deleted.",
            "GET /history/object/~at",
            (PATH,),
            object_history,
            "read",
        ),
    ),
    absent={"create": "write is its create: a path is written, not minted"},
)
