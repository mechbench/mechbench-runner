from __future__ import annotations

from contextlib import suppress
from pathlib import Path

CONFIG_NAME = "config.toml"


def mechbench_dir() -> Path:
    d = Path.home() / ".mechbench"
    d.mkdir(mode=0o700, parents=True, exist_ok=True)
    with suppress(OSError):
        d.chmod(0o700)
    return d


def config_path() -> Path:
    return mechbench_dir() / CONFIG_NAME


def spool_dir() -> Path:
    d = mechbench_dir() / "spool"
    d.mkdir(mode=0o700, exist_ok=True)
    return d


def limits_path() -> Path:
    return mechbench_dir() / "limits.json"


def checkpoints_dir() -> Path:
    return mechbench_dir() / "checkpoints"
