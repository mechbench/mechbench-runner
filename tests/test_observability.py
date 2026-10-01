from __future__ import annotations

import json

import pytest

from mechbench import cli
from mechbench_runner.config import Config
from mechbench_runner.verbs import Ctx, invoke, noun
from mechbench_runner.verbs.observability import trace_lines

CFG = Config(api_base_url="http://api.test", api_key="mbk_test",
             poll_interval_seconds=0.01, warm_model_id=None, runner_id=None)

TRACE_ID = "0af7651916cd43dd8448eb211c80319c"


def span(id_, name, type_, subtype, ms, children=(), outcome="success"):
    return {"id": id_, "traceId": TRACE_ID, "transactionId": "tx_1", "parentId": "tx_1",
            "name": name, "type": type_, "subtype": subtype,
            "timestamp": "2026-09-30T10:00:00.001Z", "durationMs": ms,
            "outcome": outcome, "labels": {}, "children": list(children)}


TRACE = {
    "transaction": {
        "id": "tx_1", "traceId": TRACE_ID, "origin": "server",
        "name": "/projects/:owner/:slug", "type": "request",
        "timestamp": "2026-09-30T10:00:00.000Z", "durationMs": 48.25,
        "outcome": "failure", "status": 500, "method": "GET", "userId": "u_1",
        "actorKind": "agent", "via": "cli", "apiKeyId": "key_9", "instance": "ip-10-0-0-4",
        "release": "api-0.200.0", "labels": {}, "entity": {},
    },
    "spans": [
        span("sp_1", "SELECT projects", "db", "mysql", 12.5,
             [span("sp_2", "GET s3", "external", "http", 3.0, outcome="failure")]),
        span("sp_3", "render", "app", None, 1.0),
    ],
    "errors": [
        {"id": "er_1", "traceId": TRACE_ID, "transactionId": "tx_1",
         "timestamp": "2026-09-30T10:00:00.040Z", "type": "TypeError",
         "code": "ECONNREFUSED", "message": "connect refused", "fingerprint": "fp_a",
         "stack": None, "route": "/projects/:owner/:slug", "userId": "u_1", "labels": {}},
    ],
    "links": {"visitId": "vis_1", "visitorId": "vtr_1", "userId": "u_1",
              "entity": {"project": "prj_1"}},
}

GROUPS = [
    {"fingerprint": f"fp_{i}", "type": "TypeError", "code": None, "message": "boom",
     "firstSeenAt": "2026-09-30T09:00:00Z", "lastSeenAt": "2026-09-30T10:00:00Z",
     "count": 10 - i, "lastTraceId": TRACE_ID, "lastRoute": "/runs/:id"}
    for i in range(5)
]


@pytest.fixture
def api(monkeypatch):
    calls: list[tuple] = []

    def fake(self, method, route, *, query=None, body=None):
        q = {k: v for k, v in (query or {}).items() if v is not None}
        calls.append((method, route, q, body))
        if route == "/admin/observability/errors":
            offset = int(q.get("offset") or 0)
            limit = int(q.get("limit") or 50)
            page = GROUPS[offset:offset + limit]
            more = offset + limit < len(GROUPS)
            return page, ({"x-next-offset": str(offset + limit)} if more else {})
        return TRACE, {}

    monkeypatch.setattr(Ctx, "api", fake)
    monkeypatch.setattr(Config, "from_env", classmethod(lambda cls: CFG))
    return calls


def test_the_verbs_shapes_and_effects():
    n = noun("observability")
    assert [(v.name, v.shape, v.effect) for v in n.verbs] == [
        ("traces", "read", "read"), ("errors", "list", "read")]
    assert n.verb("errors").columns == (
        "fingerprint", "type", "code", "count", "lastSeenAt", "lastRoute", "lastTraceId")


def test_traces_reads_the_trace_by_id(api):
    out = invoke(Ctx(CFG), "observability", "traces", {"id": TRACE_ID})
    assert api[-1][:2] == ("GET", f"/admin/observability/traces/{TRACE_ID}")
    assert out == TRACE


def test_the_trace_prints_its_transaction_spans_as_a_tree_errors_and_links():
    lines = trace_lines(TRACE)
    assert lines[0] == f"{TRACE_ID}  GET /projects/:owner/:slug  500  48.2ms  failure"
    assert lines[1] == ("server request at 2026-09-30T10:00:00.000Z · user u_1 · "
                        "agent via cli · key key_9 · instance ip-10-0-0-4 · release api-0.200.0")
    s = lines.index("spans")
    assert lines[s + 1:s + 4] == [
        "  SELECT projects  db/mysql  12.5ms  success",
        "    GET s3  external/http  3.0ms  failure",
        "  render  app  1.0ms  success",
    ]
    e = lines.index("errors")
    assert lines[e + 1] == ("  2026-09-30T10:00:00.040Z  TypeError:ECONNREFUSED  "
                            "connect refused  [fp_a]")
    k = lines.index("links")
    assert lines[k + 1:] == ["  visit vis_1", "  visitor vtr_1", "  user u_1",
                             "  project prj_1"]
    assert s < e < k


def test_an_empty_trace_says_none_in_each_section():
    lines = trace_lines({"transaction": TRACE["transaction"], "spans": [], "errors": [],
                         "links": {"visitId": None, "visitorId": None, "userId": None,
                                   "entity": {}}})
    assert lines.count("  (none)") == 3


def test_the_cli_prints_the_trace(api, capsys):
    assert cli.main(["observability", "traces", TRACE_ID]) == 0
    out = capsys.readouterr().out
    assert out.startswith(f"{TRACE_ID}  GET /projects/:owner/:slug")
    assert "    GET s3  external/http" in out


def test_errors_passes_its_filters_and_pages_by_x_next_offset(api):
    ctx = Ctx(CFG)
    out = invoke(ctx, "observability", "errors",
                 {"since": "2026-09-29T00:00:00Z", "search": "boom", "limit": 2})
    assert api[-1][:3] == ("GET", "/admin/observability/errors",
                           {"since": "2026-09-29T00:00:00Z", "search": "boom", "limit": 2})
    assert [g["fingerprint"] for g in out["items"]] == ["fp_0", "fp_1"]
    assert out["next"] == 2
    out = invoke(ctx, "observability", "errors", {"limit": 2, "offset": 4})
    assert [g["fingerprint"] for g in out["items"]] == ["fp_4"] and out["next"] is None


def test_the_cli_prints_the_error_table_and_the_next_page(api, capsys):
    assert cli.main(["observability", "errors", "--limit", "2", "--since",
                     "2026-09-29T00:00:00Z"]) == 0
    got = capsys.readouterr()
    header, first, second = got.out.splitlines()
    assert header.split() == ["fingerprint", "type", "code", "count", "lastSeenAt",
                              "lastRoute", "lastTraceId"]
    assert first.split() == ["fp_0", "TypeError", "10", "2026-09-30T10:00:00Z", "/runs/:id",
                             TRACE_ID]
    assert second.startswith("fp_1")
    assert "(more: --offset 2)" in got.err
    assert cli.main(["observability", "errors", "--json", "--offset", "4"]) == 0
    assert json.loads(capsys.readouterr().out)["next"] is None
