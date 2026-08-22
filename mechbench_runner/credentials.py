"""Stored credentials: what `mechbench-runner login` writes down.

Before this, running a job meant `export MECHBENCH_API_KEY=mbk_...` —
which puts a durable secret in shell history, loses it between
terminals, and has to be pasted by hand from a settings page. For the
first thing a new user does, that is poor.

What replaces it is a file: `~/.mechbench/config.toml`, mode 0600 in a
0700 directory, written by the machine itself when it redeems a
registration token. The durable key is never displayed and never typed.

**Precedence.** `MECHBENCH_API_KEY` still wins, and when it is set the
environment owns the credential *entirely* — the stored file is ignored,
URL included. CI and containers have no place to put a config file, and
half-taking a credential from each source is how you end up sending a
production key to localhost.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

from .paths import config_path

TABLE = "runner"


@dataclass(frozen=True)
class StoredCredentials:
    """One account pairing, as it sits on disk."""

    api_url: str
    api_key: str
    runner_id: str | None = None
    name: str | None = None
    registered_at: str | None = None


def load(path: Path | None = None) -> StoredCredentials | None:
    """Read the stored pairing, or None if this machine has not logged in.

    A malformed or unreadable file reads as "not logged in" rather than
    raising: the runner should say "run `mechbench-runner login`", not
    hand someone a TOML parse error for a file they never wrote.
    """
    p = path or config_path()
    try:
        raw = p.read_bytes()
    except OSError:
        return None
    try:
        data = tomllib.loads(raw.decode("utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError):
        return None
    table = data.get(TABLE)
    if not isinstance(table, dict):
        return None
    api_url = table.get("api_url")
    api_key = table.get("api_key")
    if not isinstance(api_url, str) or not isinstance(api_key, str):
        return None
    if not api_url or not api_key:
        return None
    return StoredCredentials(
        api_url=api_url,
        api_key=api_key,
        runner_id=_opt_str(table.get("id")),
        name=_opt_str(table.get("name")),
        registered_at=_opt_str(table.get("registered_at")),
    )


def save(creds: StoredCredentials, path: Path | None = None) -> Path:
    """Write the pairing, 0600, replacing whatever was there.

    Written to a temporary file in the same directory and renamed, so a
    crash mid-write cannot leave a half-key behind — and created 0600
    from the start rather than chmod'ed afterwards, so the secret is
    never briefly world-readable.
    """
    p = path or config_path()
    tmp = p.with_name(p.name + ".tmp")
    body = _render(creds)
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(body)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    os.replace(tmp, p)
    os.chmod(p, 0o600)
    return p


def clear(path: Path | None = None) -> bool:
    """Forget the pairing. True if there was one to forget."""
    p = path or config_path()
    try:
        p.unlink()
        return True
    except FileNotFoundError:
        return False


def _render(creds: StoredCredentials) -> str:
    """Emit the flat table by hand.

    Python reads TOML in the standard library but does not write it, and
    this file is five string keys — not worth a dependency on every
    machine that installs the runner just to quote them.
    """
    lines = [
        "# mechbench-runner credentials, written by `mechbench-runner login`.",
        "# Holds a durable API key: keep this file mode 0600.",
        "",
        f"[{TABLE}]",
        f"api_url = {_toml_str(creds.api_url)}",
        f"api_key = {_toml_str(creds.api_key)}",
    ]
    for key, value in (
        ("id", creds.runner_id),
        ("name", creds.name),
        ("registered_at", creds.registered_at),
    ):
        if value:
            lines.append(f"{key} = {_toml_str(value)}")
    return "\n".join(lines) + "\n"


def _toml_str(value: str) -> str:
    """A TOML basic string. Escapes what the spec requires, and control
    characters, which is everything these values could plausibly hold."""
    out = []
    for ch in value:
        if ch == "\\":
            out.append("\\\\")
        elif ch == '"':
            out.append('\\"')
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\r":
            out.append("\\r")
        elif ch == "\t":
            out.append("\\t")
        elif ord(ch) < 0x20 or ord(ch) == 0x7F:
            out.append(f"\\u{ord(ch):04X}")
        else:
            out.append(ch)
    return '"' + "".join(out) + '"'


def _opt_str(value: object) -> str | None:
    return value if isinstance(value, str) and value else None
