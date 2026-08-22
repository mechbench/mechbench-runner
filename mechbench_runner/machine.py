"""What this machine calls itself.

Deliberately stdlib-only and deliberately *not* `mechbench_compute`'s
`describe_platform`. The two answer different questions that happen to
render alike: this is the runner's own identity — the OS, the
architecture, the interpreter it is running under — and it has to be
answerable on a machine with no compute backend at all, which is
exactly the machine `login` most needs to work on. (Importing
`mechbench_compute` there raises on purpose.)

The substrate question — which backends exist, how much memory, which
models are cached — is a *capability*, belongs to the registry record,
and arrives with tasks 000285 and 000290.
"""

from __future__ import annotations

import platform
import socket
import sys


def hostname() -> str:
    """As the machine reports itself, unedited."""
    try:
        return socket.gethostname() or "unknown-host"
    except OSError:
        return "unknown-host"


def default_name() -> str:
    """A first guess at a display name, which the owner can change later.

    Trims the `.local` mDNS suffix macOS appends: "studio" is a name a
    person recognizes in a list, "studio.local" is a network artifact.
    """
    host = hostname()
    for suffix in (".local", ".lan", ".home"):
        if host.endswith(suffix):
            host = host[: -len(suffix)]
            break
    return (host or "runner")[:80]


def describe_platform() -> str:
    """`darwin/arm64 python 3.11.1` — short enough to sit in a table."""
    v = sys.version_info
    return f"{sys.platform}/{platform.machine()} python {v.major}.{v.minor}.{v.micro}"
