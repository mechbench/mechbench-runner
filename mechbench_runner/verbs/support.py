from __future__ import annotations

import sys
from datetime import UTC, datetime
from typing import Any

from .core import ID, LIMIT, OFFSET, SEARCH, Arg, Ctx, Noun, Verb, listing, unwrap

VIA = "cli"
STATUSES = ("open", "waiting", "answered", "closed")


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
        "age": age(c.get("lastInboundAt") or c.get("openedAt"), now),
    }


def support_list(ctx: Ctx, a: dict) -> Any:
    out = listing(
        ctx,
        "/support/cases",
        {k: a.get(k) for k in ("status", "search", "limit", "offset")},
        unwrap="cases",
    )
    want = str(a.get("search") or "").lower()
    cases = [c for c in out["items"] if isinstance(c, dict)]
    if want:
        cases = [c for c in cases
                 if want in f"{c.get('subject')} {requester(c.get('requester'))}".lower()]
    now = datetime.now(UTC)
    return {**out, "items": [row(c, now) for c in cases]}


def support_read(ctx: Ctx, a: dict) -> Any:
    return ctx.get(f"/support/cases/{a['id']}")


def support_open(ctx: Ctx, a: dict) -> Any:
    body = {"subject": a["subject"], "body": text(a["body"]), "via": VIA}
    return ctx.api("POST", "/support/cases", body=body)[0]


def support_reply(ctx: Ctx, a: dict) -> Any:
    body = {"body": text(a["body"]), "via": VIA}
    return ctx.api("POST", f"/support/cases/{a['id']}/messages", body=body)[0]


def support_close(ctx: Ctx, a: dict) -> Any:
    body = {"status": "closed", "via": VIA}
    return unwrap(ctx.api("PATCH", f"/support/cases/{a['id']}", body=body)[0], "case")


def who(actor: Any) -> str:
    if not isinstance(actor, dict):
        return ""
    return str(actor.get("handle") or actor.get("kind") or "")


def history_lines(answer: dict[str, Any]) -> list[str]:
    c = answer.get("case") or {}
    lines = [
        f"{c.get('id')}  {c.get('status')}  {c.get('subject')}",
        f"requester {requester(c.get('requester'))} · plan {c.get('plan')}"
        + (" · priority" if c.get("priority") else "")
        + (" · mail thread" if c.get("mailThread") else ""),
    ]
    entries: list[tuple[str, int, list[str]]] = []
    for m in answer.get("messages") or []:
        arrow = "<-" if m.get("direction") == "in" else "->"
        head = f"{m.get('createdAt')}  {arrow} {who(m.get('author'))} via {m.get('via')}"
        delivery = m.get("delivery") or {}
        if delivery.get("sentAt"):
            head += f" (sent {delivery['sentAt']} by {who(delivery.get('sentBy'))})"
        body = [head, *("   " + x for x in str(m.get("body") or "").splitlines())]
        for f in m.get("attachments") or []:
            body.append(f"   [attachment] {f.get('filename')} "
                        f"({f.get('contentType')}, {f.get('size')} bytes)"
                        + (f" {f['object']}" if f.get("object") else ""))
        entries.append((str(m.get("createdAt") or ""), 0, body))
    for e in answer.get("events") or []:
        if e.get("kind") in ("received", "sent"):
            continue
        entries.append((str(e.get("at") or ""), 1,
                        [f"{e.get('at')}  ** {e.get('kind')} by {who(e.get('actor'))}"
                         + (f" {e['details']}" if e.get("details") else "")]))
    for _at, _k, body in sorted(entries, key=lambda x: (x[0], x[1])):
        lines += ["", *body]
    return lines


BODY = Arg("body", "The message, as text; - reads it from stdin.", required=True)

SUPPORT = Noun(
    "support",
    "Support cases: one history per case, whichever way each message came (mail, "
    "the command line, MCP, the web). A platform admin sees every case, anyone "
    "else their own.",
    (
        Verb(
            "support",
            "list",
            "Cases, those on priority plans first, then the newest message in: id, "
            "status, subject, requester, age of the last inbound message, plan.",
            "GET /support/cases",
            (
                Arg("status", "Only cases in this status.", choices=STATUSES),
                SEARCH,
                LIMIT,
                OFFSET,
            ),
            support_list,
            "list",
            ("id", "status", "subject", "requester", "age", "plan"),
            effect="read",
        ),
        Verb(
            "support",
            "read",
            "The case and its whole history in order: every message with who wrote "
            "it, when and which way it came, attachments, and what was done to it.",
            "GET /support/cases/:id",
            (ID,),
            support_read,
            "read",
            effect="read",
        ),
        Verb(
            "support",
            "open",
            "Open a case from this side: the subject and its first message.",
            "POST /support/cases",
            (Arg("subject", "One line: what it is about.", required=True), BODY),
            support_open,
            effect="outward",
        ),
        Verb(
            "support",
            "reply",
            "Add a message to a case. On a case with a mail thread it is sent as "
            "mail in that thread, to the requester's mailbox, and the case records "
            "who sent it.",
            "POST /support/cases/:id/messages",
            (ID, BODY),
            support_reply,
            effect="outward",
        ),
        Verb(
            "support",
            "close",
            "Close a case; a new message from the requester opens it again.",
            "PATCH /support/cases/:id",
            (ID,),
            support_close,
            effect="draft",
        ),
    ),
    absent={
        "create": "open is its create: a case starts with its first message",
        "update": "a case is its messages, which are never edited: reply adds one, "
                  "close sets its status",
        "delete": "a case is a record of what was said to someone, kept whole",
        "history": "read is its history: every message and event in order",
    },
)
