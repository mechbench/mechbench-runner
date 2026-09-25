from __future__ import annotations

from typing import Any

from .core import (
    CONFIRMED,
    FULL,
    ID,
    LIMIT,
    OFFSET,
    SEARCH,
    YES,
    Arg,
    Ctx,
    Noun,
    Verb,
    VerbError,
    given,
    history,
    listing,
    page,
    unwrap,
    view,
)
from .project import project_id

SHARED = {"visibility": ("shared",)}
THREAD_VISIBILITY = Arg(
    "visibility",
    "private, or shared: read-only for anyone who reads its project.",
    choices=("private", "shared"),
)


def thread_list(ctx: Ctx, a: dict) -> Any:
    project = a.get("project")
    return listing(
        ctx,
        "/threads",
        {"projectId": project_id(ctx, project) if project else None, **page(a)},
        unwrap="threads",
    )


def thread_read(ctx: Ctx, a: dict) -> Any:
    out = ctx.get(f"/threads/{a['id']}", view=view(a))
    return out if a.get("full") else unwrap(out, "thread")


def thread_create(ctx: Ctx, a: dict) -> Any:
    body = {
        "projectId": project_id(ctx, a["project"]),
        **given(a, "title", "visibility", "model", "key"),
    }
    return unwrap(ctx.api("POST", "/threads", body=body)[0], "thread")


def thread_update(ctx: Ctx, a: dict) -> Any:
    body = given(a, "title", "visibility", "model", "key")
    if not body:
        raise VerbError("update needs a title, visibility, model or key")
    return unwrap(ctx.api("PATCH", f"/threads/{a['id']}", body=body)[0], "thread")


def thread_fork(ctx: Ctx, a: dict) -> Any:
    body: dict[str, Any] = {"message": a["message"], **given(a, "title")}
    if a.get("project"):
        body["projectId"] = project_id(ctx, a["project"])
    return unwrap(ctx.api("POST", f"/threads/{a['id']}/fork", body=body)[0], "thread")


def thread_delete(ctx: Ctx, a: dict) -> Any:
    thread = unwrap(ctx.get(f"/threads/{a['id']}", view="summary"), "thread")
    if not a.get("yes"):
        return {"deleted": False, "dryRun": True, "deletes": {"threads": [thread]}}
    ctx.api("DELETE", f"/threads/{a['id']}")
    return {"deleted": True, "dryRun": False, "deletes": {"threads": [thread]}}


PROJECT = Arg("project", "The project: its id, or owner/slug.")
MODEL = Arg(
    "model",
    'The model each turn uses: {"provider": …, "model": …}.',
    type="json",
)
KEY = Arg("key", "The credential that pays for its tokens (an integration id).")

THREAD = Noun(
    "thread",
    "A thread: a conversation with the platform's agent, private to its author.",
    (
        Verb(
            "thread",
            "list",
            "Your threads and the shared ones you read, most recently changed first.",
            "GET /threads",
            (PROJECT, SEARCH, LIMIT, OFFSET),
            thread_list,
            "list",
            ("id", "title", "authorHandle", "visibility", "updatedAt"),
            effect="read",
        ),
        Verb(
            "thread",
            "read",
            "Its summary; full has the transcript and its forks.",
            "GET /threads/:id",
            (ID, FULL),
            thread_read,
            "read",
            effect="read",
        ),
        Verb(
            "thread",
            "create",
            "A new private thread in a project you write in.",
            "POST /threads",
            (
                Arg("project", "The project: its id, or owner/slug.", required=True),
                Arg("title", "Its title."),
                THREAD_VISIBILITY,
                MODEL,
                KEY,
            ),
            thread_create,
            effect="outward",
            consent_when=SHARED,
        ),
        Verb(
            "thread",
            "update",
            "Retitle it, share it or make it private, or change its model or key.",
            "PATCH /threads/:id",
            (ID, Arg("title", "Its title."), THREAD_VISIBILITY, MODEL, KEY),
            thread_update,
            effect="outward",
            consent_when=SHARED,
        ),
        Verb(
            "thread",
            "fork",
            "Continue a thread you read as a new private one of yours, "
            "its transcript up to a message.",
            "POST /threads/:id/fork",
            (
                ID,
                Arg("message", "The last message to copy.", required=True),
                Arg("project", "Where the fork lives (default: the thread's)."),
                Arg("title", "The fork's title."),
            ),
            thread_fork,
            effect="draft",
        ),
        Verb(
            "thread",
            "delete",
            "Delete it and its transcript: a dry run unless yes.",
            "DELETE /threads/:id",
            (ID, YES),
            thread_delete,
            effect="delete",
            consent_when=CONFIRMED,
        ),
        Verb(
            "thread",
            "history",
            "Its audit log, for its author; readable after it is deleted.",
            "GET /history/:kind/:id",
            (ID,),
            lambda ctx, a: history(ctx, "thread", a["id"]),
            "read",
            effect="read",
        ),
    ),
)
