"""Every noun's verbs, on the command line and over MCP (task 000661).

Each verb is run twice with the same arguments, once as `mechbench <noun>
<verb> …` and once as the MCP tool `<noun>(verb, args)`, against a fake
API and fake bench verbs that record every call. The two surfaces must
make the same calls: the same routes with the same query and body, the
same bench verbs with the same arguments. The bench fakes are held to
the real verbs' signatures.

Then the shapes an agent relies on: summaries by default and `full` on
request, `{items, next}` from a listing, a dry run unless `yes`, a
refusal as data, and an unknown argument refused with the verb's list.
"""

from __future__ import annotations

import inspect
import json
from typing import Any

import pytest
from mechbench_compute import bench

from mechbench import cli
from mechbench_runner import bench_cmd
from mechbench_runner.api_client import ApiError
from mechbench_runner.config import Config
from mechbench_runner.mcp_server import build_tools
from mechbench_runner.verbs import NOUNS, Arg, Ctx, Verb

CFG = Config(
    api_base_url="http://api.test",
    api_key="mbk_test",
    poll_interval_seconds=0.01,
    warm_model_id=None,
    runner_id=None,
)

ANSWER = {
    "id": "x_1",
    "jobId": "j_1",
    "jobStatus": "done",
    "resultPath": "benji/lab/results/r1",
    "version": 3,
    "text": "{}\n",
    "name": "draws",
    "protocol": {"id": "prt_1", "name": "d"},
    "deletes": {"objects": 1},
    "keeps": {},
    "citedBy": [],
    "unreadable": 0,
    "lifetime": {"kind": "protocol", "id": "prt_1"},
    "events": [],
    "action": "created",
    "user": {"handle": "benji"},
    "project": {"id": "proj_1", "slug": "lab"},
    "status": "cancelled",
    "from": "queued",
}

BENCH_VERBS = (
    "launch",
    "push_protocol",
    "export_protocol",
    "get_protocol",
    "publish_protocol_version",
    "unpublish_protocol_version",
    "copy_protocol_version",
    "delete",
    "history",
    "cancel",
    "label_run",
    "result",
    "emit",
    "fetch",
    "watch",
    "runs",
)


@pytest.fixture
def rec(monkeypatch, tmp_path):
    """Record API calls (through `Ctx.api`) and bench calls, in order."""
    calls: list[tuple] = []

    def api(self, method, route, *, query=None, body=None):
        calls.append(
            (
                "api",
                method,
                route,
                {k: v for k, v in (query or {}).items() if v is not None},
                body,
            )
        )
        if route in ("/objects",):
            return {"objects": [{"path": "benji/lab/x"}]}, {"x-next-offset": "3"}
        if method == "GET" and route.count("/") == 1 and route not in ("/auth/me",):
            return [{"id": "row_1"}], {"x-next-offset": "3"}
        return dict(ANSWER), {}

    monkeypatch.setattr(Ctx, "api", api)
    monkeypatch.setattr(bench, "configure", lambda **_k: None)
    monkeypatch.setattr(bench_cmd, "HISTORY", tmp_path / "runs.jsonl")
    for name in BENCH_VERBS:
        sig = inspect.signature(getattr(bench, name))

        def fake(*args, _name=name, _sig=sig, **kwargs):
            _sig.bind(*args, **kwargs)
            calls.append(("bench", _name, args, kwargs))
            if _name == "watch":
                return iter(())
            if _name == "runs":
                return []
            return dict(ANSWER)

        monkeypatch.setattr(bench, name, fake)
    monkeypatch.setattr(Config, "from_env", classmethod(lambda cls: CFG))
    return calls


IDS = {
    "object": "benji/lab/x",
    "protocol": "prt_1",
    "run": "j_1",
    "article": "art_1",
    "dataset": "ds_1",
    "project": "proj_1",
}


def sample(noun: str, a: Arg, tmp) -> Any:
    """A value for one argument, the same whichever surface gets it."""
    if a.name in ("id", "path"):
        return IDS[noun]
    if a.name == "into":
        return "benji/lab"
    if a.name == "file":
        f = tmp / f"{noun}.json"
        f.write_text('{"kind": "note"}')
        return str(f)
    if a.name in ("body_file", "description_file"):
        f = tmp / "body.json"
        f.write_text('{"ops": [{"insert": "Words.\\n"}]}')
        return str(f)
    if a.name == "protocol" and noun == "run":
        return "prt_1"
    if a.name == "inputs":
        return {"corpus": "benji/lab/x"}
    if a.choices:
        return a.choices[-1]
    return {
        "str": f"{a.name}-v",
        "int": 2,
        "float": 1.5,
        "bool": True,
        "json": {"kind": "note"},
        "pairs": {"n": 3},
        "strs": ["one"],
    }[a.type]


def argv_of(noun: str, v: Verb, args: dict[str, Any]) -> list[str]:
    from mechbench_runner.verbs_cli import flag

    out = [noun, v.name]
    for a in v.args:
        if a.name not in args:
            continue
        val = args[a.name]
        if a.positional:
            out.append(str(val))
        elif a.type == "bool":
            out.append(flag(a))
        elif a.type == "pairs":
            for k, x in val.items():
                out += [
                    flag(a),
                    f"{k}={json.dumps(x) if not isinstance(x, str) else x}",
                ]
        elif a.type == "strs":
            for x in val:
                out += [flag(a), x]
        elif a.type == "json":
            continue  # the command line takes a FILE for what MCP passes inline
        else:
            out += [flag(a), str(val)]
    return out


#: Verbs whose command line keeps its own way of waiting or reading
#: (bench_cmd's watch and result), tested on their own below.
OWN_WAY = {("run", "watch"), ("run", "result")}

CASES = [(n.name, v.name) for n in NOUNS for v in n.verbs]


@pytest.mark.parametrize("noun,verb", CASES)
def test_each_verb_makes_the_same_calls_on_both_surfaces(
    noun, verb, rec, tmp_path, capsys
):
    n = next(x for x in NOUNS if x.name == noun)
    v = n.verb(verb)
    args = {a.name: sample(noun, a, tmp_path) for a in v.args if a.type != "json"}
    if (noun, verb) == ("run", "update"):
        args.pop("clear")
    if (noun, verb) == ("protocol", "read"):
        args.pop("format")  # a format reads the live protocol, not a version
    tools = build_tools(CFG, executor=object())
    out = tools[noun](verb, args)
    assert not (isinstance(out, dict) and "error" in out), out
    mcp_calls = list(rec)
    rec.clear()
    assert cli.main(argv_of(noun, v, args)) in (0, 1), capsys.readouterr()
    cli_calls = list(rec)
    assert mcp_calls, "the verb reached nothing"
    if (noun, verb) in OWN_WAY:
        return
    assert cli_calls == mcp_calls


def test_every_verb_is_exercised():
    assert len(CASES) == sum(len(n.verbs) for n in NOUNS) >= 40


class TestShapes:
    def test_a_read_is_a_summary_unless_full(self, rec):
        tools = build_tools(CFG, executor=object())
        tools["protocol"]("read", {"id": "prt_1"})
        assert rec[-1][1:4] == ("GET", "/protocols/prt_1", {"view": "summary"})
        tools["protocol"]("read", {"id": "prt_1", "full": True})
        assert rec[-1][3] == {"view": "full"}
        tools["protocol"]("read", {"id": "prt_1", "version": 2})
        assert rec[-1][2] == "/protocols/prt_1/versions/2"

    def test_an_object_read_is_its_header_and_full_is_its_payload(self, rec):
        tools = build_tools(CFG, executor=object())
        tools["object"]("read", {"path": "benji/lab/x"})
        assert rec[-1][1:4] == ("GET", "/objects/~meta", {"path": "benji/lab/x"})
        tools["object"]("read", {"path": "benji/lab/x", "full": True})
        assert rec[-1][:3] == ("bench", "fetch", ("benji/lab/x",))

    def test_a_listing_answers_items_and_the_next_offset(self, rec):
        tools = build_tools(CFG, executor=object())
        out = tools["article"]("list", {"search": "ladder", "limit": 3})
        assert out == {"items": [{"id": "row_1"}], "next": 3}
        assert rec[-1][3] == {"search": "ladder", "limit": 3, "view": "summary"}
        objs = tools["object"]("list", {"prefix": "benji/lab"})
        assert objs["items"] == [{"path": "benji/lab/x"}]

    def test_delete_is_a_dry_run_unless_yes(self, rec):
        tools = build_tools(CFG, executor=object())
        out = tools["dataset"]("delete", {"id": "ds_1"})
        assert out["deleted"] is False
        assert [c[1] for c in rec] == ["delete"] and rec[-1][3] == {
            "prefix": False,
            "dry_run": True,
        }
        rec.clear()
        assert (
            tools["dataset"]("delete", {"id": "ds_1", "yes": True})["deleted"] is True
        )
        assert [c[3].get("dry_run") for c in rec] == [True, None]

    def test_a_cited_thing_is_not_deleted_without_acknowledging(self, rec, monkeypatch):
        cited = {**ANSWER, "citedBy": [{"title": "An article"}]}
        monkeypatch.setattr(bench, "delete", lambda target, **k: rec.append(k) or cited)
        tools = build_tools(CFG, executor=object())
        out = tools["protocol"]("delete", {"id": "prt_1", "yes": True})
        assert out["deleted"] is False and out["refusal"]["code"] == "CITED"
        assert len(rec) == 1  # the dry run only
        out = tools["protocol"](
            "delete", {"id": "prt_1", "yes": True, "acknowledge_citations": True}
        )
        assert out["deleted"] is True and rec[-1] == {
            "prefix": False,
            "acknowledge_citations": True,
        }

    def test_a_run_id_is_resolved_to_its_job(self, rec):
        tools = build_tools(CFG, executor=object())
        tools["run"]("rerun", {"id": "run_7"})
        assert [c[2] for c in rec] == ["/runs/run_7", "/jobs/j_1/rerun"]

    def test_a_project_by_owner_and_slug(self, rec):
        tools = build_tools(CFG, executor=object())
        tools["project"]("update", {"id": "benji/lab", "name": "Lab"})
        assert rec[0][2] == "/projects/by/benji/lab"
        assert rec[-1][1:3] == ("PATCH", "/projects/proj_1")
        assert rec[-1][4] == {"displayName": "Lab"}

    def test_a_create_names_its_owner_or_asks_who_you_are(self, rec):
        tools = build_tools(CFG, executor=object())
        tools["project"]("create", {"slug": "ladders"})
        assert rec[0][2] == "/auth/me"
        assert rec[-1][4] == {
            "ownerKind": "user",
            "ownerHandle": "benji",
            "slug": "ladders",
            "displayName": "ladders",
        }
        rec.clear()
        tools["project"]("create", {"slug": "x", "owner": "lab-org", "org": True})
        assert [c[2] for c in rec] == ["/projects"]
        assert rec[-1][4]["ownerKind"] == "org"

    def test_an_api_refusal_is_data_on_mcp_and_exit_1_on_the_cli(
        self, monkeypatch, capsys
    ):
        def refuse(self, method, route, **_k):
            raise ApiError(
                409, {"code": "ALREADY_EXISTS", "error": "slug already in use"}
            )

        monkeypatch.setattr(Ctx, "api", refuse)
        monkeypatch.setattr(Config, "from_env", classmethod(lambda cls: CFG))
        tools = build_tools(CFG, executor=object())
        out = tools["project"]("create", {"slug": "lab", "owner": "benji"})
        assert out == {
            "error": {
                "status": 409,
                "code": "ALREADY_EXISTS",
                "error": "slug already in use",
            }
        }
        assert cli.main(["project", "create", "--slug", "lab", "--owner", "benji"]) == 1
        assert "refused (ALREADY_EXISTS)" in capsys.readouterr().err

    def test_an_unknown_argument_is_refused_with_the_verbs_list(self, rec):
        tools = build_tools(CFG, executor=object())
        with pytest.raises(ValueError, match="takes .*id.*not colour"):
            tools["protocol"]("read", {"id": "prt_1", "colour": "blue"})
        with pytest.raises(ValueError, match="needs id"):
            tools["protocol"]("read", {})
        with pytest.raises(ValueError, match="no verb 'frobnicate'"):
            tools["protocol"]("frobnicate", {})

    def test_a_markdown_body_is_an_edit_at_the_version_read(
        self, rec, tmp_path, capsys
    ):
        f = tmp_path / "body.md"
        f.write_text("# A heading\n")
        assert cli.main(["article", "update", "art_1", "--body-file", str(f)]) == 2
        assert "pass base_version" in capsys.readouterr().err
        rec.clear()
        argv = [
            "article",
            "update",
            "art_1",
            "--body-file",
            str(f),
            "--base-version",
            "4",
        ]
        assert cli.main(argv) == 0
        assert rec[-1][1:] == (
            "PUT",
            "/articles/art_1",
            {"format": "markdown"},
            {"body": "# A heading\n", "baseVersion": 4},
        )

    def test_a_formatted_read_edited_goes_back_at_its_version(self, rec, tmp_path):
        tools = build_tools(CFG, executor=object())
        tools["article"]("read", {"id": "art_1", "format": "markdown"})
        assert rec[-1][1:4] == ("GET", "/articles/art_1", {"format": "markdown"})
        read = {
            "article": {"id": "art_1", "title": "T", "body": "Words.\n"},
            "collabEdit": 7,
            "format": "markdown",
        }
        f = tmp_path / "a.json"
        f.write_text(json.dumps(read))
        tools["article"]("edit", {"id": "art_1", "file": str(f)})
        assert rec[-1][1:] == (
            "PUT",
            "/articles/art_1",
            {"format": "markdown"},
            {"id": "art_1", "title": "T", "body": "Words.\n", "baseVersion": 7},
        )
        tools["protocol"]("read", {"id": "prt_1", "format": "markdown"})
        assert rec[-1][1:4] == ("GET", "/protocols/prt_1", {"format": "markdown"})
        with pytest.raises(ValueError, match="base_version"):
            tools["protocol"]("edit", {"id": "prt_1", "name": "n"})

    def test_markdown_refusals_print_where_they_stand(
        self, rec, monkeypatch, tmp_path, capsys
    ):
        def refuse(self, method, route, *, query=None, body=None):
            raise ApiError(
                422,
                {
                    "code": "MARKDOWN_UNSUPPORTED",
                    "error": "1 construct rich text cannot hold",
                    "refusals": [
                        {
                            "field": "body",
                            "line": 12,
                            "column": 3,
                            "construct": "html",
                            "message": "HTML other than an embed tag",
                        }
                    ],
                },
            )

        monkeypatch.setattr(Ctx, "api", refuse)
        f = tmp_path / "b.md"
        f.write_text("x\n")
        argv = [
            "article",
            "edit",
            "art_1",
            "--body-file",
            str(f),
            "--base-version",
            "1",
        ]
        assert cli.main(argv) == 1
        err = capsys.readouterr().err
        assert f"{f}:body:12:3: html — HTML other than an embed tag" in err

    def test_run_launches_bare_and_as_a_verb(self, rec, capsys):
        assert cli.main(["run", "prt_1", "--label", "P0"]) == 0
        bare = [c for c in rec if c[0] == "bench"]
        rec.clear()
        assert cli.main(["run", "launch", "prt_1", "--label", "P0"]) == 0
        assert [c for c in rec if c[0] == "bench"] == bare

    def test_watch_and_result_on_the_cli_resolve_a_run_to_its_job(self, rec, capsys):
        assert cli.main(["run", "watch", "run_7"]) == 0
        assert rec[-1][:3] == ("bench", "watch", (["j_1"],))
        rec.clear()
        cli.main(["run", "result", "run_7", "stories"])
        assert ("bench", "result", ("j_1", "stories"), {}) in rec
