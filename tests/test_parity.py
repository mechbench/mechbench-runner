"""The parity gate (task 000661): a verb on one surface is on all three,
or its absence is written down with the reason.

- Every command line command is a noun, a shorter name for a noun's verb
  (`ALIASES`), or this machine's own (`CLI_ONLY`), and every MCP tool is
  a noun or `MCP_ONLY`: a new command or tool that is none of these fails
  until it is one.
- Every noun parser on the command line has exactly the registry's verbs,
  each with the registry's arguments, and every noun's MCP tool offers
  exactly those verbs.
- Every noun has list, read, create, update, delete and history, or says
  why not.
- Every verb's API route is declared in mechbench-api's routes, read from
  a checkout beside this one (`MECHBENCH_API_SRC` names another; skipped
  when there is none).
- docs/CAPABILITIES.md carries the matrix the registry writes.
"""

from __future__ import annotations

import argparse
import os
import re
import typing
from pathlib import Path

import pytest

from mechbench.cli import build_parser
from mechbench_runner import capabilities
from mechbench_runner.config import Config
from mechbench_runner.mcp_server import build_tools
from mechbench_runner.verbs import LIFECYCLE, NOUNS
from mechbench_runner.verbs_cli import RUN_NOUN, flag

ROOT = Path(__file__).resolve().parent.parent
CFG = Config(
    api_base_url="http://api.test",
    api_key="mbk_test",
    poll_interval_seconds=0.01,
    warm_model_id=None,
    runner_id=None,
)


def subcommands(p: argparse.ArgumentParser) -> dict[str, argparse.ArgumentParser]:
    for a in p._actions:  # noqa: SLF001 — argparse keeps its subparsers nowhere public
        if isinstance(a, argparse._SubParsersAction):  # noqa: SLF001
            return dict(a.choices)
    return {}


def test_every_command_is_a_noun_an_alias_or_this_machines():
    commands = set(subcommands(build_parser())) - {RUN_NOUN}
    nouns = {n.name for n in NOUNS}
    explained = nouns | set(capabilities.ALIASES) | set(capabilities.CLI_ONLY)
    assert commands - explained == set(), (
        "a command with no place in docs/CAPABILITIES.md: make it a noun's verb, "
        "or add it to ALIASES or CLI_ONLY in mechbench_runner/capabilities.py "
        "with its reason"
    )
    assert explained - commands == set(), "listed in capabilities.py but not a command"


def test_every_mcp_tool_is_a_noun_or_says_why():
    tools = set(build_tools(CFG, executor=object()))
    assert tools == {n.name for n in NOUNS} | set(capabilities.MCP_ONLY)


@pytest.mark.parametrize("noun", NOUNS, ids=lambda n: n.name)
def test_the_command_line_has_each_verb_with_its_arguments(noun):
    top = subcommands(build_parser())
    parser = top[RUN_NOUN if noun.name == "run" else noun.name]
    verbs = subcommands(parser)
    assert set(verbs) == {v.name for v in noun.verbs}
    for v in noun.verbs:
        declared = {a.dest for a in verbs[v.name]._actions}  # noqa: SLF001
        options = {s for a in verbs[v.name]._actions for s in a.option_strings}  # noqa: SLF001
        for a in v.args:
            assert a.name in declared, f"{noun.name} {v.name} lacks {a.name}"
            if not a.positional:
                assert flag(a) in options


@pytest.mark.parametrize("noun", NOUNS, ids=lambda n: n.name)
def test_mcp_offers_each_verb(noun):
    tool = build_tools(CFG, executor=object())[noun.name]
    offered = typing.get_args(tool.__annotations__["verb"])
    assert set(offered) == {v.name for v in noun.verbs}
    for v in noun.verbs:
        assert f"\n{v.name}(" in tool.__doc__, (
            f"{noun.name}'s description lacks {v.name}"
        )


@pytest.mark.parametrize("noun", NOUNS, ids=lambda n: n.name)
def test_every_noun_has_its_whole_lifecycle_or_says_why(noun):
    have = {v.name for v in noun.verbs}
    for verb in LIFECYCLE:
        assert verb in have or noun.absent.get(verb), (
            f"{noun.name} has no {verb}, and no reason in its `absent`"
        )
    assert not (set(noun.absent) & have), "a reason given for a verb it has"


def api_source() -> Path | None:
    src = Path(
        os.environ.get("MECHBENCH_API_SRC") or ROOT.parent / "mechbench-api" / "src"
    )
    return src if (src / "app.ts").exists() else None


def declared_routes(src: Path) -> set[tuple[str, str]]:
    """`(METHOD, /mount/path)` for every route mechbench-api declares: each
    routes file's `r.get("/…")`, under where app.ts mounts it."""
    app = (src / "app.ts").read_text()
    files: dict[str, Path] = {}
    for fn, mod in re.findall(r"import \{([^}]*)\} from \"\./routes/(\w+)\.js\"", app):
        for name in (x.strip() for x in fn.split(",")):
            files[name] = src / "routes" / f"{mod}.ts"
    out: set[tuple[str, str]] = set()
    for mount, fn in re.findall(r'app\.route\("([^"]+)",\s*(\w+)\(\)\)', app):
        path = files.get(fn)
        if path is None:
            continue
        for method, sub in re.findall(
            r'\br\.(get|post|put|patch|delete)\(\s*"([^"]*)"', path.read_text()
        ):
            sub = re.sub(r"\{[^}]*\}", "", sub)
            out.add((method.upper(), (mount + sub).rstrip("/") or "/"))
    return out


def normal(route: str) -> str:
    return re.sub(r":\w+", ":_", route)


@pytest.mark.parametrize("noun", NOUNS, ids=lambda n: n.name)
def test_every_verb_names_a_route_the_api_declares(noun):
    src = api_source()
    if src is None:
        pytest.skip("no mechbench-api checkout beside this one")
    routes = {(m, normal(p)) for m, p in declared_routes(src)}
    for v in noun.verbs:
        method, _, path = v.api.partition(" ")
        assert (method, normal(path)) in routes, (
            f"{noun.name} {v.name}: no {v.api} in the API"
        )


def test_the_matrix_in_the_docs_is_the_registrys():
    doc = (ROOT / "docs" / "CAPABILITIES.md").read_text()
    assert capabilities.splice(doc) == doc, (
        "docs/CAPABILITIES.md is stale: run `python scripts/capabilities.py`"
    )
