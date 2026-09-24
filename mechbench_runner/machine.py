from __future__ import annotations

import platform
import socket
import sys


def hostname() -> str:
    try:
        return socket.gethostname() or "unknown-host"
    except OSError:
        return "unknown-host"


def default_name() -> str:
    host = hostname()
    for suffix in (".local", ".lan", ".home"):
        if host.endswith(suffix):
            host = host[: -len(suffix)]
            break
    return (host or "runner")[:80]


def describe_platform() -> str:
    v = sys.version_info
    return f"{sys.platform}/{platform.machine()} python {v.major}.{v.minor}.{v.micro}"
