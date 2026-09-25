from __future__ import annotations

import json
from typing import Any

import pytest

from mechbench import cli
from mechbench_runner.api_client import ApiError
from mechbench_runner.config import Config
from mechbench_runner.mcp_server import build_tools
from mechbench_runner.verbs import Ctx
from mechbench_runner.verbs import sweep as sweep_mod

CFG = Config(
    api_base_url="http://api.test",
    api_key="mbk_test",
    poll_interval_seconds=0.01,
    warm_model_id=None,
    runner_id=None,
)

LAUNCHED = [
    {"id": "run_a", "jobId": "j_a", "sweepId": "swp_1", "sweepMember": 0},
    {"id": "run_b", "jobId": "j_b", "sweepId": "swp_1", "sweepMember": 1},
]


@pytest.fixture
def api(monkeypatch):
    calls: list[tuple[str, str, dict, Any]] = []
    listings: list[list[dict]] = []

    def fake(self, method, route, *, query=None, body=None):
        q = {k: v for k, v in (query or {}).items() if v is not None}
        calls.append((method, route, q, body))
        if method == "POST" and route.endswith("/sweeps"):
            return [dict(r) for r in LAUNCHED], {}
        if method == "GET" and route == "/runs" and listings:
            return listings.pop(0), {}
        return [], {}

    monkeypatch.setattr(Ctx, "api", fake)
    monkeypatch.setattr(Config, "from_env", classmethod(lambda cls: CFG))
    monkeypatch.setattr(sweep_mod.time, "sleep", lambda _s: None)
    return calls, listings


def posted(calls) -> dict:
    (body,) = [c[3] for c in calls if c[0] == "POST"]
    return body


def test_a_grid_on_the_command_line_parses_its_values_and_its_inputs_become_refs(
    api, capsys
):
    calls, _ = api
    argv = [
        "run", "sweep", "prt_1",
        "--grid", "n=5,50,500",
        "--grid", 'temp=[0.5, 1.0]',
        "--grid", "mode=fast,slow",
        "--grid-input", "prompts=me/lab/a,me/lab/b",
        "--param", "model=google/gemma-3-4b-it",
        "--input", "judge=me/lab/rubric",
        "--label", "n ladder",
        "--budget", "2",
    ]  # fmt: skip
    assert cli.main(argv) == 0
    body = posted(calls)
    assert calls[0][1] == "/protocols/prt_1/sweeps"
    assert body == {
        "params": {"model": "google/gemma-3-4b-it"},
        "inputs": {"judge": {"$ref": {"bench": "me/lab/rubric"}}},
        "grid": {
            "params": {"n": [5, 50, 500], "temp": [0.5, 1.0], "mode": ["fast", "slow"]},
            "inputs": {
                "prompts": [
                    {"$ref": {"bench": "me/lab/a"}},
                    {"$ref": {"bench": "me/lab/b"}},
                ]
            },
        },
        "label": "n ladder",
        "budgetUsd": 2.0,
    }
    out = json.loads(capsys.readouterr().out)
    assert out["sweepId"] == "swp_1"
    assert [r["jobId"] for r in out["runs"]] == ["j_a", "j_b"]


def test_members_from_a_yaml_file_keep_their_order(api, tmp_path):
    calls, _ = api
    f = tmp_path / "members.yaml"
    f.write_text(
        "- params: {n: 5}\n- params: {n: 1}\n  inputs: {prompts: me/lab/b}\n- {}\n"
    )
    assert cli.main(["run", "sweep", "prt_1", "--file", str(f)]) == 0
    assert posted(calls)["members"] == [
        {"params": {"n": 5}},
        {"params": {"n": 1}, "inputs": {"prompts": {"$ref": {"bench": "me/lab/b"}}}},
        {},
    ]


def test_a_whole_sweep_in_a_json_file_yields_to_flags(api, tmp_path):
    calls, _ = api
    f = tmp_path / "sweep.json"
    f.write_text(
        json.dumps(
            {
                "label": "from the file",
                "params": {"model": "m", "n": 1},
                "grid": {"params": {"temp": [0, 1]}},
            }
        )
    )
    argv = ["run", "sweep", "prt_1", "--file", str(f), "--param", "n=2"]
    assert cli.main(argv) == 0
    assert posted(calls) == {
        "params": {"model": "m", "n": 2},
        "grid": {"params": {"temp": [0, 1]}},
        "label": "from the file",
    }


def test_mcp_takes_members_and_a_grid_as_values():
    calls: list[Any] = []
    tools = build_tools(CFG, executor=object())
    ctx_api = Ctx.api

    def fake(self, method, route, *, query=None, body=None):
        calls.append(body)
        return [dict(r) for r in LAUNCHED], {}

    Ctx.api = fake
    try:
        tools["run"]("sweep", {"protocol": "prt_1", "members": [{"params": {"n": 1}}]})
        tools["run"]("sweep", {"protocol": "prt_1", "grid": {"n": [1, 2], "t": "3,4"}})
    finally:
        Ctx.api = ctx_api
    assert calls[0] == {"members": [{"params": {"n": 1}}]}
    assert calls[1] == {"grid": {"params": {"n": [1, 2], "t": [3, 4]}}}


@pytest.mark.parametrize(
    "args,why",
    [
        ({}, "neither"),
        ({"members": [{}], "grid": {"n": [1]}}, "both"),
        ({"members": [{"n": 1}]}, "a member is"),
    ],
)
def test_a_sweep_that_is_not_one_is_refused_before_anything_is_sent(api, args, why):
    calls, _ = api
    tools = build_tools(CFG, executor=object())
    with pytest.raises(ValueError, match=why):
        tools["run"]("sweep", {"protocol": "prt_1", **args})
    assert calls == []


def test_a_file_with_a_field_a_sweep_lacks_is_refused(api, tmp_path):
    f = tmp_path / "sweep.json"
    f.write_text('{"members": [{}], "repeat": 3}')
    tools = build_tools(CFG, executor=object())
    with pytest.raises(ValueError, match="not repeat"):
        tools["run"]("sweep", {"protocol": "prt_1", "file": str(f)})


def test_wait_polls_the_sweep_until_every_run_has_finished(api):
    calls, listings = api
    listings += [
        [{"id": "run_a", "jobStatus": "done"}, {"id": "run_b", "jobStatus": "running"}],
        [{"id": "run_a", "jobStatus": "done"}, {"id": "run_b", "jobStatus": "failed"}],
    ]
    tools = build_tools(CFG, executor=object())
    out = tools["run"](
        "sweep", {"protocol": "prt_1", "grid": {"n": [1, 2]}, "wait": True}
    )
    assert out["finished"] is True
    assert [r["jobStatus"] for r in out["runs"]] == ["done", "failed"]
    polls = [c for c in calls if c[1] == "/runs"]
    assert len(polls) == 2
    assert polls[0][2] == {"sweep": "swp_1", "limit": 500, "view": "summary"}


def test_wait_gives_up_at_the_timeout_and_says_so(api):
    _, listings = api
    listings += [[{"id": "run_a", "jobStatus": "running"}]]
    tools = build_tools(CFG, executor=object())
    out = tools["run"](
        "sweep",
        {"protocol": "prt_1", "grid": {"n": [1]}, "wait": True, "timeout": 0.0001},
    )
    assert out["finished"] is False


def test_runs_and_jobs_are_found_by_sweep(api):
    calls, _ = api
    tools = build_tools(CFG, executor=object())
    tools["run"]("list", {"sweep": "swp_1"})
    tools["run"]("jobs", {"sweep": "swp_1"})
    assert calls[0][1:3] == ("/runs", {"sweep": "swp_1", "view": "summary"})
    assert calls[1][1:3] == ("/jobs", {"sweep": "swp_1"})


def test_a_refused_member_is_named_on_the_command_line(monkeypatch, capsys):
    def refuse(self, method, route, **_k):
        raise ApiError(
            400,
            {
                "code": "BAD_MEMBER",
                "error": 'member 1 ({"n":"x"}): param \'n\' is not an int',
            },
        )

    monkeypatch.setattr(Ctx, "api", refuse)
    monkeypatch.setattr(Config, "from_env", classmethod(lambda cls: CFG))
    assert cli.main(["run", "sweep", "prt_1", "--grid", "n=1,x"]) == 1
    err = capsys.readouterr().err
    assert "refused (BAD_MEMBER)" in err and "member 1" in err
