from __future__ import annotations

import pytest

from mechbench import cli
from mechbench.cli import _render
from mechbench_runner import bench_cmd
from mechbench_runner.config import Config
from mechbench_runner.control import RunnerState
from mechbench_runner.verbs import Ctx, invoke
from mechbench_runner.verbs.sweep import sweep_body
from mechbench_runner.verbs_cli import rewrite_run

CFG = Config(api_base_url="http://api.test", api_key="mbk_test",
             poll_interval_seconds=0.01, warm_model_id=None, runner_id=None)

RUNNERS = [
    {"id": "rnr_home", "name": "studio", "hostname": "studio.local", "connected": True,
     "signedOut": False},
    {"id": "rnr_box", "name": "rented-box", "hostname": "box-7", "connected": False,
     "signedOut": False},
    {"id": "rnr_old", "name": "old laptop", "hostname": "air", "connected": False,
     "signedOut": True},
]


@pytest.fixture
def api(monkeypatch, tmp_path):
    calls: list[tuple] = []

    def fake(self, method, route, *, query=None, body=None):
        calls.append((method, route, body))
        if route == "/runners":
            return RUNNERS, {}
        return {"id": "run_1", "jobId": "j_1"}, {}

    monkeypatch.setattr(Ctx, "api", fake)
    monkeypatch.setattr(Config, "from_env", classmethod(lambda cls: CFG))
    monkeypatch.setattr(bench_cmd, "HISTORY", tmp_path / "runs.jsonl")
    monkeypatch.setattr(bench_cmd, "_connect", lambda _c: None)
    return calls


def test_a_launch_names_its_runner_in_the_body(api):
    invoke(Ctx(CFG), "run", "launch", {"protocol": "prt_1", "params": {"n": 2},
                                       "runner": "rnr_box", "label": "on the box"})
    assert api[-1] == ("POST", "/protocols/prt_1/runs",
                       {"params": {"n": 2}, "label": "on the box", "runner": "rnr_box"})


def test_mechbench_run_protocol_takes_runner_and_remembers_it(api, capsys):
    assert cli.main(["run", "prt_1", "--param", "n=2", "--runner", "rented-box"]) == 0
    assert api[-1][2]["runner"] == "rented-box"
    assert "on runner rented-box" in capsys.readouterr().err
    assert '"runner": "rented-box"' in bench_cmd.HISTORY.read_text()


def test_a_bare_run_with_runner_is_refused(api, capsys):
    assert cli.main(["run", "--runner", "rnr_box"]) == 2
    assert "--runner" in capsys.readouterr().err


def test_a_sweep_carries_the_runner_from_the_flag_or_its_file(tmp_path):
    body = sweep_body({"grid": {"n": "1,2"}, "runner": "rnr_box"})
    assert body["runner"] == "rnr_box"
    f = tmp_path / "s.json"
    f.write_text('{"members": [{"params": {"n": 1}}], "runner": "studio"}')
    assert sweep_body({"file": str(f)})["runner"] == "studio"


def test_runners_lists_the_ids_to_pin_leaving_signed_out_ones_out(api, capsys):
    assert rewrite_run(["runners", "--search", "box"]) == ["runner", "list", "--search", "box"]
    out = invoke(Ctx(CFG), "runner", "list", {})
    assert [r["id"] for r in out["items"]] == ["rnr_home", "rnr_box"]
    every = invoke(Ctx(CFG), "runner", "list", {"signed_out": True})
    assert len(every["items"]) == 3
    assert [r["id"] for r in invoke(Ctx(CFG), "runner", "list",
                                    {"search": "BOX"})["items"]] == ["rnr_box"]
    assert cli.main(["runners"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0].split()[:2] == ["id", "name"] and lines[1].startswith("rnr_home")


def test_status_shows_the_id_a_run_pins():
    state = RunnerState(version="0.51.0", api_url="http://x", runner_id="rnr_home",
                        runner_name="studio")
    snap = state.snapshot()
    assert snap["runner_id"] == "rnr_home" and snap["runner_name"] == "studio"
    assert "id       rnr_home (studio)  pin a run here: --runner rnr_home" in _render(snap)
    assert "pin a run" not in _render(RunnerState(version="x", api_url="y").snapshot())
