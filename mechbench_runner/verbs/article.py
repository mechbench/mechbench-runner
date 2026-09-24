from __future__ import annotations

import json
from typing import Any

from .core import (
    BASE,
    EDITED,
    FORMAT,
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
    edit,
    given,
    history,
    listing,
    owner_of,
    page,
    text_of,
    unwrap,
    view,
)


def delta_text(text: str) -> str | None:
    try:
        doc = json.loads(text)
    except ValueError:
        return None
    if isinstance(doc, dict) and isinstance(doc.get("ops"), list):
        return json.dumps(doc)
    return None


def article_body(a: dict) -> str | None:
    text = text_of(a, "body_file")
    if text is None:
        return None
    doc = delta_text(text)
    if doc is None:
        raise VerbError(
            "create takes body_file as rich text JSON; create it, then "
            "`article edit --body-file` with markdown"
        )
    return doc


def article_create(ctx: Ctx, a: dict) -> Any:
    kind, handle = owner_of(a, ctx)
    body = {
        "ownerKind": kind,
        "ownerHandle": handle,
        "slug": a["slug"],
        "title": a["title"],
        **given(a, "subtitle", "visibility", "tags"),
    }
    text = article_body(a)
    if text is not None:
        body["body"] = text
    return unwrap(ctx.api("POST", "/articles", body=body)[0], "article")


def article_update(ctx: Ctx, a: dict) -> Any:
    body = given(
        a, "title", "subtitle", "slug", "status", "visibility", "tags", "base_version"
    )
    text = text_of(a, "body_file")
    if text is not None and delta_text(text) is None:
        if a.get("base_version") is None:
            raise VerbError(
                "a markdown body is an edit at the version you read: pass "
                "base_version (the collabEdit of `article read --format markdown`), "
                "or use `article edit`"
            )
        content = {k: body.pop(k) for k in ("title", "subtitle", "tags") if k in body}
        base = body.pop("baseVersion")
        out = ctx.api(
            "PUT",
            f"/articles/{a['id']}",
            query={"format": "markdown"},
            body={**content, "body": text, "baseVersion": base},
        )[0]
        if not body:
            return out
        return unwrap(ctx.api("PATCH", f"/articles/{a['id']}", body=body)[0], "article")
    if text is not None:
        body["body"] = delta_text(text)
    if not body:
        raise VerbError("update needs something to change")
    return unwrap(ctx.api("PATCH", f"/articles/{a['id']}", body=body)[0], "article")


def article_read(ctx: Ctx, a: dict) -> Any:
    if a.get("format"):
        return ctx.get(f"/articles/{a['id']}", format=a["format"])
    return ctx.get(f"/articles/{a['id']}", view=view(a))


def article_edit(ctx: Ctx, a: dict) -> Any:
    return edit(ctx, "article", f"/articles/{a['id']}", a, "body_file", "body")


def article_restore(ctx: Ctx, a: dict) -> Any:
    route = f"/articles/{a['id']}/versions/{int(a['version'])}/restore"
    return unwrap(ctx.api("POST", route)[0], "article")


BODY = Arg("body_file", "A file of its body: rich text JSON, or markdown.")
TAGS = Arg("tags", "A tag.", type="strs", flag="--tag")

ARTICLE = Noun(
    "article",
    "An article: prose with embedded results and protocols.",
    (
        Verb(
            "article",
            "list",
            "Articles newest first, by owner, status or title.",
            "GET /articles",
            (
                Arg("owner", "A user or org handle (default: every one you can read)."),
                Arg(
                    "status",
                    "draft, published or archived.",
                    choices=("draft", "published", "archived"),
                ),
                Arg("mine", "Only those you wrote.", type="bool"),
                SEARCH,
                LIMIT,
                OFFSET,
                FULL,
            ),
            lambda ctx, a: listing(
                ctx,
                "/articles",
                {
                    "owner": a.get("owner"),
                    "status": a.get("status"),
                    "mine": a.get("mine"),
                    **page(a),
                    "view": view(a),
                },
            ),
            "list",
            ("id", "ownerHandle", "slug", "status", "title"),
        ),
        Verb(
            "article",
            "read",
            "Its summary; full has the body; a format gives the body as "
            "markdown or a delta, with the version to edit from.",
            "GET /articles/:id",
            (ID, FULL, FORMAT),
            article_read,
            "read",
        ),
        Verb(
            "article",
            "create",
            "A new draft.",
            "POST /articles",
            (
                Arg("slug", "Its address.", required=True),
                Arg("title", "Its title.", required=True),
                OWNER,
                ORG,
                Arg("subtitle", "Its subtitle."),
                BODY,
                VISIBILITY,
                TAGS,
            ),
            article_create,
        ),
        Verb(
            "article",
            "update",
            "Change its title, body, status, visibility, slug or tags.",
            "PATCH /articles/:id",
            (
                ID,
                Arg("title", "Its title."),
                Arg("subtitle", "Its subtitle."),
                Arg("slug", "Its address."),
                Arg(
                    "status",
                    "draft, published or archived.",
                    choices=("draft", "published", "archived"),
                ),
                VISIBILITY,
                TAGS,
                BODY,
                Arg(
                    "base_version", "The live version the body was read at.", type="int"
                ),
            ),
            article_update,
        ),
        Verb(
            "article",
            "edit",
            "Write back an article read with a format, edited, at the version "
            "it was read; edits made since are kept.",
            "PUT /articles/:id",
            (
                ID,
                EDITED,
                Arg("body_file", "Its body from a file, in the edit's format."),
                Arg("title", "Its title."),
                Arg("subtitle", "Its subtitle."),
                TAGS,
                BASE,
                FORMAT,
            ),
            article_edit,
        ),
        Verb(
            "article",
            "versions",
            "Its sealed versions, newest first.",
            "GET /articles/:id/versions",
            (ID,),
            lambda ctx, a: ctx.get(f"/articles/{a['id']}/versions"),
            "read",
        ),
        Verb(
            "article",
            "restore",
            "Make an earlier version its content again, as the next version.",
            "POST /articles/:id/versions/:n/restore",
            (ID, Arg("version", "The version to restore.", type="int", required=True)),
            article_restore,
        ),
        Verb(
            "article",
            "delete",
            "Delete it, its revisions and comments: a dry run unless yes.",
            "DELETE /articles/:id",
            (ID, YES),
            lambda ctx, a: delete(ctx, a["id"], a),
        ),
        Verb(
            "article",
            "history",
            "Its audit log; readable after it is deleted.",
            "GET /history/:kind/:id",
            (ID,),
            lambda ctx, a: history(ctx, "article", a["id"]),
            "read",
        ),
    ),
)
