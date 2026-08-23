"""The runner's local control surface (task 000283).

A running runner can otherwise be asked nothing: whether it is polling or
wedged, what it is executing, how far in. This serves that over a Unix domain
socket at `~/.mechbench/runner.sock`, speaking newline-delimited JSON, and it
is the one contract both `mechbench status` and mechbench-app-mac are
built on.

Why a socket rather than a file the runner writes and readers poll: a file
cannot carry commands, cannot push, and leaves every consumer to invent its
own staleness rule. A held-open connection lets the runner announce a state
change rather than have it discovered a poll later.

Why a Unix socket rather than HTTP on loopback: no port to collide with, be
firewalled, or be reachable by anything else on the machine, and the socket's
own file permissions are the authentication — there is no token to mint,
store, or leak.

    request   {"v": 1, "op": "status"}
    response  {"v": 1, "ok": true, "data": {...}}
    error     {"v": 1, "ok": false, "error": {"code": ..., "message": ...}}
    event     {"v": 1, "event": "job.claimed", "data": {...}}

Every message carries `v`. The app and the runner are updated separately and
will disagree in the field; a version both sides check turns that into a
sentence rather than a mystery.

THREADING. The job loop is synchronous and spends minutes inside model loads
and forward passes, so the server runs its own asyncio loop on a daemon
thread. `RunnerState` is the boundary: the job loop mutates it under a lock,
the server reads snapshots, and events cross to the loop via
`call_soon_threadsafe`.
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import threading
import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .paths import mechbench_dir

PROTOCOL_VERSION = 1
SOCKET_NAME = "runner.sock"

# A client that asks for nothing and holds the connection open forever is a
# leak; a subscriber that stops reading must not grow the runner's memory
# without bound. Both are bounded here rather than trusted.
MAX_LINE_BYTES = 64 * 1024
EVENT_QUEUE_LIMIT = 256


def control_dir() -> Path:
    """`~/.mechbench`, created 0700 — the socket's permissions are the auth."""
    return mechbench_dir()


def socket_path() -> Path:
    return control_dir() / SOCKET_NAME


# --- state -------------------------------------------------------------------


@dataclass
class JobView:
    """What is being executed, as the control surface describes it."""

    id: str
    protocol_kind: str
    model_id: str | None = None
    started_at: float = field(default_factory=time.time)
    done: int = 0
    total: int = 0

    @property
    def elapsed_seconds(self) -> float:
        return time.time() - self.started_at


class RunnerState:
    """Everything the control surface can report, guarded by one lock.

    Mutated from the job thread; read from the server thread. Keep the
    critical sections trivial — no I/O, no model calls — so a `status`
    request can never be blocked behind real work.
    """

    def __init__(self, *, version: str, api_url: str) -> None:
        self._lock = threading.Lock()
        self._phase = "starting"
        self._job: JobView | None = None
        self._model_id: str | None = None
        self._paused = False
        self._exit_code: int | None = None
        self._exit_reason = ""
        self._completed = 0
        self._failed = 0
        self._started_at = time.time()
        self._version = version
        self._api_url = api_url
        # Subscribers live on the server's event loop; the job thread reaches
        # them only through `call_soon_threadsafe`.
        self._loop: asyncio.AbstractEventLoop | None = None
        self._subscribers: list[asyncio.Queue[dict[str, Any]]] = []
        # Plain callbacks, for consumers that live on a different loop
        # than the control socket's — the live channel (000289) runs its
        # own. Keeping them as calls rather than queues is what stops
        # RunnerState from having to know about more than one loop.
        self._listeners: list[Callable[[dict[str, Any]], None]] = []

    # -- reads

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            job = asdict(self._job) if self._job else None
            if job is not None and self._job is not None:
                job["elapsed_seconds"] = round(self._job.elapsed_seconds, 3)
            return {
                "phase": "paused" if self._paused and self._job is None else self._phase,
                "paused": self._paused,
                "job": job,
                "model_id": self._model_id,
                "completed": self._completed,
                "failed": self._failed,
                "uptime_seconds": round(time.time() - self._started_at, 3),
                "runner_version": self._version,
                "api_url": self._api_url,
                "pid": os.getpid(),
            }

    @property
    def paused(self) -> bool:
        with self._lock:
            return self._paused

    # -- mutations, from the job thread

    def set_phase(self, phase: str) -> None:
        with self._lock:
            self._phase = phase

    def model_downloading(self, model_id: str) -> None:
        """Weights are being fetched. Distinct from loading: one is minutes of
        network, the other is seconds of disk, and a watcher should be able to
        tell a slow download from a wedged runner."""
        with self._lock:
            self._phase = "downloading-model"
        self.emit("model.downloading", {"model_id": model_id})

    def model_loading(self, model_id: str) -> None:
        with self._lock:
            self._phase = "loading-model"
        self.emit("model.loading", {"model_id": model_id})

    def model_loaded(self, model_id: str) -> None:
        with self._lock:
            self._model_id = model_id
            self._phase = "idle"
        self.emit("model.loaded", {"model_id": model_id})

    def job_claimed(self, job_id: str, protocol_kind: str, model_id: str | None) -> None:
        with self._lock:
            self._job = JobView(id=job_id, protocol_kind=protocol_kind, model_id=model_id)
            self._phase = "executing"
        self.emit("job.claimed", {"id": job_id, "protocol_kind": protocol_kind,
                                  "model_id": model_id})

    def job_progress(self, done: int, total: int) -> None:
        with self._lock:
            if self._job is None:
                return
            self._job.done = done
            self._job.total = total
            job_id = self._job.id
        self.emit("job.progress", {"id": job_id, "done": done, "total": total})

    def job_finished(self, job_id: str) -> None:
        with self._lock:
            self._completed += 1
            self._job = None
            self._phase = "idle"
        self.emit("job.finished", {"id": job_id})

    def job_failed(self, job_id: str, message: str) -> None:
        with self._lock:
            self._failed += 1
            self._job = None
            self._phase = "idle"
        self.emit("job.failed", {"id": job_id, "message": message})

    def signed_out(self, message: str) -> None:
        """The API rejected this machine's credential (task 000284).

        Terminal, unlike every other failure in the poll loop: a revoked
        key does not start working again, and retrying it forever turns
        "you were signed out" into "the runner seems stuck". Anything
        watching the socket learns the reason at the same moment.
        """
        with self._lock:
            self._phase = "signed-out"
            self._job = None
        self.emit("runner.signed_out", {"message": message})

    def request_exit(self, code: int, reason: str) -> None:
        """Ask the poll loop to stop, with a specific exit code.

        The channel runs on its own thread and cannot exit the process
        itself — a thread that calls sys.exit only ends the thread, and
        killing the process outright would abandon whatever the job
        thread is doing. So it asks, and the loop decides when.

        The code is the point: 75 tells the supervisor to bring us
        straight back (task 000294), which is how an approved update
        gets a fresh process to install into.
        """
        with self._lock:
            self._exit_code = code
            self._exit_reason = reason
        self.emit("runner.exit_requested", {"code": code, "reason": reason})

    @property
    def exit_requested(self) -> tuple[int, str] | None:
        with self._lock:
            if self._exit_code is None:
                return None
            return self._exit_code, self._exit_reason

    def pause(self) -> None:
        """Stop claiming new work. Never interrupts a job in flight: a
        half-executed protocol emits nothing and throws away the minutes
        already spent loading a model."""
        with self._lock:
            self._paused = True
        self.emit("runner.paused", {})

    def resume(self) -> None:
        with self._lock:
            self._paused = False
        self.emit("runner.resumed", {})

    # -- events

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        with self._lock:
            self._loop = loop

    def add_subscriber(self) -> asyncio.Queue[dict[str, Any]]:
        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=EVENT_QUEUE_LIMIT)
        with self._lock:
            self._subscribers.append(q)
        return q

    def add_listener(self, fn: Callable[[dict[str, Any]], None]) -> None:
        """Call `fn` with every event, from whichever thread emitted it.

        The callback is responsible for its own thread-hopping and must
        not block: it runs on the job thread, between a model load and
        the next forward pass.
        """
        with self._lock:
            self._listeners.append(fn)

    def drop_listener(self, fn: Callable[[dict[str, Any]], None]) -> None:
        with self._lock:
            if fn in self._listeners:
                self._listeners.remove(fn)

    def drop_subscriber(self, q: asyncio.Queue[dict[str, Any]]) -> None:
        with self._lock:
            if q in self._subscribers:
                self._subscribers.remove(q)

    def emit(self, event: str, data: dict[str, Any]) -> None:
        """Fan an event out to subscribers. Called from the job thread."""
        with self._lock:
            loop, subs = self._loop, list(self._subscribers)
            listeners = list(self._listeners)
        message = {"v": PROTOCOL_VERSION, "event": event, "data": data}
        if loop is not None and subs:
            for q in subs:
                with suppress(RuntimeError):  # loop closed during shutdown
                    loop.call_soon_threadsafe(_offer, q, message)
        for fn in listeners:
            # A broken listener is a broken listener; it does not get to
            # take down the job that was only reporting its progress.
            with suppress(Exception):
                fn(message)


def _offer(q: asyncio.Queue[dict[str, Any]], message: dict[str, Any]) -> None:
    """Never block the job thread's loop on a subscriber that stopped reading;
    drop the oldest event instead, and keep the newest."""
    if q.full():
        with suppress(asyncio.QueueEmpty):
            q.get_nowait()
    with suppress(asyncio.QueueFull):
        q.put_nowait(message)


# --- server ------------------------------------------------------------------


class ControlServer:
    """Serves `RunnerState` on the control socket from a daemon thread."""

    def __init__(self, state: RunnerState, path: Path | None = None) -> None:
        self._state = state
        self._path = path or socket_path()
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._ready = threading.Event()
        self._error: BaseException | None = None
        self._server: asyncio.AbstractServer | None = None

    @property
    def path(self) -> Path:
        return self._path

    def start(self) -> None:
        # A Unix socket path is capped by the kernel — 104 bytes on macOS —
        # and the failure from inside asyncio names neither the limit nor the
        # path. `~/.mechbench/runner.sock` is nowhere near it, but a deep
        # temporary directory or an unusual home is, so say it plainly.
        encoded = len(str(self._path).encode())
        if encoded > 100:
            raise RuntimeError(
                f"the control socket path is {encoded} bytes, and the kernel "
                f"allows about 104: {self._path}"
            )
        self._thread = threading.Thread(target=self._run, name="control", daemon=True)
        self._thread.start()
        # Surface a bind failure to the caller rather than losing it in a
        # thread: without this, `run` would poll happily with no socket.
        if not self._ready.wait(timeout=5.0):
            if self._error is not None:
                raise RuntimeError(
                    f"could not open the control socket at {self._path}: {self._error}"
                ) from self._error
            raise RuntimeError(f"control socket did not come up at {self._path}")
        if self._error is not None:
            raise RuntimeError(
                f"could not open the control socket at {self._path}: {self._error}"
            ) from self._error

    def stop(self) -> None:
        """Close the socket and let in-flight handlers finish.

        Stopping the loop out from under a connection leaves a pending task
        that asyncio complains about at close; a subscriber attached at
        shutdown would produce that every time.
        """
        loop = self._loop
        if loop is not None:
            with suppress(RuntimeError):
                asyncio.run_coroutine_threadsafe(self._shutdown(), loop).result(timeout=2)
            with suppress(RuntimeError):
                loop.call_soon_threadsafe(loop.stop)
        if self._thread is not None:
            self._thread.join(timeout=2)
        with suppress(OSError):
            self._path.unlink()

    async def _shutdown(self) -> None:
        server = getattr(self, "_server", None)
        if server is not None:
            server.close()
            with suppress(Exception):
                await server.wait_closed()
        current = asyncio.current_task()
        pending = [t for t in asyncio.all_tasks() if t is not current]
        for t in pending:
            t.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    def _run(self) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        self._state.bind_loop(loop)
        try:
            loop.run_until_complete(self._listen())
            loop.run_forever()
        except BaseException as exc:  # noqa: BLE001 — handed to start()
            self._error = exc
            self._ready.set()
        finally:
            with suppress(Exception):
                loop.close()

    async def _listen(self) -> None:
        server = await asyncio.start_unix_server(self._handle, path=str(self._path))
        os.chmod(self._path, 0o600)
        self._ready.set()
        self._server = server

    async def _handle(self, reader: asyncio.StreamReader,
                      writer: asyncio.StreamWriter) -> None:
        subscription: asyncio.Queue[dict[str, Any]] | None = None
        pump: asyncio.Task[None] | None = None
        try:
            while True:
                line = await reader.readline()
                if not line:
                    return
                if len(line) > MAX_LINE_BYTES:
                    await _send(writer, _error("too_large", "request too large"))
                    return
                try:
                    request = json.loads(line)
                except json.JSONDecodeError:
                    await _send(writer, _error("bad_json", "request was not JSON"))
                    continue

                version = request.get("v")
                if version != PROTOCOL_VERSION:
                    await _send(writer, _error(
                        "version_mismatch",
                        f"this runner speaks control protocol v{PROTOCOL_VERSION}; "
                        f"the client sent v{version!r}. Update whichever is older.",
                    ))
                    continue

                op = request.get("op")
                if op == "status":
                    await _send(writer, _ok(self._state.snapshot()))
                elif op == "pause":
                    self._state.pause()
                    await _send(writer, _ok(self._state.snapshot()))
                elif op == "resume":
                    self._state.resume()
                    await _send(writer, _ok(self._state.snapshot()))
                elif op == "subscribe":
                    if subscription is not None:
                        await _send(writer, _error("already_subscribed",
                                                   "this connection is already a stream"))
                        continue
                    subscription = self._state.add_subscriber()
                    await _send(writer, _ok(self._state.snapshot()))
                    pump = asyncio.create_task(_pump(subscription, writer))
                else:
                    await _send(writer, _error("unknown_op", f"unknown op {op!r}"))
        except (ConnectionResetError, BrokenPipeError):
            return
        finally:
            if pump is not None:
                pump.cancel()
            if subscription is not None:
                self._state.drop_subscriber(subscription)
            with suppress(Exception):
                writer.close()


async def _pump(q: asyncio.Queue[dict[str, Any]],
                writer: asyncio.StreamWriter) -> None:
    with suppress(asyncio.CancelledError, ConnectionResetError, BrokenPipeError):
        while True:
            await _send(writer, await q.get())


async def _send(writer: asyncio.StreamWriter, message: dict[str, Any]) -> None:
    writer.write((json.dumps(message) + "\n").encode())
    await writer.drain()


def _ok(data: dict[str, Any]) -> dict[str, Any]:
    return {"v": PROTOCOL_VERSION, "ok": True, "data": data}


def _error(code: str, message: str) -> dict[str, Any]:
    return {"v": PROTOCOL_VERSION, "ok": False,
            "error": {"code": code, "message": message}}


# --- client ------------------------------------------------------------------


class ControlError(RuntimeError):
    pass


def request(op: str, *, path: Path | None = None, timeout: float = 5.0) -> dict[str, Any]:
    """Send one request to a running runner and return its `data`.

    Used by `mechbench status` and friends; the Mac app speaks the same
    protocol over the same socket.
    """
    p = path or socket_path()
    if not p.exists():
        raise ControlError(f"no runner is listening at {p} — start one with "
                           f"`mechbench run`")
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        try:
            s.connect(str(p))
        except (ConnectionRefusedError, FileNotFoundError) as exc:
            raise ControlError(
                f"the socket at {p} is stale — no runner is listening. "
                f"Starting one will replace it."
            ) from exc
        s.sendall((json.dumps({"v": PROTOCOL_VERSION, "op": op}) + "\n").encode())
        buf = b""
        while not buf.endswith(b"\n"):
            chunk = s.recv(65536)
            if not chunk:
                raise ControlError("runner closed the connection without replying")
            buf += chunk
            if len(buf) > MAX_LINE_BYTES:
                raise ControlError("reply was implausibly large")
    finally:
        s.close()

    reply = json.loads(buf)
    if not reply.get("ok"):
        err = reply.get("error") or {}
        raise ControlError(err.get("message") or "the runner refused the request")
    return reply.get("data") or {}


def probe(path: Path | None = None) -> dict[str, Any] | None:
    """Is a runner already listening here?

    Returns its status if one answers, None if nothing does — which also
    covers the socket file a crashed runner left behind, and is what lets a
    new runner claim the path instead of refusing to start.
    """
    p = path or socket_path()
    if not p.exists():
        return None
    try:
        return request("status", path=p, timeout=2.0)
    except (ControlError, OSError, json.JSONDecodeError):
        return None
