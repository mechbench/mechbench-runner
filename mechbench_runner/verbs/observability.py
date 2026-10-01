from __future__ import annotations

from typing import Any
from urllib.parse import quote

from .core import LIMIT, OFFSET, Arg, Ctx, Noun, Verb, listing


def observability_traces(ctx: Ctx, a: dict) -> Any:
    return ctx.get(f"/admin/observability/traces/{quote(str(a['id']))}")


def observability_errors(ctx: Ctx, a: dict) -> Any:
    query = {k: a.get(k) for k in ("since", "search", "limit", "offset")}
    return listing(ctx, "/admin/observability/errors", query)


def ms(v: Any) -> str:
    try:
        return f"{float(v):.1f}ms"
    except (TypeError, ValueError):
        return "?ms"


def kind(s: dict[str, Any]) -> str:
    return "/".join(str(x) for x in (s.get("type"), s.get("subtype")) if x)


def span_lines(spans: list[Any], depth: int = 1) -> list[str]:
    lines: list[str] = []
    for s in spans:
        if not isinstance(s, dict):
            continue
        lines.append(f"{'  ' * depth}{s.get('name')}  {kind(s)}  "
                     f"{ms(s.get('durationMs'))}  {s.get('outcome')}")
        lines += span_lines(s.get("children") or [], depth + 1)
    return lines


def transaction_lines(t: dict[str, Any]) -> list[str]:
    route = " ".join(str(x) for x in (t.get("method"), t.get("name")) if x)
    head = f"{t.get('traceId')}  {route}"
    if t.get("status") is not None:
        head += f"  {t['status']}"
    head += f"  {ms(t.get('durationMs'))}  {t.get('outcome')}"
    who = [f"{t.get('origin')} {t.get('type')} at {t.get('timestamp')}"]
    if t.get("userId"):
        who.append(f"user {t['userId']}")
    if t.get("actorKind") or t.get("via"):
        who.append(" via ".join(str(x) for x in (t.get("actorKind"), t.get("via")) if x))
    for key, label in (("apiKeyId", "key"), ("instance", "instance"), ("release", "release")):
        if t.get(key):
            who.append(f"{label} {t[key]}")
    return [head, " · ".join(who)]


def error_lines(e: dict[str, Any]) -> list[str]:
    what = ":".join(str(x) for x in (e.get("type"), e.get("code")) if x) or "error"
    return [f"  {e.get('timestamp')}  {what}  {e.get('message')}  [{e.get('fingerprint')}]"]


def link_lines(links: dict[str, Any]) -> list[str]:
    lines = [f"  {label} {links[key]}" for key, label in
             (("visitId", "visit"), ("visitorId", "visitor"), ("userId", "user"))
             if links.get(key)]
    lines += [f"  {k} {v}" for k, v in sorted((links.get("entity") or {}).items())]
    return lines


def trace_lines(answer: dict[str, Any]) -> list[str]:
    lines = transaction_lines(answer.get("transaction") or {})
    spans = span_lines(answer.get("spans") or [])
    lines += ["", "spans", *(spans or ["  (none)"])]
    errors = [x for e in answer.get("errors") or [] if isinstance(e, dict)
              for x in error_lines(e)]
    lines += ["", "errors", *(errors or ["  (none)"])]
    links = link_lines(answer.get("links") or {})
    lines += ["", "links", *(links or ["  (none)"])]
    return lines


OBSERVABILITY = Noun(
    "observability",
    "The platform's own records, for site admins: the API server's, the web "
    "app's and the runners' traces (a request and the work inside it, with "
    "durations), their errors grouped by fingerprint, and the instances' "
    "metrics. Every API response names its trace in X-Trace-Id.",
    (
        Verb(
            "observability",
            "traces",
            "One trace by its id (an X-Trace-Id): its transaction (route, "
            "status, duration, who called and through what), its spans as a "
            "tree with their durations, its errors, and the visit, visitor, "
            "user and entities it belongs to.",
            "GET /admin/observability/traces/:id",
            (Arg("id", "The trace id: 32 hex characters, as X-Trace-Id gives it.",
                 required=True, positional=True),),
            observability_traces,
            "read",
            effect="read",
        ),
        Verb(
            "observability",
            "errors",
            "The errors seen since a moment, grouped by fingerprint, most "
            "recently seen first: type, code, message, how many, first and last "
            "seen, and the last occurrence's route and trace id.",
            "GET /admin/observability/errors",
            (
                Arg("since", "Only groups seen at or after this ISO 8601 moment "
                             "(default: a day back)."),
                Arg("search", "Only groups whose type, code or message contains this."),
                LIMIT,
                OFFSET,
            ),
            observability_errors,
            "list",
            ("fingerprint", "type", "code", "count", "lastSeenAt", "lastRoute",
             "lastTraceId"),
            effect="read",
        ),
    ),
    absent={
        "list": "errors lists the error groups; traces are found from a "
                "response's X-Trace-Id, an error group's last trace, or the "
                "portal's routes page",
        "read": "traces reads one trace whole",
        "create": "records are written by the platform as it runs, never by hand",
        "update": "a record is what happened, kept as it was",
        "delete": "records leave by retention: spans after 14 days, "
                  "transactions and metrics after 30, errors after 90",
        "history": "the records are the platform's history",
    },
)
