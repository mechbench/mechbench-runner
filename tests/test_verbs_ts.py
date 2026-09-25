from __future__ import annotations

import contextlib
import io
import pathlib
import runpy

import pytest

from mechbench_runner.config import Config
from mechbench_runner.mcp_server import build_tools
from mechbench_runner.verbs import CONSENT, EFFECTS, NOUNS, noun

ROOT = pathlib.Path(__file__).resolve().parent.parent
COPY = ROOT.parent / "mechbench-models" / "src" / "verbs.generated.ts"
CFG = Config(
    api_base_url="http://api.test",
    api_key="mbk_test",
    poll_interval_seconds=0.01,
    warm_model_id=None,
    runner_id=None,
)
VERBS = [v for n in NOUNS for v in n.verbs]


def _body(text: str) -> str:
    return text.split("\n", 1)[1]


def test_the_models_copy_is_current():
    if not COPY.exists():
        pytest.skip("no mechbench-models checkout beside this one")
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        runpy.run_path(str(ROOT / "scripts" / "dump_verbs_ts.py"))["main"]()
    assert _body(COPY.read_text()) == _body(buf.getvalue()), (
        "mechbench-models/src/verbs.generated.ts is stale: run\n"
        "  python scripts/dump_verbs_ts.py > ../mechbench-models/src/verbs.generated.ts"
    )


@pytest.mark.parametrize("verb", VERBS, ids=lambda v: f"{v.noun}-{v.name}")
def test_every_verb_says_what_it_does(verb):
    assert verb.effect in EFFECTS
    names = {a.name: a for a in verb.args}
    for name, values in verb.consent_when.items():
        assert name in names, f"{verb.noun} {verb.name}: consent names {name}"
        choices = names[name].choices
        assert not choices or set(values) <= set(choices)
    if verb.consent_when:
        assert verb.effect in CONSENT


@pytest.mark.parametrize("verb", VERBS, ids=lambda v: f"{v.noun}-{v.name}")
def test_reads_never_need_consent(verb):
    if verb.effect == "read":
        assert not verb.needs_consent({a.name: True for a in verb.args})


@pytest.mark.parametrize(
    "noun_name,verb_name",
    [
        ("run", "launch"),
        ("run", "sweep"),
        ("run", "rerun"),
        ("protocol", "publish"),
        ("protocol", "unpublish"),
    ],
)
def test_spending_and_publishing_always_need_a_click(noun_name, verb_name):
    assert noun(noun_name).verb(verb_name).needs_consent({})


@pytest.mark.parametrize(
    "verb", [v for v in VERBS if v.name == "delete"], ids=lambda v: v.noun
)
def test_a_delete_needs_consent_only_when_it_deletes(verb):
    assert verb.effect == "delete"
    assert not verb.needs_consent({})
    assert verb.needs_consent({"yes": True})


def test_widening_who_reads_needs_consent_and_narrowing_does_not():
    update = noun("protocol").verb("update")
    assert update.needs_consent({"visibility": "public"})
    assert update.needs_consent({"visibility": "org"})
    assert not update.needs_consent({"visibility": "private", "name": "x"})
    article = noun("article").verb("update")
    assert article.needs_consent({"status": "published"})
    assert not article.needs_consent({"status": "archived"})
    assert noun("thread").verb("update").needs_consent({"visibility": "shared"})


def test_drafts_are_free():
    for noun_name, verb_name in (
        ("protocol", "push"),
        ("protocol", "edit"),
        ("object", "write"),
        ("article", "create"),
        ("article", "edit"),
    ):
        assert not noun(noun_name).verb(verb_name).needs_consent({})


def test_mcp_marks_what_needs_consent():
    tools = build_tools(CFG, executor=object())
    assert "launch(" in tools["run"].__doc__
    line = next(x for x in tools["run"].__doc__.splitlines() if x.startswith("launch("))
    assert line.endswith("[consent: spend]")
    line = next(x for x in tools["run"].__doc__.splitlines() if x.startswith("list("))
    assert "[consent" not in line


def test_every_file_argument_is_local():
    for v in VERBS:
        for a in v.args:
            if a.name in ("file", "body_file", "description_file") or (
                v.name == "export" and a.name == "path"
            ):
                assert a.local, f"{v.noun} {v.name} {a.name}"
