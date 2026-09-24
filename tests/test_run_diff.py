from __future__ import annotations

import json

import pytest
from mechbench_compute import bench

from mechbench import cli
from mechbench_runner.config import Config
from mechbench_runner.mcp_server import build_tools
from mechbench_runner.verbs import Ctx

CFG = Config(
    api_base_url="http://api.test",
    api_key="mbk_test",
    poll_interval_seconds=0.01,
    warm_model_id=None,
    runner_id=None,
)


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


LONG = "word " * 100
STORED = {
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


def mcp(args):
    return build_tools(CFG, executor=object())["run"]("diff", args)


RULE = [
    {
        "field": "text",
        "relation": "extends",
        "when": {"metadata.sampling.ended": "max_tokens"},
    }
]


def test_the_regeneration_holds_on_both_surfaces(platform, capsys):
    out = mcp(
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
    out = mcp({"a": "j_old", "b": "j_new", "node": "gen", "exclude": "text,ended"})
    assert out["equivalent"] is True
    assert out["excluded_differ"]["metadata.call.latency_ms"] == 1
    out = mcp(
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
    out = mcp({"a": "benji/lab/results/j_old/gen", "b": "j_new", "node_b": "stats"})
    assert out["records"]["only_a"] == 1 and out["records"]["differs"] == 1
    assert out["b"]["node"] == "stats"


def test_long_values_are_abbreviated_unless_full_and_limit_counts(platform):
    out = mcp(
        {
            "a": "j_old",
            "b": "j_new",
            "node": "gen",
            "fields": "text,metadata",
            "limit": 0,
        }
    )
    assert out["shown"] == [] and out["not_shown"] == 1
    out = mcp({"a": "j_old", "b": "j_new", "node": "gen", "full": True})
    assert out["shown"][0]["fields"]["text"]["b"] == LONG + "and the end."


def test_a_run_needs_its_node_and_a_result(platform):
    with pytest.raises(ValueError, match="name its node"):
        mcp({"a": "j_old", "b": "j_new"})
    with pytest.raises(ValueError, match="queued"):
        mcp({"a": "j_queued", "b": "j_new", "node": "gen"})
