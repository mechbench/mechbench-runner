from __future__ import annotations

import base64
import hashlib
import subprocess
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from mechbench_runner import release_manifest

KEY = Ed25519PrivateKey.generate()
PUB = KEY.public_key()

RUNNER_WHEEL = b"the runner wheel"
COMPUTE_WHEEL = b"the compute wheel"
FILES = "https://files.pythonhosted.org/packages/ab/cd"
RUNNER_URL = f"{FILES}/mechbench-0.53.0-py3-none-any.whl"
COMPUTE_URL = f"{FILES}/mechbench_compute-0.175.0-py3-none-any.whl"
MLX_MARKER = "platform_machine == 'arm64' and sys_platform == 'darwin'"


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def unsigned(**over: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "schema": 1,
        "runner": {"version": "0.53.0",
                   "wheel": {"url": RUNNER_URL, "sha256": sha(RUNNER_WHEEL)}},
        "compute": {"version": "0.175.0",
                    "wheel": {"url": COMPUTE_URL, "sha256": sha(COMPUTE_WHEEL)}},
        "locked": [
            {"name": "mlx", "version": "0.32.3", "marker": MLX_MARKER,
             "sha256": ["c" * 64]},
            {"name": "httpx", "version": "0.28.1", "sha256": ["a" * 64, "b" * 64]},
        ],
        "allow_downgrade": False,
        "published_at": "2026-09-30T20:00:00Z",
    }
    body.update(over)
    return body


def sign(body: dict[str, Any], key: Ed25519PrivateKey = KEY) -> dict[str, Any]:
    sig = key.sign(release_manifest.canonical(body))
    return {**body, "signature": base64.b64encode(sig).decode()}


def signed(**over: Any) -> dict[str, Any]:
    return sign(unsigned(**over))


def wheels(url: str) -> bytes:
    return {RUNNER_URL: RUNNER_WHEEL, COMPUTE_URL: COMPUTE_WHEEL}[url]


class Recorder:
    def __init__(self, fail: set[str] | None = None) -> None:
        self.calls: list[list[str]] = []
        self.fail = fail or set()

    def __call__(self, cmd: list[str], *,
                 timeout: float) -> subprocess.CompletedProcess:
        self.calls.append(list(cmd))
        code = 1 if any(f in " ".join(cmd) for f in self.fail) else 0
        return subprocess.CompletedProcess(cmd, code, "", "failed" if code else "")
