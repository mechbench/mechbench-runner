from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

from .paths import config_path

TABLE = "runner"


@dataclass(frozen=True)
class StoredCredentials:
    api_url: str
    api_key: str
    runner_id: str | None = None
    name: str | None = None
    registered_at: str | None = None


def load(path: Path | None = None) -> StoredCredentials | None:
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
    p = path or config_path()
    try:
        p.unlink()
        return True
    except FileNotFoundError:
        return False


def _render(creds: StoredCredentials) -> str:
    lines = [
        "# mechbench credentials, written by `mechbench login`.",
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
