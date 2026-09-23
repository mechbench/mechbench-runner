"""Protocols as files and runs by label, on the command line and over MCP.

Both surfaces are thin over `mechbench_compute.bench`, which is faked
here, held to the real verbs' signatures: what is tested is that each
surface passes the same arguments to the same verb, and renders the
server's answer (an action, a refusal with its findings, a row).
"""

from __future__ import annotations

import inspect
import json

import pytest
from mechbench_compute import bench
from mechbench_compute.bench import BenchError

from mechbench import cli
from mechbench_runner import bench_cmd as b
from mechbench_runner.config import Config
from mechbench_runner.mcp_server import build_tools

CFG = Config(
    api_base_url="http://api.test",
    api_key="mbk_test",
    poll_interval_seconds=0.01,
    warm_model_id=None,
    runner_id=None,
)


@pytest.fixture
def fake(monkeypatch, tmp_path):
    """Swap bench verbs for fakes checked against the real signatures, and
    record every call as (verb, args, kwargs)."""
    calls: list[tuple[str, tuple, dict]] = []
    monkeypatch.setattr(b, "HISTORY", tmp_path / "runs.jsonl")
    monkeypatch.setattr(bench, "configure", lambda **_k: None)

    def install(**fns):
        for name, fn in fns.items():
            sig = inspect.signature(getattr(bench, name))

            def checked(*args, _fn=fn, _sig=sig, _name=name, **kwargs):
                _sig.bind(*args, **kwargs)
                calls.append((_name, args, kwargs))
                return _fn(*args, **kwargs)

            monkeypatch.setattr(bench, name, checked)

    install.calls = calls  # type: ignore[attr-defined]
    return install


def _cli(argv: list[str], monkeypatch) -> int:
    monkeypatch.setattr(Config, "from_env", classmethod(lambda cls: CFG))
    return cli.main(argv)


PUSHED = {
    "action": "versioned",
    "protocol": {"id": "prt_1", "name": "draws", "version": 3},
    "findings": [],
}


class TestPush:
    def test_the_cli_names_the_file_and_the_project_and_says_what_happened(
        self, fake, monkeypatch, capsys, tmp_path
    ):
        f = tmp_path / "draws.json"
        f.write_text("{}")
        fake(push_protocol=lambda file, into, **k: PUSHED)
        assert (
            _cli(["protocol", "push", str(f), "--into", "benji/lab"], monkeypatch) == 0
        )
        assert capsys.readouterr().out.strip() == "versioned benji/lab/draws prt_1 v3"
        name, args, kwargs = fake.calls[-1]
        assert (name, args, kwargs) == (
            "push_protocol",
            (str(f), "benji/lab"),
            {"owner_kind": "user"},
        )

    def test_unchanged_is_said_and_is_success(self, fake, monkeypatch, capsys):
        fake(push_protocol=lambda file, into, **k: {**PUSHED, "action": "unchanged"})
        assert (
            _cli(
                ["protocol", "push", "x.json", "--into", "acme/lab", "--org"],
                monkeypatch,
            )
            == 0
        )
        assert capsys.readouterr().out.startswith("unchanged ")
        assert fake.calls[-1][2] == {"owner_kind": "org"}

    def test_a_refusal_prints_its_code_and_findings_and_exits_1(
        self, fake, monkeypatch, capsys
    ):
        def refuse(file, into, **k):
            raise BenchError(
                "400",
                status=400,
                body={
                    "code": "WIRING",
                    "error": "1 wiring error: text/generate reads no param 'x'",
                    "findings": [
                        {
                            "severity": "error",
                            "code": "UNKNOWN_PARAM",
                            "node": "gen",
                            "message": "text/generate reads no param 'x'",
                        }
                    ],
                },
            )

        fake(push_protocol=refuse)
        assert (
            _cli(["protocol", "push", "x.json", "--into", "benji/lab"], monkeypatch)
            == 1
        )
        err = capsys.readouterr().err
        assert "push refused (WIRING)" in err
        assert "error UNKNOWN_PARAM [gen]: text/generate reads no param 'x'" in err

    def test_the_legacy_form_is_refused_by_name(self, fake, monkeypatch, capsys):
        def refuse(file, into, **k):
            raise BenchError(
                "400",
                status=400,
                body={
                    "code": "LEGACY_DATAFLOW",
                    "error": "this protocol is in the legacy dataflow form",
                },
            )

        fake(push_protocol=refuse)
        assert (
            _cli(["protocol", "push", "x.json", "--into", "benji/lab"], monkeypatch)
            == 1
        )
        assert "push refused (LEGACY_DATAFLOW)" in capsys.readouterr().err

    def test_mcp_pushes_the_same_way_and_answers_a_refusal_as_data(self, fake):
        tools = build_tools(CFG, executor=object())
        fake(push_protocol=lambda file, into, **k: PUSHED)
        push = {"file": "x.json", "into": "benji/lab"}
        assert tools["protocol"]("push", push)["action"] == "versioned"
        assert fake.calls[-1] == (
            "push_protocol",
            ("x.json", "benji/lab"),
            {"owner_kind": "user"},
        )

        def refuse(file, into, **k):
            raise BenchError(
                "400",
                status=400,
                body={"code": "WIRING", "findings": [{"code": "UNKNOWN_PARAM"}]},
            )

        fake(push_protocol=refuse)
        out = tools["protocol"]("push", push)
        assert out["action"] == "refused" and out["code"] == "WIRING"


class TestExport:
    TEXT = '{\n  "name": "draws"\n}\n'

    def test_the_cli_writes_the_text_to_stdout_exactly(self, fake, monkeypatch, capsys):
        fake(
            export_protocol=lambda protocol, **k: {
                "name": "draws",
                "version": 2,
                "text": self.TEXT,
            }
        )
        assert _cli(["protocol", "export", "prt_1", "--version", "2"], monkeypatch) == 0
        assert capsys.readouterr().out == self.TEXT
        assert fake.calls[-1] == (
            "export_protocol",
            ("prt_1",),
            {"version": 2, "path": None},
        )

    def test_the_cli_writes_a_file_with_o(self, fake, monkeypatch, capsys, tmp_path):
        f = tmp_path / "draws.json"

        def export(protocol, *, version=None, path=None, **_k):
            if path:
                f.write_text(self.TEXT)
            return {"name": "draws", "version": 5, "text": self.TEXT}

        fake(export_protocol=export)
        assert _cli(["protocol", "export", "prt_1", "-o", str(f)], monkeypatch) == 0
        assert f.read_text() == self.TEXT
        assert "draws v5" in capsys.readouterr().err

    def test_mcp_exports_with_the_same_arguments(self, fake):
        tools = build_tools(CFG, executor=object())
        fake(export_protocol=lambda protocol, **k: {"text": self.TEXT})
        assert tools["protocol"]("export", {"id": "prt_1", "version": 2})["text"] == self.TEXT
        assert fake.calls[-1] == (
            "export_protocol",
            ("prt_1",),
            {"version": 2, "path": None},
        )


ROWS = [
    {
        "id": "run_2",
        "jobId": "j_2",
        "jobStatus": "done",
        "protocolName": "ladder",
        "protocolVersion": 4,
        "computeVersion": "0.132.0",
        "spentUsd": 0.4213,
        "createdAt": "2026-09-23T10:00:00.000Z",
        "label": "P0 reasoning on",
    },
    {
        "id": "run_1",
        "jobId": "j_1",
        "jobStatus": "failed",
        "protocolName": "ladder",
        "protocolVersion": 3,
        "computeVersion": None,
        "spentUsd": None,
        "createdAt": "2026-09-22T10:00:00.000Z",
        "label": None,
    },
]


class TestLabels:
    def test_run_sets_the_label_at_launch(self, fake, monkeypatch, capsys):
        fake(
            launch=lambda protocol, **k: {
                "id": "run_9",
                "jobId": "j_9",
                "label": k["label"],
            }
        )
        assert (
            _cli(
                ["run", "prt_1", "--param", "n=3", "--label", "P0, reasoning on"],
                monkeypatch,
            )
            == 0
        )
        assert capsys.readouterr().out.splitlines()[0] == "j_9"
        assert fake.calls[-1][2]["label"] == "P0, reasoning on"

    def test_a_label_without_a_protocol_is_a_typo_not_the_runner_loop(
        self, monkeypatch, capsys
    ):
        assert _cli(["run", "--label", "x"], monkeypatch) == 2

    def test_runs_filters_and_prints_one_line_each(self, fake, monkeypatch, capsys):
        fake(runs=lambda **k: ROWS)
        assert (
            _cli(
                ["runs", "--label-contains", "P0", "--project", "benji/lab"],
                monkeypatch,
            )
            == 0
        )
        out = capsys.readouterr().out.splitlines()
        assert out[0].split()[:5] == ["j_2", "done", "ladder", "v4", "0.132.0"]
        assert out[0].endswith("P0 reasoning on")
        assert out[1].split()[:4] == ["j_1", "failed", "ladder", "v3"]
        assert fake.calls[-1][2] == {
            "label": None,
            "label_contains": "P0",
            "protocol": None,
            "project": "benji/lab",
            "owner": None,
            "limit": None,
        }

    def test_runs_json_prints_the_rows(self, fake, monkeypatch, capsys):
        fake(runs=lambda **k: ROWS)
        assert _cli(["runs", "--label", "P0 reasoning on", "--json"], monkeypatch) == 0
        assert json.loads(capsys.readouterr().out) == ROWS

    def test_label_relabels_and_clears(self, fake, monkeypatch, capsys):
        fake(label_run=lambda run, label, **k: {"label": label, "changed": True})
        assert _cli(["label", "j_1", "P0 reasoning on (rerun)"], monkeypatch) == 0
        assert fake.calls[-1][1] == ("j_1", "P0 reasoning on (rerun)")
        assert _cli(["label", "j_1", "--clear"], monkeypatch) == 0
        assert fake.calls[-1][1] == ("j_1", None)
        assert "unlabelled" in capsys.readouterr().out

    def test_mcp_has_the_same_three_verbs(self, fake, monkeypatch):
        tools = build_tools(CFG, executor=object())
        fake(
            launch=lambda protocol, **k: {"id": "run_9", "jobId": "j_9"},
            label_run=lambda run, label, **k: {"changed": False},
        )
        tools["run"]("launch", {"protocol": "prt_1", "params": {"n": 3}, "label": "P0"})
        assert fake.calls[-1] == (
            "launch",
            ("prt_1",),
            {
                "params": {"n": 3},
                "inputs": None,
                "keep": None,
                "budget": None,
                "label": "P0",
            },
        )
        tools["run"]("update", {"id": "j_1", "clear": True})
        assert fake.calls[-1][1] == ("j_1", None)
