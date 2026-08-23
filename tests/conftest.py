"""The fence between this suite and the machine running it (task 000306).

On 2026-08-23 the full suite restarted the LIVE runner service on the
development machine six times in ten minutes. The updater tests mocked
the upgrade itself but not `service.kickstart()`, so `update_now()`
reached the real `launchctl kickstart -k`. Nothing failed; the only
symptom was launchd's run counter climbing on a machine that happened
to have a runner installed — and the diagnosis took hours because the
supervisor's shutdown grace delays the restart ~30s past the test run
that caused it, so before/after measurements straddling a suite run
looked clean.

So: every path from a test to the live machine is closed HERE, by
default, for every test — not in the tests that remembered. A test that
wants a subprocess boundary opts in by stubbing over these refusals
(monkeypatch in a later-applied fixture wins).

Three doors are fenced:

* `service._run` — every launchctl/systemctl invocation goes through it.
* `install._run` — every package-manager invocation (`uv tool upgrade`
  can rebuild the real tool install, which is its own kind of live).
* `paths.mechbench_dir` — the real `~/.mechbench` holds a live control
  socket and real credentials; tests get a per-test temporary one.
  Modules import it by value, so it is patched in each importer.
* the network — the same sweep that found the launchctl leak found a
  test dialling wss://api.mechbench.ai with a made-up key on every run
  (test_signed_out hardcoded the production URL). Loopback is allowed,
  everything else is refused at the dial.
"""

from __future__ import annotations

import pytest
import websockets

from mechbench_runner import (
    api_client,
    control,
    credentials,
    install,
    logs,
    paths,
    service,
    updater,
)


class LiveSystemTouchedError(AssertionError):
    """A test reached for the real machine. The fix is a stub, never a
    broader permission."""


@pytest.fixture(autouse=True)
def _fenced_machine(monkeypatch, tmp_path):
    def refuse_service(cmd, *, check=False):  # noqa: ARG001
        raise LiveSystemTouchedError(
            f"launchctl/systemctl reached from a test: {cmd!r}. "
            "Stub service._run (see fake_launchctl in test_service.py)."
        )

    def refuse_install(cmd, **_kw):
        raise LiveSystemTouchedError(
            f"package manager reached from a test: {cmd!r}. Stub install._run."
        )

    monkeypatch.setattr(service, "_run", refuse_service)
    monkeypatch.setattr(install, "_run", refuse_install)

    home = tmp_path / "mechbench-home"

    def fenced_dir():
        home.mkdir(mode=0o700, parents=True, exist_ok=True)
        return home

    # By-value imports: the name must be patched where it was imported to.
    for mod in (paths, service, updater, control, logs):
        monkeypatch.setattr(mod, "mechbench_dir", fenced_dir)
    monkeypatch.setattr(
        credentials, "config_path", lambda: fenced_dir() / paths.CONFIG_NAME
    )

    def _local(url: str) -> bool:
        return "127.0.0.1" in url or "localhost" in url

    # channel.py imports websockets lazily inside the dial, so the
    # patch goes on the websockets module itself.
    real_ws_connect = websockets.connect

    def fenced_ws_connect(url, *a, **kw):
        if not _local(url):
            # Raised inside the channel's own thread, where its retry
            # loop will swallow it — the point is containment, not a
            # test failure. Nothing leaves this machine.
            raise LiveSystemTouchedError(f"WSS to a non-local host from a test: {url}")
        return real_ws_connect(url, *a, **kw)

    monkeypatch.setattr(websockets, "connect", fenced_ws_connect)

    real_httpx_request = api_client.httpx.request
    real_httpx_post = api_client.httpx.post

    def fenced_request(method, url, *a, **kw):
        if not _local(str(url)):
            raise LiveSystemTouchedError(f"HTTP to a non-local host from a test: {url}")
        return real_httpx_request(method, url, *a, **kw)

    def fenced_post(url, *a, **kw):
        if not _local(str(url)):
            raise LiveSystemTouchedError(f"HTTP to a non-local host from a test: {url}")
        return real_httpx_post(url, *a, **kw)

    monkeypatch.setattr(api_client.httpx, "request", fenced_request)
    monkeypatch.setattr(api_client.httpx, "post", fenced_post)
