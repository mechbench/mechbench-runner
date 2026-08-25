"""Where the runner keeps things on disk.

One directory, `~/.mechbench`, 0700. It holds the control socket (whose
permissions are its authentication) and the credentials file (which
holds a durable API key). Both reasons point at the same mode, so the
directory is created once, here, rather than by whoever gets there
first.
"""

from __future__ import annotations

from contextlib import suppress
from pathlib import Path

CONFIG_NAME = "config.toml"


def mechbench_dir() -> Path:
    """`~/.mechbench`, created 0700."""
    d = Path.home() / ".mechbench"
    d.mkdir(mode=0o700, parents=True, exist_ok=True)
    with suppress(OSError):  # pre-existing directories keep their mode
        d.chmod(0o700)
    return d


def config_path() -> Path:
    return mechbench_dir() / CONFIG_NAME


def checkpoints_dir() -> Path:
    """Where materialized bench checkpoints live. Not created here —
    only the materializer makes it, and an absent directory is simply
    an empty cache. Derived through `mechbench_dir` so the test fence
    over that one name covers this cache too."""
    return mechbench_dir() / "checkpoints"
