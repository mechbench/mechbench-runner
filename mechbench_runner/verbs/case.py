from __future__ import annotations

import sys
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote

from .core import ID, LIMIT, OFFSET, SEARCH, Arg, Ctx, Noun, Verb, VerbError, listing, unwrap

KINDS = ("support", "submission")
STATUSES = ("open", "waiting", "answered", "closed", "all")
OUTCOMES = ("resolved", "closed", "withdrawn", "verified", "rejected")
QUIET_EVENTS = ("received", "sent", "message", "note")


def text(value: str) -> str:
    return sys.stdin.read() if value == "-" else value


def age(at: Any, now: datetime | None = None) -> str:
    if not at:
        return ""
    try:
        then = datetime.fromisoformat(str(at).replace("Z", "+00:00"))
    except ValueError:
        return ""
    s = max(0, int(((now or datetime.now(UTC)) - then).total_seconds()))
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if s >= size:
            return f"{s // size}{unit}"
    return f"{s}s"


def requester(r: Any) -> str:
    if not isinstance(r, dict):
        return str(r or "")
    return str(r.get("handle") or r.get("address") or "")


def row(c: dict[str, Any], now: datetime | None = None) -> dict[str, Any]:
    return {
        **c,
        "requester": requester(c.get("requester")),
        "assignee": c.get("assigneeUserId") or "",
        "age": age(c.get("lastInboundAt") or c.get("openedAt"), now),
    }


def user_id_of(ctx: Ctx, who: str) -> str:
    said = str(who).strip().lstrip("@")
    if said == "me":
        return "me"
    got = ctx.get("/admin/users", search=said)
    rows = unwrap(got, "users")
    for r in rows if isinstance(rows, list) else []:
        if isinstance(r, dict) and str(r.get("handle") or "").lower() == said.lower() and r.get("id"):
            return str(r["id"])
    raise VerbError(f"no user @{said}")


def case_list(ctx: Ctx, a: dict) -> Any:
    query = {k: a.get(k) for k in ("kind", "status", "search", "limit", "offset")}
    if a.get("assignee"):
        query["assignee"] = user_id_of(ctx, a["assignee"])
    out = listing(ctx, "/cases", query, unwrap="cases")
    now = datetime.now(UTC)
    return {**out, "items": [row(c, now) for c in out["items"] if isinstance(c, dict)]}


def route(a: dict, tail: str = "") -> str:
    return f"/cases/{quote(str(a['id']))}{tail}"


def case_read(ctx: Ctx, a: dict) -> Any:
    return ctx.get(route(a))


def message(ctx: Ctx, a: dict, visibility: str) -> Any:
    body = {"body": text(a["body"]), "visibility": visibility}
    return ctx.api("POST", route(a, "/messages"), body=body)[0]


def case_assign(ctx: Ctx, a: dict) -> Any:
    to = str(a.get("to") or "me").strip()
    user = None if to == "none" else user_id_of(ctx, to)
    return unwrap(ctx.api("POST", route(a, "/assign"), body={"userId": user})[0], "case")


def case_close(ctx: Ctx, a: dict) -> Any:
    body = {"outcome": a["outcome"]} if a.get("outcome") else {}
    return unwrap(ctx.api("POST", route(a, "/close"), body=body)[0], "case")


def who(actor: Any) -> str:
    if not isinstance(actor, dict):
        return ""
    return str(actor.get("handle") or actor.get("kind") or "")


def head_lines(c: dict[str, Any]) -> list[str]:
    status = str(c.get("status") or "")
    if c.get("outcome"):
        status += f" ({c['outcome']})"
    lines = [
        f"{c.get('id')}  {c.get('kind') or 'support'}  {status}  {c.get('subject')}",
        f"requester {requester(c.get('requester'))} · plan {c.get('plan')}"
        + (" · priority" if c.get("priority") else "")
        + (" · mail thread" if c.get("mailThread") else ""),
    ]
    about = c.get("about")
    if isinstance(about, dict) and about.get("address"):
        lines.append(f"about {about['address']}@{about.get('version')} ({about.get('hash')})")
    if c.get("assigneeUserId"):
        lines.append(f"assignee {c['assigneeUserId']}")
    return lines


def message_lines(m: dict[str, Any]) -> list[str]:
    at = m.get("createdAt")
    if m.get("visibility") == "internal":
        head = f"{at}  ## internal note by {who(m.get('author'))} via {m.get('via')}"
        indent = "   ## "
    else:
        arrow = "<-" if m.get("direction") == "in" else "->"
        head = f"{at}  {arrow} {who(m.get('author'))} via {m.get('via')}"
        indent = "   "
    delivery = m.get("delivery") or {}
    if delivery.get("sentAt"):
        head += f" (sent {delivery['sentAt']} by {who(delivery.get('sentBy'))})"
    body = [head, *(indent + x for x in str(m.get("body") or "").splitlines())]
    for f in m.get("attachments") or []:
        body.append(f"{indent}[attachment] {f.get('filename')} "
                    f"({f.get('contentType')}, {f.get('size')} bytes)"
                    + (f" {f['object']}" if f.get("object") else ""))
    return body


def history_lines(answer: dict[str, Any]) -> list[str]:
    lines = head_lines(answer.get("case") or {})
    entries: list[tuple[str, int, list[str]]] = []
    for m in answer.get("messages") or []:
        entries.append((str(m.get("createdAt") or ""), 0, message_lines(m)))
    for e in answer.get("events") or []:
        if e.get("kind") in QUIET_EVENTS:
            continue
        entries.append((str(e.get("at") or ""), 1,
                        [f"{e.get('at')}  ** {e.get('kind')} by {who(e.get('actor'))}"
                         + (f" {e['details']}" if e.get("details") else "")]))
    for _at, _k, body in sorted(entries, key=lambda x: (x[0], x[1])):
        lines += ["", *body]
    return lines


BODY = Arg("body", "The message, as text; - reads it from stdin.", required=True)

CASE = Noun(
    "case",
    "Cases: matters waiting on a person, a support question or an extension "
    "submission. One history per case, whichever way each message came (mail, "
    "the command line, MCP, the web): external messages are the conversation "
    "with the requester, internal notes are for the case's audience alone. A "
    "case is its requester's and its audience's (the platform's admins); to "
    "anyone else it does not exist.",
    (
        Verb(
            "case",
            "list",
            "Cases, those on priority plans first, then the newest message in: id, "
            "kind, status, subject, requester, assignee, age of the last inbound "
            "message.",
            "GET /cases",
            (
                Arg("kind", "Only cases of this kind.", choices=KINDS),
                Arg("status", "Only cases in this status.", choices=STATUSES),
                Arg("assignee", "Only cases this person is on: a handle, or me."),
                SEARCH,
                LIMIT,
                OFFSET,
            ),
            case_list,
            "list",
            ("id", "kind", "status", "subject", "requester", "assignee", "age"),
            effect="read",
        ),
        Verb(
            "case",
            "read",
            "The case and its whole history in order: every message with who wrote "
            "it, when, which way it came and whether it is an internal note, "
            "attachments, and what was done to it. A submission's case also carries "
            "the version it is about.",
            "GET /cases/:id",
            (ID,),
            case_read,
            "read",
            effect="read",
        ),
        Verb(
            "case",
            "reply",
            "Add an external message to a case: the requester sees it, and on a case "
            "with a mail thread it is sent as mail in that thread.",
            "POST /cases/:id/messages",
            (ID, BODY),
            lambda ctx, a: message(ctx, a, "external"),
            effect="outward",
        ),
        Verb(
            "case",
            "note",
            "Add an internal note to a case: for its audience alone, never sent and "
            "never shown to the requester.",
            "POST /cases/:id/messages",
            (ID, BODY),
            lambda ctx, a: message(ctx, a, "internal"),
            effect="draft",
        ),
        Verb(
            "case",
            "assign",
            "Put a person on a case: you, or the user named.",
            "POST /cases/:id/assign",
            (ID, Arg("to", "A handle, me (the default), or none to take everyone off it.")),
            case_assign,
            effect="draft",
        ),
        Verb(
            "case",
            "close",
            "Close a case with an outcome that fits its kind: resolved or closed "
            "(the default) for support; withdrawn for a submission, which extension "
            "verify and reject otherwise close.",
            "POST /cases/:id/close",
            (ID, Arg("outcome", "How it ended.", choices=OUTCOMES)),
            case_close,
            effect="draft",
        ),
    ),
    absent={
        "create": "a support case opens by mail or in the app; a submission by "
                  "extension submit",
        "update": "a case is its messages, which are never edited: reply and note "
                  "add one, assign and close set its fields",
        "delete": "a case is a record of what was said to someone, kept whole",
        "history": "read is its history: every message and event in order",
    },
)
