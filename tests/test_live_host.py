from __future__ import annotations

import pytest

pytest.importorskip("mechbench_compute")

from mechbench_compute.protocol import ProtocolExecutor  # noqa: E402

from mechbench_runner.live import LiveHost  # noqa: E402

GRAPH = {
    "nodes": [{"id": "said", "block": "records/union", "params": {}, "inputs": {}}],
    "edges": [
        {"from": {"input": "state"}, "to": {"node": "said", "port": "before"}},
        {"from": {"input": "event"}, "to": {"node": "said", "port": "now"}},
    ],
}
LIVE_RUN = {"id": "live_1", "spec": {"graph": GRAPH, "outputs": [{"name": "state", "from": {"node": "said"}}]},
            "params": {}, "state": [], "seq": 0, "idleSeconds": 600}


class FakeApi:
    def __init__(self, leases=()):
        self.completed: list[tuple] = []
        self.leases = list(leases)

    def live_complete(self, live_run_id, seq, *, outputs=None, state=None, error=None):
        self.completed.append((live_run_id, seq, outputs, state, error))

    def live_leased(self):
        return self.leases


def _host():
    sent: list[dict] = []
    return LiveHost(ProtocolExecutor(), sent.append), sent


def _texts(state):
    return sorted(it.get("text") for it in state["items"])


def test_an_attached_live_run_runs_each_event_and_keeps_the_state_it_leaves():
    host, _ = _host()
    api = FakeApi()
    host.offer({"op": "attach", "liveRun": LIVE_RUN})
    host.offer({"op": "event", "liveRunId": "live_1", "seq": 1, "params": {},
                "event": {"id": "e1", "type": "message", "text": "hi"}})
    host.offer({"op": "event", "liveRunId": "live_1", "seq": 2, "params": {},
                "event": {"id": "e2", "type": "message", "text": "again"}})
    assert host.wake.is_set()
    host.serve(api)
    assert [(c[1], c[4]) for c in api.completed] == [(1, None), (2, None)]
    assert _texts(api.completed[-1][3]) == ["again", "hi"]
    assert host.holding and not host.wake.is_set()


def test_an_event_for_a_live_run_it_does_not_hold_catches_up_first():
    host, _ = _host()
    api = FakeApi(leases=[{"op": "attach", "liveRun": LIVE_RUN,
                           "running": [{"seq": 1, "params": {}, "event": {"id": "e1", "type": "message", "text": "hi"}}]}])
    host.offer({"op": "event", "liveRunId": "live_1", "seq": 1, "params": {},
                "event": {"id": "e1", "type": "message", "text": "hi"}})
    host.serve(api)
    assert [c[1] for c in api.completed] == [1]


def test_a_step_that_fails_is_recorded_as_its_error():
    host, _ = _host()
    api = FakeApi()
    host.offer({"op": "attach", "liveRun": {**LIVE_RUN, "spec": {"graph": GRAPH, "outputs": [{"name": "said", "from": {"node": "said"}}]}}})
    host.offer({"op": "event", "liveRunId": "live_1", "seq": 1, "params": {}, "event": {"id": "e1", "type": "message"}})
    host.serve(api)
    assert api.completed[0][4].startswith("ValueError")


def test_detaching_the_last_live_run_lets_the_runner_claim_model_work_again():
    host, _ = _host()
    host.offer({"op": "attach", "liveRun": LIVE_RUN})
    host.serve(FakeApi())
    assert host.holding
    host.offer({"op": "detach", "liveRunId": "live_1"})
    host.serve(FakeApi())
    assert not host.holding


def test_attaching_warms_the_handler_with_a_step_it_does_not_record(monkeypatch):
    steps = []
    real = __import__("mechbench_compute.live.run_step", fromlist=["run_step"]).run_step
    monkeypatch.setattr("mechbench_compute.live.run_step.run_step", lambda *a, **k: steps.append(k["event"]) or real(*a, **k))
    host, sent = _host()
    api = FakeApi()
    run = {**LIVE_RUN, "spec": {**LIVE_RUN["spec"], "signature": {"events": [{"type": "message"}]}}}
    host.offer({"op": "attach", "liveRun": run})
    host.serve(api)
    assert [e["id"] for e in steps] == ["e0"]
    assert api.completed == []
    assert [f["state"] for f in sent if f["op"] == "status"].count("ready") == 1
    assert [f["state"] for f in sent if f["op"] == "status"][-1] == "ready"


def test_a_live_runs_other_inputs_are_resolved_once_for_all_its_steps(monkeypatch):
    from mechbench_compute.protocol import resolver as resolver_mod

    calls = []
    real = resolver_mod.Resolver.resolve_value
    monkeypatch.setattr(resolver_mod.Resolver, "resolve_value",
                        lambda self, v, keep_reference=False: calls.append(v) or real(self, v, keep_reference))
    host, _ = _host()
    api = FakeApi()
    run = {**LIVE_RUN, "spec": {**LIVE_RUN["spec"], "inputs": {"notes": [{"id": "n1", "text": "a note"}]}}}
    host.offer({"op": "attach", "liveRun": run})
    for seq in (1, 2):
        host.offer({"op": "event", "liveRunId": "live_1", "seq": seq, "params": {},
                    "event": {"id": f"e{seq}", "type": "message", "text": "hi"}})
    host.serve(api)
    assert [c[4] for c in api.completed] == [None, None]
    top_level = [c for c in calls if c == [{"id": "n1", "text": "a note"}]]
    assert len(top_level) == 1
