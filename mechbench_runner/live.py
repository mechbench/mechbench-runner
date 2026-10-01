from __future__ import annotations

import json
import queue
import threading
import time
import traceback
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Callable

PURE = "pure"
ANY = "any"
NOTHING = "nothing"
QUIET_SECONDS = 30.0
INLINE_BYTES = 256 * 1024
TRY_SECONDS = 120.0
CACHED_RESULTS = 16
OPEN = "open"
WARM_RECORDS = [{"id": "warm", "user": "Hello."}]


class Stopped(Exception):
    pass


@dataclass
class Session:
    id: str
    spec: dict[str, Any]
    params: dict[str, Any]
    state: Any
    idle_seconds: float
    last: float = field(default_factory=time.monotonic)
    warm: bool = False
    inputs: dict[str, Any] = field(default_factory=dict)
    form: str = "handler"
    model: str | None = None
    inline_bytes: int = INLINE_BYTES
    try_seconds: float = TRY_SECONDS
    results: OrderedDict[str, Any] = field(default_factory=OrderedDict)

    def remember(self, name: str, value: Any) -> None:
        self.results[name] = value
        self.results.move_to_end(name)
        while len(self.results) > CACHED_RESULTS:
            self.results.popitem(last=False)


def _plain(value: Any) -> Any:
    def fallback(v: Any) -> Any:
        if hasattr(v, "tolist"):
            return v.tolist()
        if hasattr(v, "model_dump"):
            return v.model_dump(mode="json")
        return str(v)

    return json.loads(json.dumps(value, default=fallback))


def _canonical(op: str) -> str:
    from mechbench_compute import lexicon

    return lexicon.canonical_path(op)


class LiveHost:
    def __init__(self, executor: Any, send: Callable[[dict[str, Any]], None],
                 stamp: Callable[[], None] = lambda: None,
                 may_hold: Callable[[Any], str | None] = lambda _api: None,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.executor = executor
        self.send = send
        self.stamp = stamp
        self.may_hold = may_hold
        self.clock = clock
        self.last_asked: float | None = None
        self.inbox: queue.Queue[dict[str, Any]] = queue.Queue()
        self.wake = threading.Event()
        self.sessions: dict[str, Session] = {}
        self._stopping: set[str] = set()
        self._lock = threading.Lock()

    def offer(self, frame: dict[str, Any]) -> None:
        if frame.get("op") == "cancel" and isinstance(frame.get("liveRunId"), str):
            with self._lock:
                self._stopping.add(frame["liveRunId"])
            return
        self.inbox.put(frame)
        self.wake.set()

    @property
    def attached(self) -> bool:
        return bool(self.sessions)

    @property
    def holding(self) -> bool:
        return any(s.warm for s in self.sessions.values())

    @property
    def held(self) -> str | None:
        return next((s.id for s in self.sessions.values() if s.warm), None)

    def claims(self) -> str:
        if not self.holding:
            return ANY
        if self.last_asked is not None and self.clock() - self.last_asked < QUIET_SECONDS:
            return NOTHING
        return PURE

    def quiet_for(self) -> float:
        if self.last_asked is None:
            return 0.0
        return max(0.0, QUIET_SECONDS - (self.clock() - self.last_asked))

    def serve(self, api: Any) -> None:
        self.wake.clear()
        while True:
            try:
                frame = self.inbox.get_nowait()
            except queue.Empty:
                break
            try:
                self._take(api, frame)
            except Exception:  # noqa: BLE001
                traceback.print_exc()
        self._release_idle()

    def _take(self, api: Any, frame: dict[str, Any]) -> None:
        op = frame.get("op")
        if op == "catchup":
            for lease in api.live_leased():
                if not self._attach(api, lease["liveRun"]):
                    continue
                for ev in lease.get("running") or []:
                    self._dispatch(api, lease["liveRun"]["id"], ev)
        elif op == "attach":
            self._attach(api, frame["liveRun"])
        elif op == "event":
            live_run_id = str(frame.get("liveRunId"))
            if live_run_id not in self.sessions:
                self._take(api, {"op": "catchup"})
                return
            self._dispatch(api, live_run_id, frame)
        elif op == "detach":
            if self.sessions.pop(str(frame.get("liveRunId")), None) is not None and not self.holding:
                self._let_model_go()

    def _status(self, live_run_id: str, state: str, message: str | None = None) -> None:
        self.send({"op": "status", "liveRunId": live_run_id, "state": state,
                   **({"message": message} if message else {})})

    def _dispatch(self, api: Any, live_run_id: str, frame: dict[str, Any]) -> None:
        if (frame.get("event") or {}).get("type") == "try":
            self._try(api, live_run_id, frame)
        else:
            self._step(api, live_run_id, frame)

    def _attach(self, api: Any, lr: dict[str, Any]) -> bool:
        if lr["id"] in self.sessions:
            return True
        why = self.may_hold(api)
        if why is not None:
            self._status(lr["id"], "refused", why)
            try:
                api.live_release(lr["id"], why)
            except Exception as exc:  # noqa: BLE001
                print(f"[live] could not release {lr['id']}: {exc}")
            return False
        spec = lr.get("spec") or {}
        limits = lr.get("limits") or {}
        session = Session(id=lr["id"], spec=spec, params=dict(lr.get("params") or {}),
                          state=lr.get("state"), idle_seconds=float(lr.get("idleSeconds") or 600),
                          form=str(lr.get("form") or "handler"),
                          model=spec.get("model") if isinstance(spec.get("model"), str) else None,
                          inline_bytes=int(limits.get("inlineBytes") or INLINE_BYTES),
                          try_seconds=float(limits.get("trySeconds") or TRY_SECONDS))
        self.sessions[session.id] = session
        self._warm(session, announce=False)
        if session.warm:
            self._warm_up(session)
        return True

    def _model_of(self, session: Session) -> Any:
        return session.model if session.form == OPEN else session.params.get("model")

    def _warm(self, session: Session, *, announce: bool = True) -> None:
        model = self._model_of(session)
        if model is None or session.warm:
            session.warm = True
            return
        self._status(session.id, "warming", f"loading {model}")
        try:
            self.executor._model_loaded(model)
        except Exception as exc:  # noqa: BLE001
            self._status(session.id, "failed", f"could not load {model}: {exc}")
            return
        session.warm = True
        if announce:
            self._status(session.id, "ready")

    def _resolve_inputs(self, session: Session) -> None:
        from mechbench_compute.protocol.resolver import Resolver

        resolver = Resolver(bound_params=session.params)
        session.inputs = {k: resolver.resolve_value(v)
                          for k, v in (session.spec.get("inputs") or {}).items()}

    def _warm_up(self, session: Session) -> None:
        if session.form == OPEN:
            self._warm_open(session)
            return
        from mechbench_compute.live.run_step import run_step

        events = (session.spec.get("signature") or {}).get("events") or []
        if not events:
            return
        self._status(session.id, "warming", "a first step, to warm what the handler runs")
        try:
            if not session.inputs:
                self._resolve_inputs(session)
            spec = session.spec
            run_step(self.executor, graph=spec["graph"], params=session.params, outputs=spec["outputs"],
                     event={"id": "e0", "type": events[0]["type"], "text": "Hello."},
                     state=session.state, inputs=session.inputs)
        except Exception as exc:  # noqa: BLE001
            print(f"[live] warm-up for {session.id} did not run: {type(exc).__name__}: {exc}")
        self._status(session.id, "ready")

    def _warm_open(self, session: Session) -> None:
        from mechbench_compute.api import run_try

        self._status(session.id, "warming", "one forward pass, to compile what the model runs")
        try:
            run_try(self.executor, op="logits/read", inputs={"records": WARM_RECORDS},
                    params={"top_k": 1}, model=str(session.model), live_run_id=session.id)
        except Exception as exc:  # noqa: BLE001
            print(f"[live] warm-up for {session.id} did not run: {type(exc).__name__}: {exc}")
        self._status(session.id, "ready")

    def _streamer(self, live_run_id: str, seq: int) -> Callable[..., None]:
        def on_token(node: str, key: str, token: dict[str, Any]) -> None:
            with self._lock:
                stop = live_run_id in self._stopping
            if stop:
                raise Stopped
            self.stamp()
            self.send({"op": "stream", "liveRunId": live_run_id, "seq": seq,
                       "node": node, "key": key, "token": token})

        return on_token

    def _from_cache(self, session: Session, value: Any) -> Any:
        if isinstance(value, dict):
            ref = value.get("$ref")
            path = ref.get("bench") if isinstance(ref, dict) and len(ref) == 1 else None
            if isinstance(path, str) and len(value) == 1:
                head, _, name = path.rpartition("/")
                if head == f"~scratch/{session.id}" and name in session.results:
                    return session.results[name]
            return {k: self._from_cache(session, v) for k, v in value.items()}
        if isinstance(value, list):
            return [self._from_cache(session, v) for v in value]
        return value

    def _try(self, api: Any, live_run_id: str, frame: dict[str, Any]) -> None:
        from mechbench_compute.api import TryRefused, run_try

        session = self.sessions[live_run_id]
        seq = int(frame["seq"])
        event = frame.get("event") or {}
        self.last_asked = self.clock()
        if not session.warm:
            self._warm(session)
        try:
            got = run_try(self.executor, op=event.get("op"), graph=event.get("graph"),
                          inputs=self._from_cache(session, event.get("inputs") or {}),
                          params=self._from_cache(session, event.get("params") or {}),
                          model=str(session.model), on_token=self._streamer(live_run_id, seq),
                          live_run_id=live_run_id, seq=seq, wall_seconds=session.try_seconds)
        except Stopped:
            api.live_complete(live_run_id, seq, error="stopped")
        except TryRefused as exc:
            api.live_complete(live_run_id, seq, error=str(exc), refused=True)
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            api.live_complete(live_run_id, seq, error=f"{type(exc).__name__}: {exc}")
        else:
            result = _plain(got["result"])
            session.remember(f"t{seq}", result)
            address = None
            if len(json.dumps(result, separators=(",", ":"))) > session.inline_bytes:
                op = got["provenance"]["op"]
                written = api.put_scratch(f"~scratch/{live_run_id}/t{seq}", result,
                                          operation=_canonical(op) if isinstance(op, str) else None,
                                          params=event.get("params") or {})
                address = written.get("path") or f"~scratch/{live_run_id}/t{seq}"
            api.live_complete(live_run_id, seq, outputs=_plain({
                "address": address,
                "kind": got["kind"],
                "summary": got["summary"],
                "result": None if address else result,
                "lines": got["lines"],
                "runMs": round(got["seconds"] * 1000, 3),
                "provenance": {**got["provenance"], "hash": f"sha256:{got['hash']}"},
            }))
        finally:
            with self._lock:
                self._stopping.discard(live_run_id)
            session.last = time.monotonic()
            self.last_asked = self.clock()

    def _step(self, api: Any, live_run_id: str, frame: dict[str, Any]) -> None:
        from mechbench_compute.live.run_step import run_step

        session = self.sessions[live_run_id]
        seq = int(frame["seq"])
        session.params = dict(frame.get("params") or session.params)
        self.last_asked = self.clock()
        if not session.warm:
            self._warm(session)
        on_token = self._streamer(live_run_id, seq)

        spec = session.spec
        try:
            if spec.get("inputs") and not session.inputs:
                self._resolve_inputs(session)
            got = run_step(self.executor, graph=spec["graph"], params=session.params,
                           outputs=spec["outputs"], event=frame["event"], state=session.state,
                           inputs=session.inputs, on_token=on_token)
        except Stopped:
            api.live_complete(live_run_id, seq, error="stopped")
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            api.live_complete(live_run_id, seq, error=f"{type(exc).__name__}: {exc}")
        else:
            state = _plain(got["state"])
            outputs = _plain({k: v for k, v in got["outputs"].items() if k != "state"})
            api.live_complete(live_run_id, seq, outputs=outputs, state=state)
            session.state = state
        finally:
            with self._lock:
                self._stopping.discard(live_run_id)
            session.last = time.monotonic()
            self.last_asked = self.clock()

    def _release_idle(self) -> None:
        now = time.monotonic()
        idle = [s for s in self.sessions.values() if s.warm and now - s.last > s.idle_seconds]
        for s in idle:
            s.warm = False
            self._status(s.id, "released", "idle; the model loads again on the next event")
        if idle and not any(s.warm for s in self.sessions.values()):
            self._let_model_go()

    def _let_model_go(self) -> None:
        self.executor._model = None
        self.executor._model_id = None
        try:
            import mlx.core as mx

            mx.clear_cache()
        except Exception:  # noqa: BLE001
            pass
