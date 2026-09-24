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
    pass


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

    for mod in (paths, service, updater, control, logs):
        monkeypatch.setattr(mod, "mechbench_dir", fenced_dir)
    monkeypatch.setattr(
        credentials, "config_path", lambda: fenced_dir() / paths.CONFIG_NAME
    )

    def _local(url: str) -> bool:
        return "127.0.0.1" in url or "localhost" in url

    real_ws_connect = websockets.connect

    def fenced_ws_connect(url, *a, **kw):
        if not _local(url):
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
