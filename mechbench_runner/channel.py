from __future__ import annotations

import asyncio
import json
import random
import threading
from collections import OrderedDict
from contextlib import suppress
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import ssl

from .config import Config
from .control import RunnerState
from .exits import EXIT_RESTART

PROTOCOL_VERSION = 1

BACKOFF_MIN_SECONDS = 1.0
BACKOFF_MAX_SECONDS = 30.0

SILENCE_TIMEOUT_SECONDS = 90.0

COMMAND_MEMORY = 64


def channel_url(api_base_url: str) -> str:
    base = api_base_url.rstrip("/")
    if base.startswith("https://"):
        base = "wss://" + base[len("https://") :]
    elif base.startswith("http://"):
        base = "ws://" + base[len("http://") :]
    return f"{base}/runners/channel"


def _ssl_context(url: str) -> ssl.SSLContext | None:
    if not url.startswith("wss://"):
        return None
    import ssl

    import certifi

    return ssl.create_default_context(cafile=certifi.where())


class LiveChannel:
    def __init__(self, config: Config, state: RunnerState) -> None:
        self.config = config
        self.state = state
        self.url = channel_url(config.api_base_url)
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._outbox: asyncio.Queue[dict[str, Any]] | None = None
        self._stop = threading.Event()
        self._connected = threading.Event()
        self._applied: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self._listener_registered = False
        self._stopped = False
        self._ws: Any = None

    def start(self) -> None:
        if not self.config.api_key:
            return
        self.state.add_listener(self._on_event)
        self._listener_registered = True
        self._thread = threading.Thread(
            target=self._run, name="live-channel", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        if self._stopped:
            return
        self._stopped = True
        self._stop.set()
        if self._listener_registered:
            self.state.drop_listener(self._on_event)
            self._listener_registered = False

        loop = self._loop
        if loop is not None and not loop.is_closed():
            with suppress(Exception):
                asyncio.run_coroutine_threadsafe(self._close_now(), loop).result(
                    timeout=2
                )
        if self._thread is not None:
            self._thread.join(timeout=3)
            if self._thread.is_alive() and loop is not None and not loop.is_closed():
                with suppress(RuntimeError):
                    loop.call_soon_threadsafe(loop.stop)
                self._thread.join(timeout=2)
            self._thread = None

    async def _close_now(self) -> None:
        ws = self._ws
        if ws is not None:
            with suppress(Exception):
                await ws.close()

    @property
    def connected(self) -> bool:
        return self._connected.is_set()

    def _on_event(self, message: dict[str, Any]) -> None:
        loop, outbox = self._loop, self._outbox
        if loop is None or outbox is None or not self._connected.is_set():
            return
        frame = {
            "v": PROTOCOL_VERSION,
            "type": "event",
            "event": message.get("event"),
            "data": message.get("data") or {},
        }
        with suppress(RuntimeError):
            loop.call_soon_threadsafe(_offer, outbox, frame)

    def _run(self) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._reconnect_forever())
        except Exception:  # noqa: BLE001
            pass
        finally:
            with suppress(Exception):
                loop.close()

    async def _reconnect_forever(self) -> None:
        self._outbox = asyncio.Queue(maxsize=256)
        backoff = BACKOFF_MIN_SECONDS
        while not self._stop.is_set():
            try:
                await self._session()
                backoff = BACKOFF_MIN_SECONDS
            except Exception as exc:  # noqa: BLE001
                if self._stop.is_set():
                    return
                print(f"[channel] {self.url} unavailable ({exc}); "
                      f"retrying in {backoff:.0f}s")
            finally:
                self._connected.clear()
            if self._stop.is_set():
                return
            await asyncio.sleep(backoff * (0.5 + random.random()))  # noqa: S311
            backoff = min(backoff * 2, BACKOFF_MAX_SECONDS)

    async def _session(self) -> None:
        import websockets

        key = self.config.require_api_key()
        async with websockets.connect(
            self.url,
            additional_headers={"authorization": f"Bearer {key}"},
            ssl=_ssl_context(self.url),
            open_timeout=15,
            close_timeout=5,
            ping_interval=None,
        ) as ws:
            self._ws = ws
            await ws.send(json.dumps(self._hello()))
            self._connected.set()
            print(f"[channel] connected to {self.url}")
            sender = asyncio.create_task(self._send_loop(ws))
            try:
                await self._receive_loop(ws)
            finally:
                sender.cancel()
                with_suppressed(sender)
                self._ws = None

    def _hello(self) -> dict[str, Any]:
        from . import machine

        try:
            from . import __version__ as runner_version
        except ImportError:
            runner_version = "unknown"
        from . import install as install_mod

        return {
            "v": PROTOCOL_VERSION,
            "type": "hello",
            "runnerVersion": runner_version,
            "platform": machine.describe_platform(),
            "hostname": machine.hostname(),
            "packages": install_mod.installed_versions(),
            "installMethod": install_mod.detect().method,
            "selfUpdatable": install_mod.detect().upgradable,
        }

    async def _send_loop(self, ws: Any) -> None:
        assert self._outbox is not None
        while True:
            frame = await self._outbox.get()
            await ws.send(json.dumps(frame))

    async def _receive_loop(self, ws: Any) -> None:
        while not self._stop.is_set():
            try:
                raw = await asyncio.wait_for(
                    ws.recv(), timeout=SILENCE_TIMEOUT_SECONDS
                )
            except TimeoutError as exc:
                raise ConnectionError(
                    f"no frame in {SILENCE_TIMEOUT_SECONDS:.0f}s"
                ) from exc
            try:
                frame = json.loads(raw)
            except (TypeError, ValueError):
                continue
            if frame.get("v") != PROTOCOL_VERSION:
                raise ConnectionError(
                    f"this runner speaks channel protocol v{PROTOCOL_VERSION}, "
                    f"the API speaks v{frame.get('v')}"
                )
            await self._handle(ws, frame)

    async def _handle(self, ws: Any, frame: dict[str, Any]) -> None:
        kind = frame.get("type")
        if kind == "ping":
            await ws.send(json.dumps({"v": PROTOCOL_VERSION, "type": "pong"}))
            return
        if kind == "welcome":
            return
        if kind == "error":
            print(f"[channel] {frame.get('code')}: {frame.get('message')}")
            return
        if kind == "command":
            await self._run_command(ws, frame)

    async def _run_command(self, ws: Any, frame: dict[str, Any]) -> None:
        cmd_id = frame.get("id")
        name = frame.get("name")
        if not isinstance(cmd_id, str) or not isinstance(name, str):
            return

        remembered = self._applied.get(cmd_id)
        if remembered is not None:
            await ws.send(json.dumps({**remembered, "id": cmd_id}))
            return

        ack: dict[str, Any] = {"v": PROTOCOL_VERSION, "type": "ack", "ok": True}
        if name == "update":
            ack = self._accept_update(frame)
        elif name == "pause":
            self.state.pause()
        elif name == "resume":
            self.state.resume()
        elif name == "status":
            pass
        else:
            ack = {
                "v": PROTOCOL_VERSION,
                "type": "ack",
                "ok": False,
                "error": f"this runner does not know the command {name!r}",
            }
        if ack["ok"]:
            ack["state"] = self.state.snapshot()

        self._remember(cmd_id, ack)
        await ws.send(json.dumps({**ack, "id": cmd_id}))

    def _accept_update(self, frame: dict[str, Any]) -> dict[str, Any]:
        target = (frame.get("args") or {}).get("version")
        if not isinstance(target, str) or not target:
            return {"v": PROTOCOL_VERSION, "type": "ack", "ok": False,
                    "error": "update needs args.version"}

        snap = self.state.snapshot()
        if snap.get("job") is not None:
            return {"v": PROTOCOL_VERSION, "type": "ack", "ok": False,
                    "error": "busy: a job is in flight; ask again when idle"}

        from . import install as install_mod
        from . import updater

        where = install_mod.detect()
        if not where.upgradable:
            return {"v": PROTOCOL_VERSION, "type": "ack", "ok": False,
                    "error": where.advice}

        try:
            from . import __version__ as current
        except ImportError:
            current = ""
        updater.request(target, current)
        self.state.set_phase("updating")
        self.state.request_exit(EXIT_RESTART, f"update to {target}")
        return {"v": PROTOCOL_VERSION, "type": "ack", "ok": True,
                "state": {"updating_to": target, "from": current}}

    def _remember(self, cmd_id: str, ack: dict[str, Any]) -> None:
        self._applied[cmd_id] = ack
        while len(self._applied) > COMMAND_MEMORY:
            self._applied.popitem(last=False)


def _offer(queue: asyncio.Queue[dict[str, Any]], frame: dict[str, Any]) -> None:
    if queue.full():
        with suppress(asyncio.QueueEmpty):
            queue.get_nowait()
    with suppress(asyncio.QueueFull):
        queue.put_nowait(frame)


def with_suppressed(task: asyncio.Task[Any]) -> None:
    task.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)
