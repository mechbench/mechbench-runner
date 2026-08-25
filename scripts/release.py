#!/usr/bin/env python3
"""The release gate (task 000300): one command between HEAD and PyPI.

Seven bugs shipped in one day because a development environment differs
from a fresh install in exactly the ways that hide bugs: pinned deps,
exported env vars, editable installs, plaintext localhost APIs. Twice
since, a publish outran its own verification. This script is the
antidote to both: it will not upload until

  1. the test suite is green,
  2. the wheel builds,
  3. the wheel installs into a FRESH venv with dependencies resolved
     from the real index (floors get exercised, not inherited),
  4. and that venv passes the cheap smoke list from 000300 — CLI alive,
     modules importable, no-credential paths answer with sentences
     rather than tracebacks, and a real `wss://` dial to production
     that must fail with the server's POLICY close, not an SSL error
     (the certifi wiring that broke twice lives exactly there).

The smoke runs with HOME pointed at a scratch dir and every MECHBENCH_*
variable scrubbed — the env-vars-that-happened-to-be-there class.

Usage:
    python scripts/release.py              # gate, then upload
    python scripts/release.py --dry-run    # gate only
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
API_HOST = "api.mechbench.ai"


def run(cmd: list[str], *, env: dict | None = None, cwd: Path | None = None,
        timeout: float = 1800.0) -> subprocess.CompletedProcess:
    return subprocess.run(  # noqa: S603
        cmd, cwd=str(cwd or REPO), env=env, capture_output=True, text=True,
        timeout=timeout, check=False,
    )


def die(step: str, proc: subprocess.CompletedProcess | None = None) -> None:
    print(f"\nRELEASE BLOCKED at: {step}")
    if proc is not None:
        tail = ((proc.stdout or "") + (proc.stderr or "")).strip()
        print(tail[-2000:])
    sys.exit(1)


def main() -> None:
    dry = "--dry-run" in sys.argv

    version = re.search(
        r'^version = "([^"]+)"', (REPO / "pyproject.toml").read_text(), re.M
    )
    if not version:
        die("reading version from pyproject.toml")
    ver = version.group(1)
    print(f"gating mechbench {ver}")

    # 1. Tests. The suite is fenced (tests/conftest.py), so this cannot
    # touch the live machine.
    print("[1/5] pytest")
    proc = run([sys.executable, "-m", "pytest", "tests/", "-q"])
    if proc.returncode != 0:
        die("pytest", proc)

    # 2. Build.
    print("[2/5] build")
    run(["rm", "-rf", str(REPO / "dist")])
    proc = run(["uv", "build"])
    if proc.returncode != 0:
        die("uv build", proc)
    wheels = sorted((REPO / "dist").glob("mechbench-*.whl"))
    if not wheels:
        die("no wheel produced")
    wheel = wheels[-1]

    with tempfile.TemporaryDirectory(prefix="mechbench-gate-") as td:
        tmp = Path(td)
        venv = tmp / "venv"
        home = tmp / "home"
        home.mkdir()

        # 3. Fresh install: the wheel from disk, every DEPENDENCY from
        # the real index — an unbounded or wrong floor fails here, not
        # on a user's machine.
        print("[3/5] fresh venv install (deps from the real index)")
        proc = run(["uv", "venv", str(venv)])
        if proc.returncode != 0:
            die("uv venv", proc)
        proc = run(["uv", "pip", "install", "--refresh", "--python",
                    str(venv / "bin" / "python"), str(wheel)])
        if proc.returncode != 0:
            die("uv pip install", proc)

        py = str(venv / "bin" / "python")
        cli = str(venv / "bin" / "mechbench")
        # The scrubbed environment: no MECHBENCH_*, HOME with no
        # credentials — the fresh-machine shape.
        env = {k: v for k, v in os.environ.items()
               if not k.startswith("MECHBENCH_")}
        env["HOME"] = str(home)

        print("[4/5] smoke: cli, imports, credential-less paths")
        proc = run([cli, "--help"], env=env)
        if proc.returncode != 0 or "install-service" not in proc.stdout:
            die("mechbench --help", proc)
        # mcp_server is here by earned right: 0.1.0 shipped with an
        # unbounded mcp floor, every fresh install resolved 2.0, and
        # `mechbench mcp` raised ModuleNotFoundError (000298).
        proc = run([py, "-c",
                    "import mechbench.cli, mechbench_runner.job_runner, "
                    "mechbench_runner.channel, mechbench_runner.mcp_server, "
                    "mechbench_compute"], env=env)
        if proc.returncode != 0:
            die("module imports", proc)
        for sub in ("whoami", "models"):
            proc = run([cli, sub], env=env)
            out = (proc.stdout or "") + (proc.stderr or "")
            if "Traceback" in out:
                die(f"mechbench {sub} crashed without credentials", proc)

        # 5. Real TLS to production: the dial must fail with the
        # server's POLICY close (no key -> not a runner), never an SSL
        # error. Certifi wiring broke twice in exactly this spot.
        print(f"[5/5] real wss:// dial to {API_HOST}")
        # The server accepts the handshake (which proves the TLS
        # wiring — certifi in a fresh venv) and then delivers its
        # policy verdict as a 4401 close on the first exchange.
        snippet = (
            "import asyncio, ssl, certifi, websockets\n"
            "async def go():\n"
            "    ctx = ssl.create_default_context(cafile=certifi.where())\n"
            "    try:\n"
            f"        url = 'wss://{API_HOST}/runners/channel'\n"
            "        kw = dict(ssl=ctx, open_timeout=15)\n"
            "        async with websockets.connect(url, **kw) as ws:\n"
            "            try:\n"
            "                # the server states its objection, then closes 4401\n"
            "                for _ in range(5):\n"
            "                    await asyncio.wait_for(ws.recv(), timeout=20)\n"
            "                print('KEPT_TALKING')\n"
            "            except websockets.exceptions.ConnectionClosed as e:\n"
            "                code = getattr(e.rcvd, 'code', None)\n"
            "                ok = code == 4401\n"
            "                print('POLICY_REJECT' if ok else f'CLOSE_{code}')\n"
            "    except websockets.exceptions.InvalidStatus:\n"
            "        print('POLICY_REJECT')\n"
            "asyncio.run(go())\n"
        )
        proc = run([py, "-c", snippet], env=env, timeout=60)
        out = (proc.stdout or "") + (proc.stderr or "")
        if "POLICY_REJECT" not in out:
            die("wss dial: wanted a policy rejection over real TLS", proc)

    print(f"\ngate PASSED for {ver}")
    if dry:
        print("dry run — not uploading")
        return
    print("uploading…")
    proc = run(["uvx", "twine", "upload", f"dist/mechbench-{ver}*"],
               timeout=600)
    if proc.returncode != 0:
        die("twine upload", proc)
    print(f"published mechbench {ver}")


if __name__ == "__main__":
    main()
