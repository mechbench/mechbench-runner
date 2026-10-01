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


OPEN_RUN = {"id": "live_open", "form": "open", "spec": {"model": "tiny/m"}, "params": {}, "state": [],
            "seq": 0, "idleSeconds": 600, "limits": {"inlineBytes": 256 * 1024, "trySeconds": 120}}
RECORDS = [{"id": "a", "text": "x"}, {"id": "b", "text": "y"}]


class TryApi(FakeApi):
    def __init__(self, leases=()):
        super().__init__(leases)
        self.refused: list[tuple] = []
        self.written: list[tuple] = []
        self.released: list[tuple] = []

    def live_complete(self, live_run_id, seq, *, outputs=None, state=None, error=None, refused=False):
        self.completed.append((live_run_id, seq, outputs, state, error))
        if refused:
            self.refused.append((seq, error))

    def put_scratch(self, path, payload, *, operation=None, params=None):
        self.written.append((path, payload, operation))
        return {"path": path}

    def live_release(self, live_run_id, reason):
        self.released.append((live_run_id, reason))


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def _open_host(monkeypatch, **kw):
    loads, warmups = [], []
    ex = ProtocolExecutor()
    monkeypatch.setattr(ex, "_model_loaded", lambda m: loads.append(m) or "model")
    real = __import__("mechbench_compute.live.run_try", fromlist=["run_try"]).run_try

    def counting(executor, **k):
        if k.get("op") == "logits/read":
            warmups.append(k["model"])
            return {}
        return real(executor, **k)

    monkeypatch.setattr("mechbench_compute.live.run_try.run_try", counting)
    sent: list[dict] = []
    clock = Clock()
    host = LiveHost(ex, sent.append, clock=clock, **kw)
    return host, sent, loads, warmups, clock


def _try(seq, **event):
    return {"op": "event", "liveRunId": "live_open", "seq": seq, "params": {},
            "event": {"id": f"e{seq}", "type": "try", **event}}


def test_an_open_live_run_warms_with_one_forward_pass_then_says_ready(monkeypatch):
    host, sent, loads, warmups, _ = _open_host(monkeypatch)
    host.offer({"op": "attach", "liveRun": OPEN_RUN})
    host.serve(TryApi())
    assert loads == ["tiny/m"] and warmups == ["tiny/m"]
    states = [f["state"] for f in sent if f["op"] == "status"]
    assert states[-1] == "ready" and states.count("ready") == 1
    assert host.holding and host.held == "live_open"


def test_a_try_answers_inline_with_its_lines_and_provenance(monkeypatch):
    host, _, _, _, _ = _open_host(monkeypatch)
    api = TryApi()
    host.offer({"op": "attach", "liveRun": OPEN_RUN})
    host.offer(_try(1, graph={"nodes": [{"id": "u", "block": "records/union", "params": {}, "inputs": {}}],
                              "edges": [{"from": {"input": "a"}, "to": {"node": "u", "port": "before"}}]},
                    inputs={"a": RECORDS}))
    host.serve(api)
    (_, seq, out, state, error), = api.completed
    assert (seq, error, state) == (1, None, None)
    assert out["address"] is None and out["kind"] == "records/record"
    assert [i["id"] for i in out["result"]["items"]] == ["a", "b"]
    assert out["lines"] == ["2 records/record items"]
    assert out["provenance"]["hash"].startswith("sha256:") and out["provenance"]["model"] == "tiny/m"
    assert api.written == []


def test_a_try_past_the_inline_cap_is_written_to_scratch(monkeypatch):
    host, _, _, _, _ = _open_host(monkeypatch)
    api = TryApi()
    host.offer({"op": "attach", "liveRun": {**OPEN_RUN, "limits": {"inlineBytes": 64}}})
    big = [{"id": str(i), "text": "x" * 40} for i in range(4)]
    host.offer(_try(1, op="records/union", inputs={"before": big}))
    host.serve(api)
    out = api.completed[0][2]
    assert out["address"] == "~scratch/live_open/t1" and out["result"] is None
    assert api.written[0][0] == "~scratch/live_open/t1"
    assert api.written[0][2] == "~canonical/ops/records/union"


def test_a_try_reads_an_earlier_try_from_memory(monkeypatch):
    host, _, _, _, _ = _open_host(monkeypatch)
    api = TryApi()
    host.offer({"op": "attach", "liveRun": OPEN_RUN})
    host.offer(_try(1, op="records/union", inputs={"before": RECORDS}))
    host.offer(_try(2, op="records/union", inputs={"before": {"$ref": {"bench": "~scratch/live_open/t1"}},
                                                    "now": [{"id": "c", "text": "z"}]}))
    host.serve(api)
    assert [c[4] for c in api.completed] == [None, None]
    assert sorted(i["id"] for i in api.completed[1][2]["result"]["items"]) == ["a", "b", "c"]


def test_a_refused_try_says_why_and_is_marked_refused(monkeypatch):
    host, _, _, _, _ = _open_host(monkeypatch)
    api = TryApi()
    host.offer({"op": "attach", "liveRun": OPEN_RUN})
    host.offer(_try(1, op="adapter/train", inputs={}))
    host.offer(_try(2, op="logits/read-layers", params={"model": "other/model"}, inputs={}))
    host.serve(api)
    assert api.refused == [(1, "adapter/train is a job, not a try"),
                           (2, "this live run holds tiny/m, not other/model")]


def test_a_runner_whose_policy_holds_no_live_run_releases_the_lease(monkeypatch):
    host, sent, loads, _, _ = _open_host(monkeypatch, may_hold=lambda _api: "The policy holds no live runs on this machine.")
    api = TryApi()
    host.offer({"op": "attach", "liveRun": OPEN_RUN})
    host.serve(api)
    assert loads == [] and not host.attached
    assert api.released == [("live_open", "The policy holds no live runs on this machine.")]
    assert [f["state"] for f in sent if f["op"] == "status"] == ["refused"]


def test_a_warm_live_run_claims_nothing_for_thirty_seconds_after_a_try_then_pure_jobs(monkeypatch):
    from mechbench_runner.live import ANY, NOTHING, PURE

    host, _, _, _, clock = _open_host(monkeypatch)
    assert host.claims() == ANY
    host.offer({"op": "attach", "liveRun": OPEN_RUN})
    host.serve(TryApi())
    assert host.claims() == PURE
    host.offer(_try(1, op="records/union", inputs={"before": RECORDS}))
    host.serve(TryApi())
    assert host.claims() == NOTHING
    clock.now += 29
    assert host.claims() == NOTHING and 0 < host.quiet_for() <= 1
    clock.now += 2
    assert host.claims() == PURE


def test_a_live_run_released_for_idleness_restricts_nothing(monkeypatch):
    from mechbench_runner.live import ANY

    host, sent, _, _, _ = _open_host(monkeypatch)
    host.offer({"op": "attach", "liveRun": {**OPEN_RUN, "idleSeconds": 1e-9}})
    host.serve(TryApi())
    host.serve(TryApi())
    assert host.attached and not host.holding
    assert host.claims() == ANY
    assert [f["state"] for f in sent if f["op"] == "status"][-1] == "released"
