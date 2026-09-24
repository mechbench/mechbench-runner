from __future__ import annotations

import json

import pytest

from mechbench_runner import bench_cmd as b
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
    {"ended": {**ZERO, "end": 5, "stop": 2}},
    {"kind": "collection", "items": []},
    {"kind": "metric_table", "rows": []},
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
        assert json.loads(out.out) == CUT
        assert "!! stories: of 100 items, 79 cut off at max_tokens" in out.err

    def test_it_is_said_when_the_output_goes_to_a_file(self, patched, capsys, tmp_path):
        patched(CUT)
        assert b.result(object(), "j/stories", "auto", str(tmp_path / "r.json")) == 0
        assert "79 cut off at max_tokens" in capsys.readouterr().err

    def test_a_natural_run_adds_nothing(self, patched, capsys):
        patched({"ended": {**ZERO, "end": 3}})
        assert b.result(object(), "j/stories", "json", None) == 0
        assert capsys.readouterr().err == ""


class TestTheRegistryVerbs:
    def ctx(self, monkeypatch, payload):
        from mechbench_runner.verbs import run as run_verbs

        monkeypatch.setattr(run_verbs, "finished", lambda ctx, run: {"id": run})

        class Bench:
            def result(self, job, node):
                return payload

            def fetch(self, path):
                return payload

        class Ctx:
            def bench(self):
                return Bench()

            def get(self, route, **query):
                return payload

        return Ctx()

    def test_run_result_says_it_first(self, monkeypatch):
        from mechbench_runner.verbs.run import run_result

        out = run_result(self.ctx(monkeypatch, CUT), {"id": "run_x", "node": "gen"})
        assert list(out)[0] == "ended_notice"
        assert out["ended_notice"] == ["gen: of 100 items, 79 cut off at max_tokens"]

    @pytest.mark.parametrize("full", [True, False])
    def test_object_read_says_it_too(self, monkeypatch, full):
        from mechbench_runner.verbs.object import object_read

        out = object_read(self.ctx(monkeypatch, {"outputs": {"gen": CUT}}),
                          {"path": "u/p/results/x", "full": full})
        assert out["ended_notice"] == ["gen: of 100 items, 79 cut off at max_tokens"]

    def test_a_natural_result_is_returned_as_it_was(self, monkeypatch):
        from mechbench_runner.verbs.run import run_result

        payload = {"ended": {**ZERO, "end": 2}}
        out = run_result(self.ctx(monkeypatch, payload), {"id": "r", "node": "gen"})
        assert out == payload


def test_a_job_results_manifest_names_intermediate_nodes_once():
    summaries = {"gen": {"kind": "text/document", "collection": True, "items": 100,
                         "ended": CUT["ended"]},
                 "grade": {"kind": "eval/verdict", "collection": True, "items": 100}}
    notes = ended_notes({"node_summaries": summaries, "outputs": {"gen": CUT}})
    assert notes == ["gen: of 100 items, 79 cut off at max_tokens"]
