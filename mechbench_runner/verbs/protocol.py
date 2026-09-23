"""The protocol noun: a graph with its params, inputs and outputs, and its versions."""

from __future__ import annotations

from typing import Any

from .core import (
    ACK,
    FULL,
    ID,
    LIMIT,
    OFFSET,
    ORG,
    OWNER,
    SEARCH,
    VISIBILITY,
    YES,
    Arg,
    Ctx,
    Noun,
    Verb,
    VerbError,
    delete,
    given,
    history,
    listing,
    page,
    unwrap,
    view,
)

# --- protocol -----------------------------------------------------------------


def protocol_read(ctx: Ctx, a: dict) -> Any:
    if a.get("version") is not None:
        return ctx.get(
            f"/protocols/{a['id']}/versions/{int(a['version'])}", view=view(a)
        )
    return unwrap(ctx.get(f"/protocols/{a['id']}", view=view(a)), "protocol")


def protocol_update(ctx: Ctx, a: dict) -> Any:
    body = given(
        a,
        "name",
        "description",
        "visibility",
        "project",
        rename={"project": "projectSlug"},
    )
    if not body:
        raise VerbError(
            "update needs name, description, visibility or project; "
            "a graph changes by push"
        )
    out = ctx.api("PATCH", f"/protocols/{a['id']}", body=body)[0]
    return unwrap(out, "protocol")


def protocol_push(ctx: Ctx, a: dict) -> Any:
    bench = ctx.bench()
    try:
        return bench.push_protocol(
            a["file"], a["into"], owner_kind="org" if a.get("org") else "user"
        )
    except bench.BenchError as e:
        if e.status is None or not isinstance(e.body, dict):
            raise
        return {"action": "refused", **e.body}


def protocol_publish(ctx: Ctx, a: dict) -> Any:
    bench = ctx.bench()
    n = a.get("version")
    if n is None:
        n = int(bench.get_protocol(a["id"])["version"])
    return bench.publish_protocol_version(a["id"], int(n))


def split_version(a: dict) -> tuple[str, int]:
    pid, sep, ver = str(a["id"]).partition("@")
    n = a.get("version") if a.get("version") is not None else (ver if sep else None)
    if n is None or not str(n).isdigit():
        raise VerbError("copy needs a version: --version N, or ID@N")
    return pid, int(n)


def protocol_copy(ctx: Ctx, a: dict) -> Any:
    pid, n = split_version(a)
    owner, _, project = str(a["into"]).partition("/")
    if not owner or not project:
        raise VerbError("into is owner/project")
    return ctx.bench().copy_protocol_version(
        pid,
        n,
        owner,
        project,
        name=a.get("name"),
        owner_kind="org" if a.get("org") else "user",
        dry_run=bool(a.get("dry_run")),
    )


def protocol_restore(ctx: Ctx, a: dict) -> Any:
    route = f"/protocols/{a['id']}/versions/{int(a['version'])}/restore"
    return unwrap(ctx.api("POST", route)[0], "protocol")


VERSION = Arg("version", "A sealed version (default: the head).", type="int")
#: The version a restore makes the head again.
RESTORED = Arg("version", "The version to restore.", type="int", required=True)
INTO = Arg("into", "owner/project.", required=True)

PROTOCOL = Noun(
    "protocol",
    "A protocol: a graph with its params, inputs and outputs.",
    (
        Verb(
            "protocol",
            "list",
            "Protocols newest first, by owner, project or name.",
            "GET /protocols",
            (OWNER, Arg("project", "owner/project."), SEARCH, LIMIT, OFFSET, FULL),
            lambda ctx, a: listing(
                ctx,
                "/protocols",
                {
                    "owner": a.get("owner"),
                    "project": a.get("project"),
                    **page(a),
                    "view": view(a),
                },
            ),
            "list",
            ("id", "name", "version", "projectSlug", "updatedAt"),
        ),
        Verb(
            "protocol",
            "read",
            "Its summary (signature by name, node count); full has the graph.",
            "GET /protocols/:id",
            (ID, VERSION, FULL),
            protocol_read,
            "read",
        ),
        Verb(
            "protocol",
            "versions",
            "Its sealed versions, newest first.",
            "GET /protocols/:id/versions",
            (ID, LIMIT, OFFSET),
            lambda ctx, a: ctx.get(
                f"/protocols/{a['id']}/versions",
                limit=a.get("limit"),
                offset=a.get("offset"),
            ),
            "read",
        ),
        Verb(
            "protocol",
            "push",
            "Create or version it from a file, by its name in the project.",
            "POST /protocols/push",
            (
                Arg(
                    "file", "The protocol file (JSON).", required=True, positional=True
                ),
                INTO,
                ORG,
            ),
            protocol_push,
        ),
        Verb(
            "protocol",
            "export",
            "A version (the head by default) as its canonical file.",
            "GET /protocols/:id/export",
            (ID, VERSION, Arg("path", "Write the file here.", flag="-o")),
            lambda ctx, a: ctx.bench().export_protocol(
                a["id"], version=a.get("version"), path=a.get("path")
            ),
        ),
        Verb(
            "protocol",
            "update",
            "Rename it, redescribe it, move it, or change who can read it.",
            "PATCH /protocols/:id",
            (
                ID,
                Arg("name", "Its new name."),
                Arg("description", "Its new description (plain text)."),
                VISIBILITY,
                Arg("project", "Move it to this project (a slug)."),
            ),
            protocol_update,
        ),
        Verb(
            "protocol",
            "publish",
            "Make a version readable by anyone (the head by default).",
            "POST /protocols/:id/versions/:n/publish",
            (ID, VERSION),
            protocol_publish,
        ),
        Verb(
            "protocol",
            "unpublish",
            "Withdraw a published version.",
            "POST /protocols/:id/versions/:n/unpublish",
            (ID, Arg("version", "The version.", type="int", required=True)),
            lambda ctx, a: ctx.bench().unpublish_protocol_version(
                a["id"], int(a["version"])
            ),
        ),
        Verb(
            "protocol",
            "restore",
            "Make an earlier version the head again, as the next version.",
            "POST /protocols/:id/versions/:n/restore",
            (ID, RESTORED),
            protocol_restore,
        ),
        Verb(
            "protocol",
            "copy",
            "Copy a version into a project, sub-protocols and all.",
            "POST /protocols/:id/versions/:n/copy",
            (
                Arg("id", "Its id, or ID@VERSION.", required=True, positional=True),
                VERSION,
                INTO,
                Arg("name", "The copy's name."),
                ORG,
                Arg("dry_run", "Say what it would create.", type="bool"),
            ),
            protocol_copy,
        ),
        Verb(
            "protocol",
            "delete",
            "Delete it, every version and run: a dry run unless yes.",
            "DELETE /protocols/:id",
            (ID, YES, ACK),
            lambda ctx, a: delete(ctx, a["id"], a),
        ),
        Verb(
            "protocol",
            "history",
            "Its audit log; readable after it is deleted.",
            "GET /history/:kind/:id",
            (ID,),
            lambda ctx, a: history(ctx, "protocol", a["id"]),
            "read",
        ),
    ),
    absent={"create": "push is its create: a file is pushed, by its name"},
)
