from __future__ import annotations

import pytest

from mechbench import cli
from mechbench_runner.config import Config
from mechbench_runner.verbs import Ctx, VerbError, invoke, noun
from mechbench_runner.verbs import live as live_mod

CFG = Config(api_base_url="http://api.test", api_key="mbk_test",
             poll_interval_seconds=0.01, warm_model_id=None, runner_id=None)

ANSWER = {
    "seq": 3, "address": None, "kind": "logits/funnel",
    "summary": {"kind": "logits/funnel", "collection": True, "items": 8},
    "result": {"kind": "collection", "items": []}, "lines": ["8 logits/funnel items"],
    "timing": {"queuedMs": 1, "runMs": 80, "totalMs": 95},
    "provenance": {"op": "logits/read-layers", "pin": None, "model": "m@r", "compute": "0.182.0",
                   "seed": 1, "hash": "sha256:" + "a" * 64},
}
NAMES = {"axis": {"seq": 3, "path": "~scratch/live_9/axis", "sha256": "b" * 64, "kind": "direction/vector"}}


@pytest.fixture
def api(monkeypatch, tmp_path):
    calls: list[tuple] = []

    def fake(self, method, route, *, query=None, body=None):
        calls.append((method, route, body))
        if route == "/live-runs" and method == "POST":
            return {"liveRun": {"id": "live_9", "runnerId": "rnr_1", "model": body["model"]}}, {}
        if route.endswith("/tries"):
            return ({**ANSWER, "names": NAMES} if body.get("as") else ANSWER), {}
        if route.endswith("/names"):
            return {"liveRunId": "live_9", "names": {**NAMES, "acts": {**NAMES["axis"], "seq": 1,
                                                                       "path": "~scratch/live_9/acts"}}}, {}
        return {"ok": True}, {}

    monkeypatch.setattr(Ctx, "api", fake)
    monkeypatch.setattr(Config, "from_env", classmethod(lambda cls: CFG))
    monkeypatch.setattr(live_mod.paths, "mechbench_dir", lambda: tmp_path)
    return calls


def test_start_holds_a_model_and_makes_it_current(api, capsys):
    assert cli.main(["live", "start", "--model", "google/gemma-3n-E2B-it", "--idle", "15m", "--label", "poke"]) == 0
    assert api[-1] == ("POST", "/live-runs", {"model": "google/gemma-3n-E2B-it", "idleSeconds": 900, "label": "poke"})
    assert live_mod.current() == "live_9"
    assert capsys.readouterr().out.splitlines()[0] == "live_9"


def test_try_runs_on_the_current_live_run_and_prints_its_lines(api, capsys):
    live_mod.set_current("live_9")
    assert cli.main(["try", "logits/read-layers", "--in", "records=me/lab/prompts",
                     "--in", 'tracked=["3"]', "--set", "top_k=3", "--set", "note=x"]) == 0
    method, route, body = api[-1]
    assert (method, route) == ("POST", "/live-runs/live_9/tries")
    assert body["op"] == "logits/read-layers"
    assert body["inputs"] == {"records": {"$ref": {"bench": "me/lab/prompts"}}, "tracked": ["3"]}
    assert body["params"] == {"top_k": 3, "note": "x"}
    assert body["clientId"] and body["wait"] == 120
    out = capsys.readouterr().out
    assert "8 logits/funnel items" in out and "inline" in out


def test_try_prints_the_whole_answer_as_json(api, capsys):
    live_mod.set_current("live_9")
    assert cli.main(["try", "logits/read-layers", "--json"]) == 0
    assert '"kind": "logits/funnel"' in capsys.readouterr().out


def test_close_ends_the_current_live_run_and_forgets_it(api):
    live_mod.set_current("live_9")
    assert invoke(Ctx(CFG), "live", "close", {})["closed"] == "live_9"
    assert api[-1][:2] == ("POST", "/live-runs/live_9/close")
    assert live_mod.current() is None


def test_with_no_live_run_named_or_current_a_try_says_how_to_start_one(api):
    with pytest.raises(VerbError, match="live start"):
        invoke(Ctx(CFG), "live", "try", {"op": "logits/read"})


def test_durations_read_as_seconds():
    assert [live_mod.seconds(x) for x in ("90", "15m", "2h", "1d")] == [90, 900, 7200, 86400]
    with pytest.raises(VerbError):
        live_mod.seconds("soon")


def test_the_verbs_effects():
    n = noun("live")
    assert {v.name: v.effect for v in n.verbs} == {
        "list": "read", "read": "read", "start": "draft", "try": "draft", "names": "read",
        "close": "delete"}
    assert noun("object").verb("copy").effect == "draft"


@pytest.mark.parametrize("argv", [
    ["let", "axis", "=", "direction/fit", "--in", "vectors=$acts"],
    ["let", "axis=direction/fit", "--in", "vectors=$acts"],
    ["let", "axis=", "direction/fit", "--in", "vectors=$acts"],
    ["let", "axis", "=direction/fit", "--in", "vectors=$acts"],
])
def test_let_binds_a_try_to_a_name_and_a_dollar_input_reads_one(api, capsys, argv):
    live_mod.set_current("live_9")
    assert cli.main(argv) == 0
    method, route, body = api[-1]
    assert (method, route) == ("POST", "/live-runs/live_9/tries")
    assert body["op"] == "direction/fit" and body["as"] == "axis"
    assert body["inputs"] == {"vectors": {"$name": "acts"}}
    assert "  as axis: ~scratch/live_9/axis" in capsys.readouterr().out.splitlines()


@pytest.mark.parametrize("argv", [["let"], ["let", "axis", "direction/fit"], ["let", "=", "x"],
                                  ["let", "axis", "=", "--in", "a=b"]])
def test_let_written_wrong_says_how_to_write_it(api, argv):
    with pytest.raises(SystemExit, match="mechbench let NAME = OPERATION"):
        cli.main(argv)


def test_try_takes_as_and_leaves_a_dollar_param_a_string(api):
    live_mod.set_current("live_9")
    assert cli.main(["try", "steer/apply", "--as", "steered", "--set", "note=$axis",
                     "--set", 'direction={"$name":"axis"}']) == 0
    body = api[-1][2]
    assert body["as"] == "steered"
    assert body["params"] == {"note": "$axis", "direction": {"$name": "axis"}}


def test_an_empty_input_says_to_quote_a_name(api, capsys):
    live_mod.set_current("live_9")
    assert cli.main(["try", "direction/fit", "--in", "vectors="]) == 2
    assert "quote it ('vectors=$NAME')" in capsys.readouterr().err


def test_names_lists_the_live_runs_bindings(api, capsys):
    live_mod.set_current("live_9")
    assert cli.main(["names"]) == 0
    assert api[-1][:2] == ("GET", "/live-runs/live_9/names")
    out = capsys.readouterr().out.splitlines()
    assert out[0].split() == ["name", "seq", "kind", "path"]
    assert [line.split()[:2] for line in out[1:]] == [["acts", "1"], ["axis", "3"]]


def test_names_answers_its_bindings_as_items(api):
    got = invoke(Ctx(CFG), "live", "names", {"live_run": "live_7"})
    assert api[-1][:2] == ("GET", "/live-runs/live_7/names")
    assert [i["name"] for i in got["items"]] == ["acts", "axis"] and got["next"] is None
    assert [i["name"] for i in invoke(Ctx(CFG), "live", "names", {"live_run": "live_7", "search": "AX"})["items"]] == ["axis"]
    assert invoke(Ctx(CFG), "live", "names", {"live_run": "live_7", "limit": 1}) == {
        "items": [{"name": "acts", **NAMES["axis"], "seq": 1, "path": "~scratch/live_9/acts"}], "next": 1}


def test_object_copy_puts_a_scratch_value_in_a_project(api):
    assert cli.main(["object", "copy", "~scratch/live_9/axis", "me/lab/axes/honesty"]) == 0
    assert api[-1] == ("POST", "/objects/~copy", {"from": "~scratch/live_9/axis", "to": "me/lab/axes/honesty"})
