"""The dataset noun: a named, described object."""

from __future__ import annotations

from typing import Any

from .core import (
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
    owner_of,
    page,
    unwrap,
    view,
)


def dataset_create(ctx: Ctx, a: dict) -> Any:
    kind, handle = owner_of(a, ctx)
    body = {
        "ownerKind": kind,
        "ownerHandle": handle,
        "slug": a["slug"],
        "title": a["title"],
        "objectPath": a["object"],
        **given(a, "description", "visibility"),
    }
    return unwrap(ctx.api("POST", "/datasets/register", body=body)[0], "dataset")


def dataset_update(ctx: Ctx, a: dict) -> Any:
    body = given(a, "title", "description", "visibility", "slug")
    if not body:
        raise VerbError("update needs something to change")
    return unwrap(ctx.api("PATCH", f"/datasets/{a['id']}", body=body)[0], "dataset")


DATASET = Noun(
    "dataset",
    "A dataset: a named, described object others can find.",
    (
        Verb(
            "dataset",
            "list",
            "Datasets newest first, by owner or title.",
            "GET /datasets",
            (
                Arg("owner", "A user or org handle (default: every one you can read)."),
                SEARCH,
                LIMIT,
                OFFSET,
                FULL,
            ),
            lambda ctx, a: listing(
                ctx, "/datasets", {"owner": a.get("owner"), **page(a), "view": view(a)}
            ),
            "list",
            ("id", "ownerHandle", "slug", "dataPath", "title"),
        ),
        Verb(
            "dataset",
            "read",
            "Its metadata and the object it names.",
            "GET /datasets/:id",
            (ID, FULL),
            lambda ctx, a: unwrap(
                ctx.get(f"/datasets/{a['id']}", view=view(a)), "dataset"
            ),
            "read",
        ),
        Verb(
            "dataset",
            "create",
            "Name a stored object as a dataset.",
            "POST /datasets/register",
            (
                Arg("object", "The object's path.", required=True),
                Arg("slug", "Its address.", required=True),
                Arg("title", "Its title.", required=True),
                OWNER,
                ORG,
                Arg("description", "What it is."),
                VISIBILITY,
            ),
            dataset_create,
        ),
        Verb(
            "dataset",
            "update",
            "Change its title, description, visibility or slug.",
            "PATCH /datasets/:id",
            (
                ID,
                Arg("title", "Its title."),
                Arg("description", "What it is."),
                VISIBILITY,
                Arg("slug", "Its address."),
            ),
            dataset_update,
        ),
        Verb(
            "dataset",
            "delete",
            "Delete it (a registered one leaves its object): a dry run unless yes.",
            "DELETE /datasets/:id",
            (ID, YES),
            lambda ctx, a: delete(ctx, a["id"], a),
        ),
        Verb(
            "dataset",
            "history",
            "Its audit log; readable after it is deleted.",
            "GET /history/:kind/:id",
            (ID,),
            lambda ctx, a: history(ctx, "dataset", a["id"]),
            "read",
        ),
    ),
)
