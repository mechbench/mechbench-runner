from __future__ import annotations

import io
from datetime import UTC, datetime

import pytest

from mechbench import cli
from mechbench_runner.config import Config
from mechbench_runner.verbs import Ctx, VerbError, invoke, noun
from mechbench_runner.verbs.case import age, history_lines

CFG = Config(api_base_url="http://api.test", api_key="mbk_test",
             poll_interval_seconds=0.01, warm_model_id=None, runner_id=None)

CASE = {
    "id": "cas_1", "kind": "support", "subject": "Runner will not sign in", "status": "open",
    "about": None, "audience": {"kind": "platform"}, "assigneeUserId": None, "outcome": None,
    "requester": {"userId": None, "handle": None, "address": "ada@example.com"},
    "plan": "lab", "priority": True, "via": "email", "mailThread": True,
    "messageCount": 3, "lastInboundAt": "2026-09-29T10:00:00Z",
    "openedAt": "2026-09-29T09:00:00Z", "updatedAt": "2026-09-29T10:00:00Z",
    "closedAt": None,
}
BENJI = {"kind": "admin", "userId": "u_1", "handle": "benji"}
ADA = {"kind": "requester", "userId": None, "handle": None}
ANSWER = {
    "case": CASE,
    "messages": [
        {"id": "m_2", "caseId": "cas_1", "via": "cli", "direction": "out", "visibility": "external",
         "author": BENJI, "body": "Try `mechbench login` again.", "bodyHtml": None, "attachments": [],
         "delivery": {"agentmailMessageId": "am_1", "sentAt": "2026-09-29T11:00:01Z",
                      "sentBy": BENJI},
         "createdAt": "2026-09-29T11:00:00Z"},
        {"id": "m_1", "caseId": "cas_1", "via": "email", "direction": "in", "visibility": "external",
         "author": ADA, "body": "It hangs.\nAt the code.", "bodyHtml": None,
         "attachments": [{"filename": "log.txt", "contentType": "text/plain", "size": 12,
                          "agentmailAttachmentId": "a_1", "object": None}],
         "delivery": None, "createdAt": "2026-09-29T09:00:00Z"},
        {"id": "m_3", "caseId": "cas_1", "via": "cli", "direction": "out", "visibility": "internal",
         "author": BENJI, "body": "Probably the clock skew.", "bodyHtml": None, "attachments": [],
         "delivery": None, "createdAt": "2026-09-29T10:30:00Z"},
    ],
    "events": [
        {"id": "e_1", "kind": "closed", "actor": BENJI, "at": "2026-09-29T12:00:00Z",
         "details": {"outcome": "resolved"}},
        {"id": "e_2", "kind": "sent", "actor": BENJI, "at": "2026-09-29T11:00:01Z",
         "details": None},
        {"id": "e_3", "kind": "note", "actor": BENJI, "at": "2026-09-29T10:30:00Z",
         "details": None},
        {"id": "e_4", "kind": "assigned", "actor": BENJI, "at": "2026-09-29T10:15:00Z",
         "details": {"userId": "u_1"}},
    ],
}
USERS = [{"id": "u_7", "handle": "grace-h"}, {"id": "u_8", "handle": "grace"}]


@pytest.fixture
def api(monkeypatch):
    calls: list[tuple] = []

    def fake(self, method, route, *, query=None, body=None):
        calls.append((method, route, {k: v for k, v in (query or {}).items()
                                      if v is not None}, body))
        if method == "GET" and route == "/cases":
            return {"cases": [CASE, {**CASE, "id": "cas_2", "kind": "submission",
                                     "assigneeUserId": "u_8",
                                     "requester": {"userId": "u_2", "handle": "grace",
                                                   "address": "g@example.com"}}]}, {}
        if route == "/admin/users":
            return USERS, {}
        if method == "GET":
            return ANSWER, {}
        if route.endswith("/close") or route.endswith("/assign"):
            return {"case": {**CASE, "status": "closed"}}, {}
        return {"case": CASE, "message": ANSWER["messages"][0]}, {}

    monkeypatch.setattr(Ctx, "api", fake)
    monkeypatch.setattr(Config, "from_env", classmethod(lambda cls: CFG))
    return calls


def test_the_verbs_and_their_effects():
    n = noun("case")
    assert {v.name: v.effect for v in n.verbs} == {
        "list": "read", "read": "read", "reply": "outward", "note": "draft",
        "assign": "draft", "close": "draft"}
    assert n.verb("reply").needs_consent({}) and not n.verb("note").needs_consent({})


def test_list_asks_by_kind_status_and_assignee_and_prints_the_queue(api, capsys):
    out = invoke(Ctx(CFG), "case", "list",
                 {"kind": "submission", "status": "all", "assignee": "@grace", "limit": 5})
    assert api[0][:3] == ("GET", "/admin/users", {"search": "grace"})
    assert api[-1][:3] == ("GET", "/cases", {"kind": "submission", "status": "all",
                                             "assignee": "u_8", "limit": 5})
    assert [c["requester"] for c in out["items"]] == ["ada@example.com", "grace"]
    assert out["items"][1]["assignee"] == "u_8" and out["items"][0]["age"]
    invoke(Ctx(CFG), "case", "list", {"assignee": "me", "search": "sign"})
    assert api[-1][2] == {"assignee": "me", "search": "sign"}
    assert cli.main(["case", "list"]) == 0
    header, first, _ = capsys.readouterr().out.splitlines()
    assert header.split() == ["id", "kind", "status", "subject", "requester", "assignee", "age"]
    assert first.startswith("cas_1")


def test_an_unknown_handle_is_refused(api):
    with pytest.raises(VerbError, match="no user @nobody"):
        invoke(Ctx(CFG), "case", "assign", {"id": "cas_1", "to": "nobody"})


def test_reply_is_external_and_note_internal(api, monkeypatch):
    ctx = Ctx(CFG)
    invoke(ctx, "case", "reply", {"id": "cas_1", "body": "On its way."})
    assert api[-1] == ("POST", "/cases/cas_1/messages", {},
                       {"body": "On its way.", "visibility": "external"})
    invoke(ctx, "case", "note", {"id": "cas_1", "body": "Clock skew."})
    assert api[-1] == ("POST", "/cases/cas_1/messages", {},
                       {"body": "Clock skew.", "visibility": "internal"})
    monkeypatch.setattr("sys.stdin", io.StringIO("Dictated.\n"))
    invoke(ctx, "case", "reply", {"id": "cas_1", "body": "-"})
    assert api[-1][3]["body"] == "Dictated.\n"


def test_assign_to_me_by_default_a_handle_or_none(api):
    ctx = Ctx(CFG)
    invoke(ctx, "case", "assign", {"id": "cas_1"})
    assert api[-1] == ("POST", "/cases/cas_1/assign", {}, {"userId": "me"})
    invoke(ctx, "case", "assign", {"id": "cas_1", "to": "grace"})
    assert api[-1][3] == {"userId": "u_8"}
    invoke(ctx, "case", "assign", {"id": "cas_1", "to": "none"})
    assert api[-1][3] == {"userId": None}


def test_close_with_or_without_an_outcome(api):
    ctx = Ctx(CFG)
    assert invoke(ctx, "case", "close", {"id": "cas_1"})["status"] == "closed"
    assert api[-1] == ("POST", "/cases/cas_1/close", {}, {})
    invoke(ctx, "case", "close", {"id": "cas_1", "outcome": "withdrawn"})
    assert api[-1][3] == {"outcome": "withdrawn"}


def test_read_prints_the_timeline_with_notes_marked_and_events_between(api, capsys):
    assert cli.main(["case", "read", "cas_1"]) == 0
    out = capsys.readouterr().out
    assert (out.index("It hangs.") < out.index("assigned by benji")
            < out.index("Probably the clock skew.") < out.index("Try `mechbench login`")
            < out.index("closed by"))
    assert "<- requester via email" in out
    assert "-> benji via cli (sent 2026-09-29T11:00:01Z by benji)" in out
    assert "## internal note by benji via cli" in out
    assert "   ## Probably the clock skew." in out
    assert "[attachment] log.txt (text/plain, 12 bytes)" in out
    assert "** sent" not in out and "** note" not in out
    assert history_lines(ANSWER)[1] == (
        "requester ada@example.com · plan lab · priority · mail thread")


def test_a_submission_names_what_it_is_about():
    c = {**CASE, "kind": "submission", "status": "closed", "outcome": "rejected",
         "assigneeUserId": "u_1",
         "about": {"kind": "extension_version", "address": "alice/lab/extensions/x",
                   "version": 3, "hash": "sha256:ab"}}
    lines = history_lines({"case": c, "messages": [], "events": []})
    assert lines[0] == "cas_1  submission  closed (rejected)  Runner will not sign in"
    assert lines[2:] == ["about alice/lab/extensions/x@3 (sha256:ab)", "assignee u_1"]


def test_age_reads_the_largest_unit():
    now = datetime(2026, 9, 29, 12, 0, tzinfo=UTC)
    assert age("2026-09-29T10:00:00Z", now) == "2h"
    assert age("2026-09-27T12:00:00Z", now) == "2d"
    assert age("2026-09-29T11:59:30Z", now) == "30s"
    assert age(None, now) == "" and age("soon", now) == ""
