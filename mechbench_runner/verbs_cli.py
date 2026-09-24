from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable
from typing import Any

from . import bench_cmd
from .config import Config
from .verbs import NOUNS, Arg, Ctx, Noun, Verb, VerbError, invoke, noun, refusal

RUN_NOUN = "run-verb"


def flag(a: Arg) -> str:
    return a.flag or "--" + a.name.replace("_", "-")


def add_arg(p: argparse.ArgumentParser, a: Arg) -> None:
    if a.positional:
        p.add_argument(a.name, help=a.help, nargs=None if a.required else "?")
        return
    kw: dict[str, Any] = {"dest": a.name, "help": a.help}
    if a.type == "bool":
        kw["action"] = "store_true"
    elif a.type in ("pairs", "strs"):
        kw["action"] = "append"
        kw["metavar"] = (
            "NAME=VALUE" if a.type == "pairs" else a.name.upper().rstrip("S")
        )
    else:
        kw["type"] = {"int": int, "float": float}.get(a.type, str)
        kw["required"] = a.required
        if a.choices:
            kw["choices"] = a.choices
    p.add_argument(flag(a), **kw)


def add_nouns(sub: argparse._SubParsersAction) -> None:
    for n in NOUNS:
        name = RUN_NOUN if n.name == "run" else n.name
        p = sub.add_parser(
            name,
            help=argparse.SUPPRESS
            if name == RUN_NOUN
            else f"{n.help} Verbs: {', '.join(v.name for v in n.verbs)}.",
        )
        verbs = p.add_subparsers(dest="verb", required=True, metavar="<verb>")
        for v in n.verbs:
            vp = verbs.add_parser(v.name, help=v.help, description=v.help)
            for a in v.args:
                add_arg(vp, a)
            if v.shape == "list":
                vp.add_argument(
                    "--json",
                    dest="as_json",
                    action="store_true",
                    help="Print {items, next} as JSON.",
                )
            if v.shape == "items":
                vp.add_argument(
                    "--table",
                    action="store_true",
                    help="Print the items as a table, not JSON lines.",
                )
        p.set_defaults(noun=n.name)


def rewrite_run(argv: list[str]) -> list[str]:
    verbs = {v.name for v in noun("run").verbs}
    if len(argv) >= 2 and argv[0] == "run" and argv[1] in verbs:
        return [RUN_NOUN, *argv[1:]]
    return argv


def pairs(
    values: list[str] | None, name: str, parse_json: bool
) -> dict[str, Any] | None:
    if not values:
        return None
    out: dict[str, Any] = {}
    for p in values:
        k, eq, v = p.partition("=")
        if not k or not eq:
            raise VerbError(f"{name} wants NAME=VALUE, got {p!r}")
        if parse_json:
            try:
                out[k] = json.loads(v)
            except ValueError:
                out[k] = v
        else:
            out[k] = v
    return out


def args_of(v: Verb, ns: argparse.Namespace) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for a in v.args:
        val = getattr(ns, a.name, None)
        if a.type == "pairs":
            val = pairs(val, flag(a), parse_json=a.name == "params")
        if val is None or val is False or val == []:
            continue
        out[a.name] = val
    return out


def cell(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, float):
        return f"{v:.4g}"
    if isinstance(v, (dict, list)):
        return json.dumps(v, separators=(",", ":"))
    return str(v)


def print_list(v: Verb, out: dict[str, Any], as_json: bool) -> None:
    if as_json:
        print(json.dumps(out, indent=1, default=str))
        return
    items = out.get("items") or []
    if not items:
        print("(none)", file=sys.stderr)
    rows = [[cell(i.get(c)) for c in v.columns] for i in items]
    widths = [
        max([len(c)] + [len(r[k]) for r in rows]) for k, c in enumerate(v.columns)
    ]
    for r in [list(v.columns), *rows] if rows else []:
        print("  ".join(x.ljust(widths[k]) for k, x in enumerate(r)).rstrip())
    if out.get("next") is not None:
        print(f"(more: --offset {out['next']})", file=sys.stderr)


def print_items(out: Any, table: bool) -> None:
    if not isinstance(out, dict) or "items" not in out:
        print(json.dumps(out, indent=1, default=str))
        return
    items = out.get("items") or []
    if table:
        cols: list[str] = []
        for i in items:
            cols += [k for k in i if k not in cols]
        rows = [[cell(i.get(c)).replace("\n", " ") for c in cols] for i in items]
        widths = [max([len(c)] + [len(r[k]) for r in rows]) for k, c in enumerate(cols)]
        for r in [cols, *rows] if rows else []:
            print("  ".join(x.ljust(widths[k]) for k, x in enumerate(r)).rstrip())
    else:
        for i in items:
            print(json.dumps(i, ensure_ascii=False, separators=(",", ":"), default=str))
    offset = int(out.get("offset") or 0)
    matched = out.get("matched", out.get("total"))
    note = f"({len(items)} of {matched} matched, {out.get('total')} in all"
    if matched is not None and offset + len(items) < matched:
        note += f"; more: --offset {offset + len(items)}"
    print(note + ")", file=sys.stderr)


def resolve(ctx: Ctx, n: Noun, target: str) -> str:
    from .verbs import job_of, project_id

    if n.name == "run":
        return job_of(ctx, target)
    if n.name == "project":
        return project_id(ctx, target)
    return target


HISTORY_KIND = {
    "object": "object",
    "protocol": "protocol",
    "run": "job",
    "article": "article",
    "dataset": "dataset",
    "project": "project",
}

Render = Callable[[Config, Ctx, dict[str, Any]], int]


def render_delete(n: Noun) -> Render:
    def go(config: Config, ctx: Ctx, a: dict[str, Any]) -> int:
        target = a.get("path") or resolve(ctx, n, a["id"])
        return bench_cmd.delete(
            config,
            target,
            bool(a.get("prefix")),
            bool(a.get("yes")),
            bool(a.get("acknowledge_citations")),
        )

    return go


def render_history(n: Noun) -> Render:
    def go(config: Config, ctx: Ctx, a: dict[str, Any]) -> int:
        target = a.get("path") or a["id"]
        if n.name == "object" and "/" in target:
            print(
                json.dumps(invoke(ctx, "object", "history", a), indent=1, default=str)
            )
            return 0
        return bench_cmd.history(config, HISTORY_KIND[n.name], resolve(ctx, n, target))

    return go


def render_result(config: Config, ctx: Ctx, a: dict[str, Any]) -> int:
    from .verbs.run import finished

    job = finished(ctx, a["id"])["jobId"]
    return bench_cmd.result(config, f"{job}/{a['node']}", "auto", None)


def render_watch(config: Config, ctx: Ctx, a: dict[str, Any]) -> int:
    from .verbs import job_of

    return bench_cmd.watch(config, [job_of(ctx, a["id"])])


def render_launch(config: Config, ctx: Ctx, a: dict[str, Any]) -> int:
    params = [
        f"{k}={json.dumps(v) if not isinstance(v, str) else v}"
        for k, v in (a.get("params") or {}).items()
    ]
    inputs = [f"{k}={v}" for k, v in (a.get("inputs") or {}).items()]
    return bench_cmd.run(
        config,
        a["protocol"],
        None,
        a.get("budget"),
        False,
        params=params or None,
        inputs=inputs or None,
        keep=a.get("keep"),
        label=a.get("label"),
    )


def render_copy(config: Config, ctx: Ctx, a: dict[str, Any]) -> int:
    from .verbs import split_version

    pid, n = split_version(a)
    return bench_cmd.protocol_copy(
        config,
        f"{pid}@{n}",
        a["into"],
        a.get("name"),
        bool(a.get("org")),
        bool(a.get("dry_run")),
    )


RENDER: dict[tuple[str, str], Render] = {
    ("protocol", "push"): lambda c, _x, a: bench_cmd.protocol_push(
        c, a["file"], a["into"], bool(a.get("org"))
    ),
    ("protocol", "export"): lambda c, _x, a: bench_cmd.protocol_export(
        c, a["id"], a.get("version"), a.get("path")
    ),
    ("protocol", "publish"): lambda c, _x, a: bench_cmd.protocol_publish(
        c, a["id"], a.get("version")
    ),
    ("protocol", "unpublish"): lambda c, _x, a: bench_cmd.protocol_unpublish(
        c, a["id"], a["version"]
    ),
    ("protocol", "copy"): render_copy,
    ("run", "launch"): render_launch,
    ("run", "update"): lambda c, _x, a: bench_cmd.label_run(
        c, a["id"], None if a.get("clear") else a.get("label")
    ),
    ("run", "watch"): render_watch,
    ("run", "result"): render_result,
    ("run", "cancel"): lambda c, _x, a: bench_cmd.cancel(
        c, [a["id"]], a.get("reason") or ""
    ),
    **{(n.name, "delete"): render_delete(n) for n in NOUNS},
    **{(n.name, "history"): render_history(n) for n in NOUNS},
}


def main(config: Config, ns: argparse.Namespace, ctx: Ctx | None = None) -> int:
    n = noun(ns.noun)
    v = n.verb(ns.verb)
    ctx = ctx or Ctx(config)
    try:
        a = args_of(v, ns)
        custom = RENDER.get((n.name, v.name))
        if custom is not None:
            return custom(config, ctx, a)
        out = invoke(ctx, n.name, v.name, a)
    except VerbError as e:
        print(f"{n.name} {v.name}: {e}", file=sys.stderr)
        return 2
    except Exception as e:
        body = refusal(e)
        if body is None:
            raise
        print(
            f"{n.name} {v.name} refused ({body.get('code') or body.get('status')}): "
            f"{body.get('error') or ''}",
            file=sys.stderr,
        )
        source = a.get("body_file") or a.get("description_file") or a.get("file") or "-"
        for r in body.get("refusals") or []:
            print(
                f"{source}:{r.get('field')}:{r.get('line')}:{r.get('column')}: "
                f"{r.get('construct')} — {r.get('message')}",
                file=sys.stderr,
            )
        return 1
    if v.shape == "list":
        print_list(v, out, bool(getattr(ns, "as_json", False)))
    elif v.shape == "items":
        print_items(out, bool(getattr(ns, "table", False)))
    else:
        print(json.dumps(out, indent=1, default=str))
    return 0
