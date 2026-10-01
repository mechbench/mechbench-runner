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


@pytest.fixture
def api(monkeypatch, tmp_path):
    calls: list[tuple] = []

    def fake(self, method, route, *, query=None, body=None):
        calls.append((method, route, body))
        if route == "/live-runs" and method == "POST":
            return {"liveRun": {"id": "live_9", "runnerId": "rnr_1", "model": body["model"]}}, {}
        if route.endswith("/tries"):
            return ANSWER, {}
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
        "list": "read", "read": "read", "start": "draft", "try": "draft", "close": "delete"}
