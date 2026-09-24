from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

DIST = "mechbench"


_SEARCH = (
    "/opt/homebrew/bin",
    "/usr/local/bin",
    "~/.local/bin",
    "~/.cargo/bin",
    "/opt/local/bin",
)


def find_executable(name: str) -> str | None:
    found = shutil.which(name)
    if found:
        return found
    for d in _SEARCH:
        candidate = Path(d).expanduser() / name
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


@dataclass(frozen=True)
class Installation:
    method: str
    upgrade: list[str] | None
    advice: str

    @property
    def upgradable(self) -> bool:
        return self.upgrade is not None


def detect(prefix: str | None = None) -> Installation:
    root = Path(prefix or sys.prefix).resolve()
    parts = {p.lower() for p in root.parts}

    if prefix is None and _is_editable():
        return Installation(
            "source", None,
            "This is an editable install from a source checkout — "
            "`git pull` and reinstall instead.",
        )

    if "uv" in parts and "tools" in parts:
        uv = find_executable("uv")
        if uv is None:
            return Installation(
                "uv-tool", None,
                "This is a uv tool install but `uv` cannot be found from "
                "here — a background service does not inherit your shell's "
                f"PATH. Run: uv tool upgrade {DIST}",
            )
        return Installation(
            "uv-tool", [uv, "tool", "upgrade", DIST],
            f"Run: uv tool upgrade {DIST}",
        )

    if "pipx" in parts:
        pipx = find_executable("pipx")
        if pipx is None:
            return Installation(
                "pipx", None,
                "This is a pipx install but `pipx` cannot be found from "
                f"here. Run: pipx upgrade {DIST}",
            )
        return Installation(
            "pipx", [pipx, "upgrade", DIST], f"Run: pipx upgrade {DIST}",
        )

    if (root / "pyvenv.cfg").exists():
        return Installation(
            "venv",
            [sys.executable, "-m", "pip", "install", "--upgrade", DIST],
            f"Run: {sys.executable} -m pip install --upgrade {DIST}",
        )

    return Installation(
        "unknown", None,
        f"Could not tell how {DIST} was installed ({root}), so it will not "
        f"guess. Upgrade it the way you installed it.",
    )


def _is_editable() -> bool:
    import mechbench_runner

    here = Path(mechbench_runner.__file__ or "").resolve()
    return not any(
        part in {"site-packages", "dist-packages"} for part in here.parts
    )


def installed_versions() -> dict[str, str]:
    from importlib.metadata import PackageNotFoundError, version

    out: dict[str, str] = {}
    for name in (DIST, "mechbench-compute", "mechbench-schema"):
        try:
            out[name] = version(name)
        except PackageNotFoundError:
            out[name] = "(absent)"
    return out


def _run(cmd: list[str], *, timeout: float) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603
        cmd, capture_output=True, text=True, timeout=timeout, check=False
    )


def run_upgrade(
    install: Installation, target: str | None = None, timeout: float = 900.0
) -> tuple[bool, str]:
    if install.upgrade is None:
        return False, install.advice
    cmd = list(install.upgrade)
    if target and install.method == "venv":
        cmd[-1] = f"{DIST}=={target}"
    if target and install.method == "uv-tool":
        cmd = [cmd[0], "tool", "install", "--reinstall", f"{DIST}=={target}"]
    if not target and install.method == "uv-tool":
        cmd = [cmd[0], "tool", "install", "--upgrade", "--refresh", DIST]
    try:
        proc = _run(cmd, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        return False, str(exc)
    tail = ((proc.stdout or "") + (proc.stderr or "")).strip()[-800:]
    if proc.returncode != 0:
        return False, tail
    if target and installed_versions().get(DIST) != target:
        return False, (
            f"{DIST} is still "
            f"{installed_versions().get(DIST)} after upgrading; {tail}"
        )
    return True, tail
