"""A result whose generated items did not all end naturally says so,
in words, wherever it is read: `mechbench result` and the MCP
`get_result` tool. A header without `ended` says nothing."""

from __future__ import annotations

import json

import pytest

from mechbench_runner import bench_cmd as b
from mechbench_runner import mcp_server
from mechbench_runner.endings import ended_notes

ZERO = {"end": 0, "stop": 0, "max_tokens": 0, "tool_call": 0,
        "filtered": 0, "empty": 0, "other": 0}
CUT = {"kind": "collection", "item_kind": "text/document", "items": [],
       "ended": {**ZERO, "end": 21, "max_tokens": 79}}


def test_a_cut_corpus_is_said_in_words():
    assert ended_notes(CUT, "stories") == [
        "stories: of 100 items, 79 cut off at max_tokens"]


def test_every_unnatural_ending_is_named():
    payload = {"ended": {**ZERO, "end": 1, "max_tokens": 2, "empty": 3,
                         "filtered": 1, "other": 1}}
    [note] = ended_notes(payload)
    assert note == ("of 8 items, 2 cut off at max_tokens, 3 empty, "
                    "1 filtered by the provider, 1 ended for another reason "
                    "(see metadata.call.stop_reason)")


@pytest.mark.parametrize("payload", [
    {"ended": {**ZERO, "end": 5, "stop": 2}},        # all natural
    {"kind": "collection", "items": []},             # stored before the count
    {"kind": "metric_table", "rows": []},            # not a generation
    None,
])
def test_nothing_to_say_says_nothing(payload):
    assert ended_notes(payload) == []


def test_a_job_results_outputs_are_each_read():
    notes = ended_notes({"outputs": {"gen": CUT, "grade": {"rows": []},
                                     "chat": {"ended": {**ZERO, "end": 4}}}})
    assert notes == ["gen: of 100 items, 79 cut off at max_tokens"]


@pytest.fixture
def patched(monkeypatch):
    monkeypatch.setattr(b, "_connect", lambda config: None)

    def install(payload):
        monkeypatch.setattr(b.bench, "result", lambda source, node: payload)
    return install


class TestTheCommand:
    def test_the_count_follows_the_output_on_stderr(self, patched, capsys):
        patched(CUT)
        assert b.result(object(), "j/stories", "auto", None) == 0
        out = capsys.readouterr()
        assert json.loads(out.out) == CUT            # stdout stays parseable
        assert "!! stories: of 100 items, 79 cut off at max_tokens" in out.err

    def test_it_is_said_when_the_output_goes_to_a_file(self, patched, capsys, tmp_path):
        patched(CUT)
        assert b.result(object(), "j/stories", "auto", str(tmp_path / "r.json")) == 0
        assert "79 cut off at max_tokens" in capsys.readouterr().err

    def test_a_natural_run_adds_nothing(self, patched, capsys):
        patched({"ended": {**ZERO, "end": 3}})
        assert b.result(object(), "j/stories", "json", None) == 0
        assert capsys.readouterr().err == ""


class TestTheMcpTool:
    def tools(self, monkeypatch, payload):
        class Api:
            def __init__(self, cfg):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def fetch_object(self, path):
                return b"raw"
        monkeypatch.setattr(mcp_server, "ApiClient", Api)
        monkeypatch.setattr(mcp_server, "_decode_object", lambda raw: payload)
        return mcp_server.build_tools(object())

    def test_the_notice_comes_first(self, monkeypatch):
        tools = self.tools(monkeypatch, {"outputs": {"gen": CUT}})
        out = tools["get_result"]("u/p/results/x")
        assert list(out)[0] == "ended_notice"
        assert out["ended_notice"] == ["gen: of 100 items, 79 cut off at max_tokens"]
        assert out["outputs"] == {"gen": CUT}

    def test_a_natural_result_is_returned_as_it_was(self, monkeypatch):
        payload = {"outputs": {"gen": {"ended": {**ZERO, "end": 2}}}}
        tools = self.tools(monkeypatch, payload)
        assert tools["get_result"]("u/p/results/x") == payload


def test_a_job_results_manifest_names_intermediate_nodes_once():
    summaries = {"gen": {"kind": "text/document", "collection": True, "items": 100,
                         "ended": CUT["ended"]},
                 "grade": {"kind": "eval/verdict", "collection": True, "items": 100}}
    notes = ended_notes({"node_summaries": summaries, "outputs": {"gen": CUT}})
    assert notes == ["gen: of 100 items, 79 cut off at max_tokens"]
