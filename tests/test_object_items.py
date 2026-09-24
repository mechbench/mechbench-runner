from __future__ import annotations

import json

import pytest

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

PAGE = {
    "path": "benji/lab/r/gen",
    "total": 200,
    "matched": 100,
    "offset": 0,
    "limit": 2,
    "items": [
        {"id": "flash-s0", "text": "**The Last Call**"},
        {"id": "flash-s1", "text": "The Keeper"},
    ],
}


@pytest.fixture
def rec(monkeypatch):
    calls: list[dict] = []

    def api(self, method, route, *, query=None, body=None):
        calls.append({k: v for k, v in (query or {}).items() if v is not None})
        return PAGE, {}

    monkeypatch.setattr(Ctx, "api", api)
    monkeypatch.setattr(Config, "from_env", classmethod(lambda cls: CFG))
    return calls


ARGV = [
    "object", "items", "benji/lab/r/gen", "--fields", "id,text",
    "--where", "coords.prompt=flash", "--where", "metadata.tokens>100",
    "--lines", "1", "--limit", "2",
]


def test_both_surfaces_ask_the_same(rec, capsys):
    tools = build_tools(CFG, executor=object())
    out = tools["object"]("items", {
        "path": "benji/lab/r/gen", "fields": ["id", "text"],
        "where": ["coords.prompt=flash", "metadata.tokens>100"],
        "lines": 1, "limit": 2,
    })
    assert out == PAGE
    assert cli.main(ARGV) == 0
    assert rec[0] == rec[1] == {
        "path": "benji/lab/r/gen", "fields": "id,text",
        "where": ["coords.prompt=flash", "metadata.tokens>100"],
        "lines": 1, "limit": 2,
    }
    tools["object"]("items", {"path": "p", "where": "coords.prompt=flash"})
    assert rec[-1]["where"] == ["coords.prompt=flash"]


def test_the_command_line_prints_json_lines(rec, capsys):
    assert cli.main(ARGV) == 0
    out, err = capsys.readouterr()
    assert [json.loads(line) for line in out.splitlines()] == PAGE["items"]
    assert "2 of 100 matched, 200 in all; more: --offset 2" in err


def test_or_a_table(rec, capsys):
    assert cli.main([*ARGV, "--table"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0].split() == ["id", "text"]
    assert out[1].startswith("flash-s0") and out[1].endswith("**The Last Call**")


def test_a_count_prints_as_json(rec, monkeypatch, capsys):
    monkeypatch.setattr(
        Ctx, "api",
        lambda self, m, r, **k: ({"path": "p", "total": 200, "matched": 100}, {}),
    )
    assert cli.main(["object", "items", "p", "--count"]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "path": "p", "total": 200, "matched": 100,
    }
