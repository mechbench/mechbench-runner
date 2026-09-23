"""The article noun."""

from __future__ import annotations

import json
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
    text_of,
    unwrap,
    view,
)


def article_body(a: dict) -> str | None:
    text = text_of(a, "body_file")
    if text is None:
        return None
    try:
        doc = json.loads(text)
    except ValueError:
        doc = None
    if not isinstance(doc, dict) or not isinstance(doc.get("ops"), list):
        raise VerbError(
            'body_file is rich text JSON ({"ops": [...]}); markdown writes '
            "are task 000525"
        )
    return json.dumps(doc)


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
    text = article_body(a)
    if text is not None:
        body["body"] = text
    if not body:
        raise VerbError("update needs something to change")
    return unwrap(ctx.api("PATCH", f"/articles/{a['id']}", body=body)[0], "article")


BODY = Arg("body_file", "A file of its body, as rich text JSON.")
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
            "Its summary; full has the body and the live version to edit from.",
            "GET /articles/:id",
            (ID, FULL),
            lambda ctx, a: ctx.get(f"/articles/{a['id']}", view=view(a)),
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
