from __future__ import annotations

import json

import pytest
from mechbench_compute import bench

from mechbench import cli
from mechbench_runner.config import Config
from mechbench_runner.verbs import Ctx, VerbError, invoke

CFG = Config(
    api_base_url="http://api.test",
    api_key="mbk_test",
    poll_interval_seconds=0.01,
    warm_model_id=None,
    runner_id=None,
)


def call(noun, verb, args):
    return invoke(Ctx(CFG), noun, verb, args)


def story(prompt, sample, text, ended=None, latency=10):
    sampling = {"seed": 7}
    if ended:
        sampling["ended"] = ended
    return {
        "id": f"{prompt}-s{sample}",
        "text": text,
        "coords": {"prompt": prompt, "sample": sample},
        "metadata": {"sampling": sampling, "call": {"latency_ms": latency}},
    }


def measured(sample, logprob):
    return {
        "id": f"p-s{sample}",
        "coords": {"prompt": "p", "sample": sample},
        "logprob": logprob,
    }


def floor(operation, spread):
    return {
        "id": f"gemma4/{operation}/logprob",
        "architecture": "gemma4",
        "operation": operation,
        "field": "logprob",
        "dtype": "bfloat16",
        "machine_class": "apple-silicon",
        "spread": spread,
        "relative_spread": 0.0,
        "n": 4,
    }


NOISE = "benji/calibration/noise"
LONG = "word " * 100
STORED = {
    "benji/lab/results/j_home/gen": [measured(0, -1.0), measured(1, -2.0)],
    "benji/lab/results/j_box/gen": [measured(0, -1.0004), measured(1, -2.0)],
    NOISE: [floor("text/generate", 0.001), floor("logits/read", 0.00001)],
    "benji/lab/results/j_old/gen": [
        story("flash", 0, "done.", "end"),
        story("flash", 1, LONG, "max_tokens"),
    ],
    "benji/lab/results/j_new/gen": [
        story("flash", 0, "done.", "end", latency=12),
        story("flash", 1, LONG + "and the end.", "end"),
    ],
    "benji/lab/results/j_new/stats": [{"id": "flash-s0", "n": 3}],
}


@pytest.fixture
def platform(monkeypatch):
    reads: list[str] = []

    def api(self, method, route, *, query=None, body=None):
        job = route.rsplit("/", 1)[-1]
        if job == "j_queued":
            return {"jobId": job, "jobStatus": "queued"}, {}
        return {
            "jobId": job,
            "jobStatus": "done",
            "resultPath": f"benji/lab/results/{job}",
        }, {}

    def fetch_envelope(target, **_k):
        reads.append(target)
        return {
            "payload": {"kind": "collection", "items": STORED[target]},
            "provenance": {
                "produced_by": {"version": target[-12:]},
                "created_at": target,
            },
        }

    monkeypatch.setattr(Ctx, "api", api)
    monkeypatch.setattr(bench, "configure", lambda **_k: None)
    monkeypatch.setattr(bench, "fetch_envelope", fetch_envelope)
    monkeypatch.setattr(Config, "from_env", classmethod(lambda cls: CFG))
    return reads


def diff_of(args):
    return call("run", "diff", args)


RULE = [
    {
        "field": "text",
        "relation": "extends",
        "when": {"metadata.sampling.ended": "max_tokens"},
    }
]


def test_the_regeneration_holds_on_both_surfaces(platform, capsys):
    out = diff_of(
        {
            "a": "j_old",
            "b": "j_new",
            "node": "gen",
            "key": "prompt,sample",
            "fields": ["text"],
            "allow": RULE,
        }
    )
    assert out["holds"] is True
    assert out["records"]["identical"] == 1 and out["records"]["extends"] == 1
    assert out["a"]["path"] == "benji/lab/results/j_old/gen"
    assert out["a"]["compute"] == "benji/lab/results/j_old/gen"[-12:]
    shown = out["shown"][0]["fields"]["text"]
    assert shown["added"] == "and the end." and shown["a_chars"] == len(LONG)

    assert (
        cli.main(
            [
                "run",
                "diff",
                "j_old",
                "j_new",
                "--node",
                "gen",
                "--key",
                "prompt,sample",
                "--fields",
                "text",
                "--allow",
                json.dumps(RULE),
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out) == out
    assert (
        platform == ["benji/lab/results/j_old/gen", "benji/lab/results/j_new/gen"] * 2
    )


def test_moving_fields_are_left_out_unless_asked(platform):
    out = diff_of({"a": "j_old", "b": "j_new", "node": "gen", "exclude": "text,ended"})
    assert out["equivalent"] is True
    assert out["excluded_differ"]["metadata.call.latency_ms"] == 1
    out = diff_of(
        {
            "a": "j_old",
            "b": "j_new",
            "node": "gen",
            "exclude": "text,ended",
            "include_moving": True,
        }
    )
    assert "provenance.created_at" in out["provenance"]


def test_object_paths_and_a_second_node(platform):
    out = diff_of({"a": "benji/lab/results/j_old/gen", "b": "j_new", "node_b": "stats"})
    assert out["records"]["only_a"] == 1 and out["records"]["differs"] == 1
    assert out["b"]["node"] == "stats"


def test_long_values_are_abbreviated_unless_full_and_limit_counts(platform):
    out = diff_of(
        {
            "a": "j_old",
            "b": "j_new",
            "node": "gen",
            "fields": "text,metadata",
            "limit": 0,
        }
    )
    assert out["shown"] == [] and out["not_shown"] == 1
    out = diff_of({"a": "j_old", "b": "j_new", "node": "gen", "full": True})
    assert out["shown"][0]["fields"]["text"]["b"] == LONG + "and the end."


def test_a_run_needs_its_node_and_a_result(platform):
    with pytest.raises(VerbError, match="name its node"):
        diff_of({"a": "j_old", "b": "j_new"})
    with pytest.raises(VerbError, match="queued"):
        diff_of({"a": "j_queued", "b": "j_new", "node": "gen"})


HOME_BOX = {"a": "j_home", "b": "j_box", "node": "gen", "key": "prompt,sample"}


def test_a_floor_counts_each_difference_in_floors(platform, capsys):
    out = diff_of({**HOME_BOX, "noise": NOISE})
    assert out["verdict"] == "within the floor" and out["findings"] == 0
    assert out["shown"][0]["fields"]["logprob"]["floors"] == 0.4
    assert NOISE in platform

    exact = diff_of({**HOME_BOX, "noise": NOISE, "tolerance": "0"})
    assert exact["verdict"] == "1 finding above the tolerance"
    narrow = diff_of(
        {**HOME_BOX, "noise": NOISE, "noise_for": {"operation": "logits/read"}}
    )
    assert narrow["verdict"] == "1 finding above the floor"
    assert diff_of({**HOME_BOX, "noise": NOISE, "k": 0.25})["findings"] == 1

    args = ["run", "diff", "j_home", "j_box", "--node", "gen", "--key", "prompt,sample"]
    assert (
        cli.main([*args, "--noise", NOISE, "--noise-for", "operation=logits/read"]) == 0
    )
    assert json.loads(capsys.readouterr().out) == narrow
    assert cli.main([*args, "--tolerance", '{"abs": 0.001}']) == 0
    assert json.loads(capsys.readouterr().out)["verdict"] == "within the tolerance"


@pytest.fixture
def kept(monkeypatch, platform):
    from mechbench_compute.ops.records.diff import diff_collections

    from mechbench_runner.verbs import run_diff as module

    result = "benji/lab/results/j_diff/diff"
    sides = {
        "a": {
            "run": "j_home",
            "node": "gen",
            "path": "benji/lab/results/j_home/gen",
            "hash": "1",
        },
        "b": {
            "run": "j_box",
            "node": "gen",
            "path": "benji/lab/results/j_box/gen",
            "hash": "2",
        },
    }
    job = {
        "id": "j_diff",
        "spec": {"diff": {**sides, "noise": {"path": NOISE}, "result": result}},
    }
    state = {"posted": None, "statuses": ["queued", "running", "done"], "error": None}

    def side(path):
        return {"kind": "collection", "items": STORED[path]}

    stored = diff_collections(
        side(sides["a"]["path"]),
        side(sides["b"]["path"]),
        {"key": ["prompt", "sample"]},
        noise=side(NOISE),
    )
    run_row = Ctx.api

    def api(self, method, route, *, query=None, body=None):
        if route == "/runs/diff":
            state["posted"] = body
            return {**job, "status": state["statuses"][0], "result": result}, {}
        if route == "/jobs/j_diff":
            status = (
                state["statuses"].pop(0)
                if len(state["statuses"]) > 1
                else state["statuses"][0]
            )
            return {**job, "status": status, "errorMessage": state["error"]}, {}
        return run_row(self, method, route, query=query, body=body)

    fetch = bench.fetch_envelope

    def fetch_envelope(target, **k):
        if target == result:
            return {
                "payload": stored,
                "provenance": {"produced_by": {"version": "0.187.0"}},
            }
        return fetch(target, **k)

    monkeypatch.setattr(Ctx, "api", api)
    monkeypatch.setattr(bench, "fetch_envelope", fetch_envelope)
    monkeypatch.setattr(module.time, "sleep", lambda _s: None)
    return state


def test_into_keeps_it_as_a_job_and_answers_as_the_command_line_does(kept, platform):
    out = diff_of({**HOME_BOX, "noise": NOISE, "into": "benji/lab"})
    assert kept["posted"] == {
        "a": "j_home",
        "b": "j_box",
        "params": {"key": ["prompt", "sample"], "exclude_moving": True},
        "node": "gen",
        "noise": NOISE,
        "into": "benji/lab",
    }
    assert out["job"] == "j_diff" and out["result"] == "benji/lab/results/j_diff/diff"
    assert out["verdict"] == "within the floor"
    assert out["a"] == {
        "run": "j_home",
        "node": "gen",
        "path": "benji/lab/results/j_home/gen",
        "records": 2,
    }
    assert out["shown"][0]["fields"]["logprob"]["floors"] == 0.4
    assert "benji/lab/results/j_home/gen" not in platform


def test_a_kept_diff_still_waiting_says_where_it_will_be(kept, monkeypatch):
    from mechbench_runner.verbs import run_diff as module

    monkeypatch.setattr(module, "WAIT_SECONDS", 0.0)
    kept["statuses"] = ["queued"]
    out = diff_of({**HOME_BOX, "into": "benji/lab"})
    assert out["finished"] is False and out["status"] == "queued"
    assert out["job"] == "j_diff" and out["result"] == "benji/lab/results/j_diff/diff"


def test_a_kept_diff_that_failed_says_why(kept):
    kept["statuses"] = ["queued", "failed"]
    kept["error"] = "pinned object resolved to another hash"
    with pytest.raises(VerbError, match="j_diff failed: pinned object"):
        diff_of({**HOME_BOX, "into": "benji/lab"})
