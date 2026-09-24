"""The registry's parts: an argument, a verb, a noun, and what a verb
reaches the platform through (task 000661). The nouns are the modules
beside this one; `__init__` gathers them."""

from __future__ import annotations

import json
import pathlib
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from ..api_client import ApiClient
from ..config import Config


@dataclass(frozen=True)
class Arg:
    """One argument, by the name it has on every surface: the CLI's
    `--name` (dashes for underscores) or positional, and the MCP `args`
    key. `type` is str, int, float, bool, json (an object or list given as
    JSON on the command line), pairs (`NAME=VALUE`, repeated) or strs (a
    repeated string)."""

    name: str
    help: str
    type: str = "str"
    required: bool = False
    positional: bool = False
    many: bool = False
    choices: tuple[str, ...] = ()
    #: The command line's spelling when it is not `--name` (`--param`).
    flag: str | None = None


@dataclass(frozen=True)
class Verb:
    noun: str
    name: str
    help: str
    #: The route, `METHOD /path`, as mechbench-api declares it.
    api: str
    args: tuple[Arg, ...]
    do: Callable[[Ctx, dict[str, Any]], Any]
    #: list, read or act: how the command line prints the answer.
    shape: str = "act"
    #: A listing's columns on the command line.
    columns: tuple[str, ...] = ()


@dataclass(frozen=True)
class Noun:
    name: str
    help: str
    verbs: tuple[Verb, ...]
    #: A lifecycle verb this noun does not have, and why.
    absent: Mapping[str, str] = field(default_factory=dict)

    def verb(self, name: str) -> Verb:
        for v in self.verbs:
            if v.name == name:
                return v
        raise KeyError(name)


class Ctx:
    """What a verb reaches the platform through: this machine's
    credentials, the API, and the bench library for the calls it owns."""

    def __init__(
        self, config: Config, client: Callable[[Config], ApiClient] = ApiClient
    ) -> None:
        self.config = config
        self._client = client

    def api(
        self,
        method: str,
        route: str,
        *,
        query: Mapping[str, Any] | None = None,
        body: Any = None,
    ) -> tuple[Any, Mapping[str, str]]:
        with self._client(self.config) as api:
            return api.call(method, route, query=query, body=body)

    def get(self, route: str, **query: Any) -> Any:
        return self.api("GET", route, query=query)[0]

    def bench(self) -> Any:
        from mechbench_compute import bench

        bench.configure(
            api_url=self.config.api_base_url, api_key=self.config.require_api_key()
        )
        return bench


class VerbError(RuntimeError):
    """A verb was asked for something it cannot do as asked."""


# --- shared shapes ------------------------------------------------------------

ID = Arg("id", "Its id.", required=True, positional=True)
FULL = Arg("full", "The whole thing, not the summary.", type="bool")
SEARCH = Arg("search", "Only those whose name, title or slug contains this.")
LIMIT = Arg("limit", "At most this many.", type="int")
OFFSET = Arg("offset", "Start here (a listing's `next`).", type="int")
OWNER = Arg("owner", "A user or org handle (default: your own).")
YES = Arg("yes", "Delete, not just say what deleting would do.", type="bool")
ACK = Arg("acknowledge_citations", "Delete even though articles cite it.", type="bool")
ORG = Arg("org", "The owner is an org.", type="bool")
VISIBILITY = Arg("visibility", "Who can read it.", choices=("private", "org", "public"))
FORMAT = Arg(
    "format",
    "Rich text as markdown (MARKDOWN.md's flavor) or as the delta itself.",
    choices=("markdown", "delta"),
)
EDITED = Arg(
    "file",
    "What `read --format` gave, edited: a JSON file (its collabEdit is the base).",
)
BASE = Arg(
    "base_version",
    "The version it was read at (a read's collabEdit), when the file does not say.",
    type="int",
)


def view(a: Mapping[str, Any]) -> str:
    return "full" if a.get("full") else "summary"


def listing(
    ctx: Ctx,
    route: str,
    query: Mapping[str, Any],
    unwrap: str | None = None,
) -> dict[str, Any]:
    data, headers = ctx.api("GET", route, query=query)
    items = data.get(unwrap, []) if unwrap and isinstance(data, dict) else data
    nxt = headers.get("x-next-offset")
    return {"items": items, "next": int(nxt) if nxt else None}


def page(a: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "search": a.get("search"),
        "limit": a.get("limit"),
        "offset": a.get("offset"),
    }


def unwrap(data: Any, key: str) -> Any:
    return data.get(key, data) if isinstance(data, dict) else data


def given(
    a: Mapping[str, Any],
    *names: str,
    rename: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """The named arguments that were given, under the API's field names."""
    rename = rename or {}
    return {rename.get(n, _camel(n)): a[n] for n in names if a.get(n) is not None}


def _camel(name: str) -> str:
    head, *rest = name.split("_")
    return head + "".join(w.title() for w in rest)


def owner_of(a: Mapping[str, Any], ctx: Ctx) -> tuple[str, str]:
    """`(ownerKind, ownerHandle)` for a create: the named owner, or you."""
    handle = a.get("owner")
    if not handle:
        handle = str(ctx.get("/auth/me").get("user", {}).get("handle") or "")
    return ("org" if a.get("org") else "user"), handle


def delete(
    ctx: Ctx, target: str, a: Mapping[str, Any], prefix: bool = False
) -> dict[str, Any]:
    """The bench's deletion: a dry run unless `yes`, its refusal answered
    as data. What it deletes is gone; there is no restore."""
    bench = ctx.bench()
    plan = bench.delete(target, prefix=prefix, dry_run=True)
    if plan.get("refusal") or not a.get("yes"):
        return {"deleted": False, **plan}
    if (plan.get("citedBy") or plan.get("unreadable")) and not a.get(
        "acknowledge_citations"
    ):
        return {
            "deleted": False,
            **plan,
            "refusal": {
                "code": "CITED",
                "error": "articles cite it: repeat with acknowledge_citations",
            },
        }
    bench.delete(
        target,
        prefix=prefix,
        acknowledge_citations=bool(a.get("acknowledge_citations")),
    )
    return {"deleted": True, **plan, "dryRun": False}


def history(ctx: Ctx, kind: str, entity_id: str) -> Any:
    return ctx.bench().history(kind, entity_id)


def text_of(a: Mapping[str, Any], name: str) -> str | None:
    """A `*_file` argument's text, when one was given."""
    f = a.get(name)
    return None if f is None else pathlib.Path(f).read_text()


def edit(
    ctx: Ctx, noun: str, route: str, a: Mapping[str, Any], text_arg: str, field: str
) -> Any:
    """An edit of a whole article or protocol at a base version (epic
    000525, `PUT`): the object a formatted read gave (the `file`), with
    any field given as an argument in place of the file's, sent back with
    the version it was read at. The API diffs it against the document at
    that version, so edits made since are kept, and answers the object as
    it now stands."""
    obj: dict[str, Any] = {}
    base = a.get("base_version")
    fmt = a.get("format")
    f = a.get("file")
    if f is not None:
        data = json.loads(pathlib.Path(f).read_text())
        if not isinstance(data, dict):
            raise VerbError(f"{f} is not an {noun} as a read gives it")
        inner = data.get(noun, data)
        obj = dict(inner) if isinstance(inner, dict) else {}
        if base is None:
            base = data.get("collabEdit", obj.get("collabEdit", obj.get("baseVersion")))
        fmt = fmt or data.get("format")
    fmt = fmt or "markdown"
    text = text_of(a, text_arg)
    if text is not None:
        obj[field] = json.loads(text) if fmt == "delta" else text
    for name in ("title", "subtitle", "tags", "name"):
        if a.get(name) is not None:
            obj[name] = a[name]
    if base is None:
        raise VerbError(
            f"an edit names the version it was read at: `{noun} read --format "
            f"{fmt}` gives it as collabEdit; pass base_version, or edit that file"
        )
    obj.pop("collabEdit", None)
    obj.pop("format", None)
    obj["baseVersion"] = int(base)
    return ctx.api("PUT", route, query={"format": fmt}, body=obj)[0]


LIFECYCLE = ("list", "read", "create", "update", "delete", "history")


def invoke_on(ctx: Ctx, n: Noun, verb_name: str, args: Mapping[str, Any] | None) -> Any:
    """Run one of a noun's verbs with its arguments by name. Unknown and
    missing arguments are refused here with the verb's own list, so a
    caller learns its shape from the error."""
    noun_name = n.name
    try:
        v = n.verb(verb_name)
    except KeyError:
        raise VerbError(
            f"{noun_name} has no verb {verb_name!r}: "
            f"{', '.join(x.name for x in n.verbs)}"
        ) from None
    got = dict(args or {})
    known = {a.name for a in v.args}
    unknown = sorted(set(got) - known)
    if unknown:
        raise VerbError(
            f"{noun_name} {verb_name} takes {', '.join(sorted(known)) or 'nothing'}; "
            f"not {', '.join(unknown)}"
        )
    missing = [a.name for a in v.args if a.required and got.get(a.name) is None]
    if missing:
        raise VerbError(f"{noun_name} {verb_name} needs {', '.join(missing)}")
    return v.do(ctx, got)


def refusal(e: Exception) -> dict[str, Any] | None:
    """An API refusal as data (`{status, code, error, …}`), or None."""
    status = getattr(e, "status", None)
    body = getattr(e, "body", None)
    if status is None:
        return None
    return {
        "status": status,
        **(body if isinstance(body, dict) else {"error": str(body)}),
    }
