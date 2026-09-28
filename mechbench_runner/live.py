from __future__ import annotations

import json
import queue
import threading
import time
import traceback
from dataclasses import dataclass, field
from typing import Any, Callable

PURE = "pure"


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


def _plain(value: Any) -> Any:
    def fallback(v: Any) -> Any:
        if hasattr(v, "tolist"):
            return v.tolist()
        if hasattr(v, "model_dump"):
            return v.model_dump(mode="json")
        return str(v)

    return json.loads(json.dumps(value, default=fallback))


class LiveHost:
    def __init__(self, executor: Any, send: Callable[[dict[str, Any]], None],
                 stamp: Callable[[], None] = lambda: None) -> None:
        self.executor = executor
        self.send = send
        self.stamp = stamp
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
    def holding(self) -> bool:
        return bool(self.sessions)

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
                self._attach(lease["liveRun"])
                for ev in lease.get("running") or []:
                    self._step(api, lease["liveRun"]["id"], ev)
        elif op == "attach":
            self._attach(frame["liveRun"])
        elif op == "event":
            live_run_id = str(frame.get("liveRunId"))
            if live_run_id not in self.sessions:
                self._take(api, {"op": "catchup"})
                return
            self._step(api, live_run_id, frame)
        elif op == "detach":
            if self.sessions.pop(str(frame.get("liveRunId")), None) is not None and not self.holding:
                self._let_model_go()

    def _status(self, live_run_id: str, state: str, message: str | None = None) -> None:
        self.send({"op": "status", "liveRunId": live_run_id, "state": state,
                   **({"message": message} if message else {})})

    def _attach(self, lr: dict[str, Any]) -> None:
        session = Session(id=lr["id"], spec=lr["spec"], params=dict(lr.get("params") or {}),
                          state=lr.get("state"), idle_seconds=float(lr.get("idleSeconds") or 600))
        self.sessions[session.id] = session
        self._warm(session, announce=False)
        if session.warm:
            self._warm_up(session)

    def _warm(self, session: Session, *, announce: bool = True) -> None:
        model = session.params.get("model")
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

    def _step(self, api: Any, live_run_id: str, frame: dict[str, Any]) -> None:
        from mechbench_compute.live.run_step import run_step

        session = self.sessions[live_run_id]
        seq = int(frame["seq"])
        session.params = dict(frame.get("params") or session.params)
        if not session.warm:
            self._warm(session)

        def on_token(node: str, key: str, token: dict[str, Any]) -> None:
            with self._lock:
                stop = live_run_id in self._stopping
            if stop:
                raise Stopped
            self.stamp()
            self.send({"op": "stream", "liveRunId": live_run_id, "seq": seq,
                       "node": node, "key": key, "token": token})

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
