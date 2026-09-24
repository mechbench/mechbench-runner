#!/usr/bin/env python3

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


REQUIRED_HEADINGS = (
    "### Changes that raise",
    "### Changes that alter results without raising",
)


def check_changelog(version: str) -> str | None:
    path = REPO / "CHANGELOG.md"
    if not path.exists():
        return "CHANGELOG.md is missing"
    text = path.read_text()
    marker = f"## {version}"
    if marker not in text:
        return (f"CHANGELOG.md has no entry for {version}. Add one with "
                f"both headings before releasing.")
    start = text.index(marker)
    nxt = text.find("\n## ", start + 1)
    entry = text[start:] if nxt == -1 else text[start:nxt]
    missing = [h for h in REQUIRED_HEADINGS if h not in entry]
    if missing:
        return (f"{version}'s entry is missing {', '.join(missing)!r}. "
                f"An empty list is written `_None._`, not omitted.")
    return None


def main() -> None:
    dry = "--dry-run" in sys.argv

    version = re.search(
        r'^version = "([^"]+)"', (REPO / "pyproject.toml").read_text(), re.M
    )
    if not version:
        die("reading version from pyproject.toml")
    ver = version.group(1)
    print(f"gating mechbench {ver}")

    print("[1/5] release notes")
    problem = check_changelog(ver)
    if problem:
        die(f"release notes: {problem}")

    print("[2/5] pytest")
    proc = run([sys.executable, "-m", "pytest", "tests/", "-q"])
    if proc.returncode != 0:
        die("pytest", proc)

    print("[3/5] build")
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

        print("[4/5] fresh venv install (deps from the real index)")
        proc = run(["uv", "venv", str(venv)])
        if proc.returncode != 0:
            die("uv venv", proc)
        proc = run(["uv", "pip", "install", "--refresh", "--python",
                    str(venv / "bin" / "python"), str(wheel)])
        if proc.returncode != 0:
            die("uv pip install", proc)

        py = str(venv / "bin" / "python")
        cli = str(venv / "bin" / "mechbench")
        env = {k: v for k, v in os.environ.items()
               if not k.startswith("MECHBENCH_")}
        env["HOME"] = str(home)

        print("[5/5] smoke: cli, imports, credential-less paths")
        proc = run([cli, "--help"], env=env)
        if proc.returncode != 0 or "install-service" not in proc.stdout:
            die("mechbench --help", proc)
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

        print(f"[6/6] real wss:// dial to {API_HOST}")
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
