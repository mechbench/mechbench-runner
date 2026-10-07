from __future__ import annotations

import os
import re
import threading
from collections.abc import Iterable, Mapping
from typing import Any

MASK = "[redacted]"
MIN_SECRET_CHARS = 8

SECRET_NAME = re.compile(r"KEY|TOKEN(?!IZERS)|SECRET|PASSWORD|PASSWD|CREDENTIAL", re.I)
KEEP_NAMES = re.compile(r"^UV_")

PATTERNS = (
    re.compile(r"\bhf_[A-Za-z0-9]{8,}"),
    re.compile(r"\bmb[a-z]_[A-Za-z0-9_-]{8,}"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}"),
    re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr|github_pat)_[A-Za-z0-9_]{16,}"),
    re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{10,}"),
    re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_-]{30,}"),
    re.compile(r"(?i)(\bbearer\s+)[A-Za-z0-9._~+/=-]{8,}"),
)

_lock = threading.Lock()
_process: set[str] = set()


def is_secret_name(name: str) -> bool:
    return bool(SECRET_NAME.search(name)) and not KEEP_NAMES.match(name)


def remember(*values: Any) -> None:
    found = strings_in(values)
    with _lock:
        _process.update(found)


def strings_in(value: Any) -> set[str]:
    out: set[str] = set()
    if isinstance(value, str):
        if len(value) >= MIN_SECRET_CHARS:
            out.add(value)
    elif isinstance(value, Mapping):
        for v in value.values():
            out |= strings_in(v)
    elif isinstance(value, (list, tuple, set, frozenset)):
        for v in value:
            out |= strings_in(v)
    return out


def _env_values() -> set[str]:
    return {v for k, v in os.environ.items()
            if is_secret_name(k) and len(v) >= MIN_SECRET_CHARS}


def redact(text: Any, extra: Iterable[str] | Any = ()) -> str:
    out = str(text)
    with _lock:
        known = set(_process)
    known |= _env_values() | strings_in(extra)
    for value in sorted(known, key=len, reverse=True):
        if value in out:
            out = out.replace(value, MASK)
    for pattern in PATTERNS:
        if pattern.groups:
            out = pattern.sub(lambda m: m.group(1) + MASK, out)
        else:
            out = pattern.sub(MASK, out)
    return out


def child_env(base: Mapping[str, str] | None = None) -> dict[str, str]:
    source = os.environ if base is None else base
    return {k: v for k, v in source.items() if not is_secret_name(k)}
