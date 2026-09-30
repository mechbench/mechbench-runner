from __future__ import annotations

import io
from datetime import UTC, datetime

import pytest

from mechbench import cli
from mechbench_runner.config import Config
from mechbench_runner.verbs import Ctx, invoke, noun
from mechbench_runner.verbs.support import age, history_lines

CFG = Config(api_base_url="http://api.test", api_key="mbk_test",
             poll_interval_seconds=0.01, warm_model_id=None, runner_id=None)

CASE = {
    "id": "sup_1", "subject": "Runner will not sign in", "status": "open",
    "requester": {"userId": None, "handle": None, "address": "ada@example.com"},
    "plan": "lab", "priority": True, "via": "email", "mailThread": True,
    "messageCount": 2, "lastInboundAt": "2026-09-29T10:00:00Z",
    "openedAt": "2026-09-29T09:00:00Z", "updatedAt": "2026-09-29T10:00:00Z",
    "closedAt": None,
}
BENJI = {"kind": "admin", "userId": "u_1", "handle": "benji"}
ADA = {"kind": "requester", "userId": None, "handle": None}
ANSWER = {
    "case": CASE,
    "messages": [
        {"id": "m_2", "caseId": "sup_1", "via": "cli", "direction": "out", "author": BENJI,
         "body": "Try `mechbench login` again.", "bodyHtml": None, "attachments": [],
         "delivery": {"agentmailMessageId": "am_1", "sentAt": "2026-09-29T11:00:01Z",
                      "sentBy": BENJI},
         "createdAt": "2026-09-29T11:00:00Z"},
        {"id": "m_1", "caseId": "sup_1", "via": "email", "direction": "in", "author": ADA,
         "body": "It hangs.\nAt the code.", "bodyHtml": None,
         "attachments": [{"filename": "log.txt", "contentType": "text/plain", "size": 12,
                          "agentmailAttachmentId": "a_1", "object": None}],
         "delivery": None, "createdAt": "2026-09-29T09:00:00Z"},
    ],
    "events": [
        {"id": "e_1", "kind": "status", "actor": BENJI, "at": "2026-09-29T12:00:00Z",
         "details": {"status": "closed"}},
        {"id": "e_2", "kind": "sent", "actor": BENJI, "at": "2026-09-29T11:00:01Z",
         "details": None},
    ],
}


@pytest.fixture
def api(monkeypatch):
    calls: list[tuple] = []

    def fake(self, method, route, *, query=None, body=None):
        calls.append((method, route, {k: v for k, v in (query or {}).items()
                                      if v is not None}, body))
        if method == "GET" and route == "/support/cases":
            return {"cases": [CASE, {**CASE, "id": "sup_2", "subject": "Billing",
                                     "requester": {"userId": "u_2", "handle": "grace",
                                                   "address": "g@example.com"}}]}, {}
        if method == "GET":
            return ANSWER, {}
        if method == "PATCH":
            return {"case": {**CASE, "status": "closed"}}, {}
        return {"case": CASE, "message": ANSWER["messages"][0]}, {}

    monkeypatch.setattr(Ctx, "api", fake)
    monkeypatch.setattr(Config, "from_env", classmethod(lambda cls: CFG))
    return calls


def test_the_verbs_and_their_effects():
    n = noun("support")
    assert {v.name: v.effect for v in n.verbs} == {
        "list": "read", "read": "read", "open": "outward", "reply": "outward",
        "close": "draft"}
    assert n.verb("reply").needs_consent({}) and n.verb("open").needs_consent({})


def test_list_asks_by_status_and_prints_the_queue(api, capsys):
    out = invoke(Ctx(CFG), "support", "list", {"status": "open", "limit": 5})
    assert api[-1][:3] == ("GET", "/support/cases", {"status": "open", "limit": 5})
    assert [c["requester"] for c in out["items"]] == ["ada@example.com", "grace"]
    assert out["items"][0]["age"]
    assert cli.main(["support", "list"]) == 0
    header, first, _ = capsys.readouterr().out.splitlines()
    assert header.split() == ["id", "status", "subject", "requester", "age", "plan"]
    assert first.startswith("sup_1") and first.rstrip().endswith("lab")


def test_search_narrows_by_subject_or_requester(api):
    out = invoke(Ctx(CFG), "support", "list", {"search": "GRACE"})
    assert [c["id"] for c in out["items"]] == ["sup_2"]


def test_reply_open_and_close_say_they_came_from_the_command_line(api, monkeypatch):
    ctx = Ctx(CFG)
    invoke(ctx, "support", "reply", {"id": "sup_1", "body": "On its way."})
    assert api[-1] == ("POST", "/support/cases/sup_1/messages", {},
                       {"body": "On its way.", "via": "cli"})
    invoke(ctx, "support", "open", {"subject": "Hi", "body": "Hello."})
    assert api[-1] == ("POST", "/support/cases", {},
                       {"subject": "Hi", "body": "Hello.", "via": "cli"})
    assert invoke(ctx, "support", "close", {"id": "sup_1"})["status"] == "closed"
    assert api[-1] == ("PATCH", "/support/cases/sup_1", {},
                       {"status": "closed", "via": "cli"})
    monkeypatch.setattr("sys.stdin", io.StringIO("Dictated.\n"))
    invoke(ctx, "support", "reply", {"id": "sup_1", "body": "-"})
    assert api[-1][3]["body"] == "Dictated.\n"


def test_read_prints_the_history_in_order_with_who_when_and_which_way(api, capsys):
    assert cli.main(["support", "read", "sup_1"]) == 0
    out = capsys.readouterr().out
    assert out.index("It hangs.") < out.index("Try `mechbench login`") < out.index("status by")
    assert "<- requester via email" in out
    assert "-> benji via cli (sent 2026-09-29T11:00:01Z by benji)" in out
    assert "[attachment] log.txt (text/plain, 12 bytes)" in out
    assert "** sent" not in out
    assert history_lines(ANSWER)[1] == (
        "requester ada@example.com · plan lab · priority · mail thread")


def test_age_reads_the_largest_unit():
    now = datetime(2026, 9, 29, 12, 0, tzinfo=UTC)
    assert age("2026-09-29T10:00:00Z", now) == "2h"
    assert age("2026-09-27T12:00:00Z", now) == "2d"
    assert age("2026-09-29T11:59:30Z", now) == "30s"
    assert age(None, now) == "" and age("soon", now) == ""
