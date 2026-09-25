from __future__ import annotations

from typing import Any
from urllib.parse import quote

from .core import (
    ACK,
    CONFIRMED,
    FULL,
    LIMIT,
    OFFSET,
    ORG,
    OWNER,
    SEARCH,
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
    owner_of,
    page,
    unwrap,
    view,
)


def project_read(ctx: Ctx, a: dict) -> Any:
    target = str(a["id"])
    if "/" in target:
        owner, _, slug = target.partition("/")
        return ctx.get(f"/projects/by/{quote(owner)}/{quote(slug)}", view=view(a))
    return ctx.get(f"/projects/{target}", view=view(a))


def project_id(ctx: Ctx, target: str) -> str:
    if "/" not in target:
        return target
    return str(unwrap(project_read(ctx, {"id": target}), "project")["id"])


def project_create(ctx: Ctx, a: dict) -> Any:
    kind, handle = owner_of(a, ctx)
    body = {
        "ownerKind": kind,
        "ownerHandle": handle,
        "slug": a["slug"],
        "displayName": a.get("name") or a["slug"],
        **given(a, "description"),
    }
    return ctx.api("POST", "/projects", body=body)[0]


def project_update(ctx: Ctx, a: dict) -> Any:
    body = given(a, "name", "description", "slug", rename={"name": "displayName"})
    if not body:
        raise VerbError("update needs a name, description or slug")
    return ctx.api("PATCH", f"/projects/{project_id(ctx, a['id'])}", body=body)[0]


PROJECT_ID = Arg("id", "Its id, or owner/slug.", required=True, positional=True)

PROJECT = Noun(
    "project",
    "A project: where protocols, runs and objects live.",
    (
        Verb(
            "project",
            "list",
            "An owner's projects, most recently worked in first.",
            "GET /projects",
            (OWNER, SEARCH, LIMIT, OFFSET, FULL),
            lambda ctx, a: listing(
                ctx, "/projects", {"owner": a.get("owner"), **page(a), "view": view(a)}
            ),
            "list",
            ("id", "ownerHandle", "slug", "displayName", "lastActivityAt"),
            effect="read",
        ),
        Verb(
            "project",
            "read",
            "Its summary, by id or owner/slug.",
            "GET /projects/:id",
            (PROJECT_ID, FULL),
            project_read,
            "read",
            effect="read",
        ),
        Verb(
            "project",
            "create",
            "A new project.",
            "POST /projects",
            (
                Arg("slug", "Its address.", required=True),
                OWNER,
                ORG,
                Arg("name", "Its display name (default: the slug)."),
                Arg("description", "What it is for."),
            ),
            project_create,
            effect="draft",
        ),
        Verb(
            "project",
            "update",
            "Rename it, redescribe it, or change its slug.",
            "PATCH /projects/:id",
            (
                PROJECT_ID,
                Arg("name", "Its display name."),
                Arg("description", "What it is for."),
                Arg("slug", "Its address."),
            ),
            project_update,
            effect="draft",
        ),
        Verb(
            "project",
            "delete",
            "Delete it and everything in it: a dry run unless yes.",
            "DELETE /projects/:id",
            (PROJECT_ID, YES, ACK),
            lambda ctx, a: delete(ctx, project_id(ctx, a["id"]), a),
            effect="delete",
            consent_when=CONFIRMED,
        ),
        Verb(
            "project",
            "history",
            "Its audit log; readable after it is deleted.",
            "GET /history/:kind/:id",
            (PROJECT_ID,),
            lambda ctx, a: history(ctx, "project", project_id(ctx, a["id"])),
            "read",
            effect="read",
        ),
    ),
)
