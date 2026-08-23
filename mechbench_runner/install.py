"""How this runner was installed, and therefore how to upgrade it.

`pip`, `pipx` and `uv tool` put the runner in different places and want
different upgrade commands. Guessing wrong does not fail cleanly — it
corrupts an environment — so when the answer is not clear this module
refuses and says what to run by hand. "Update available, run
`uv tool upgrade mechbench-runner`" is a fine outcome; a half-upgraded
venv on a machine nobody is watching is not.

A source checkout always refuses. That machine belongs to a developer
and `git pull` is theirs to run.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

DIST = "mechbench-runner"


#: Where installers actually live, for when PATH does not say.
#:
#: A LaunchAgent runs with PATH=/usr/bin:/bin:/usr/sbin:/sbin — no
#: Homebrew, no ~/.local/bin — so `shutil.which("uv")` returns None in
#: exactly the context that needs it, and the first real self-update
#: died on FileNotFoundError while the same command worked by hand.
#: A service cannot inherit a login shell's PATH, so it must not depend
#: on one.
_SEARCH = (
    "/opt/homebrew/bin",
    "/usr/local/bin",
    "~/.local/bin",
    "~/.cargo/bin",
    "/opt/local/bin",
)


def find_executable(name: str) -> str | None:
    """Locate `name` on PATH, or in the places tools are actually put."""
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
    """Where the runner lives and what would upgrade it."""

    #: "uv-tool" | "pipx" | "venv" | "source" | "unknown"
    method: str
    #: The command to run, or None when we refuse to guess.
    upgrade: list[str] | None
    #: Shown to a person when we cannot do it for them.
    advice: str

    @property
    def upgradable(self) -> bool:
        return self.upgrade is not None


def detect(prefix: str | None = None) -> Installation:
    """Describe this process's installation.

    `prefix` overrides which tree is inspected — for tests, and for
    asking about an environment that is not the current one. The
    editable check only applies when asking about ourselves, since it
    reads the running module.
    """
    root = Path(prefix or sys.prefix).resolve()
    parts = {p.lower() for p in root.parts}

    # An editable install points at a working tree; upgrading it would
    # fight with git.
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
        # `--reinstall` alone reinstalls the tool and leaves already
        # satisfied dependencies where they are: seen on 2026-08-23,
        # where mechbench-compute stayed a version behind a floor that
        # had already moved. `upgrade` is the one that means it.
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

    # A plain virtualenv: pip into *this* interpreter, never a pip that
    # happens to be on PATH and might belong to something else.
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
    """True when the code being run lives in a working tree.

    Asked of the *module*, not of the metadata. `direct_url.json` is the
    documented signal and would be tidier, but a stale
    `mechbench_runner.egg-info` left in a checkout shadows the real
    dist-info and answers with a distribution that has no such file —
    which is exactly the situation where getting this wrong would let an
    upgrade run over a developer's tree.

    Code under site-packages was installed. Anything else is a checkout.
    """
    import mechbench_runner

    here = Path(mechbench_runner.__file__ or "").resolve()
    return not any(
        part in {"site-packages", "dist-packages"} for part in here.parts
    )


def installed_versions() -> dict[str, str]:
    """What is actually installed, for the three packages that matter.

    Checked after an upgrade rather than trusting the command's exit
    code: an installer can succeed and still leave a dependency behind a
    floor that moved.
    """
    from importlib.metadata import PackageNotFoundError, version

    out: dict[str, str] = {}
    for name in (DIST, "mechbench-compute", "mechbench-schema"):
        try:
            out[name] = version(name)
        except PackageNotFoundError:
            out[name] = "(absent)"
    return out


def run_upgrade(
    install: Installation, target: str | None = None, timeout: float = 900.0
) -> tuple[bool, str]:
    """Perform the upgrade. Returns (ok, output-tail)."""
    if install.upgrade is None:
        return False, install.advice
    cmd = list(install.upgrade)
    if target and install.method == "venv":
        cmd[-1] = f"{DIST}=={target}"
    if target and install.method == "uv-tool":
        # `uv tool upgrade` respects the pin a tool was installed with
        # and will refuse to move; naming the version explicitly is what
        # actually changes it, and is also what a rollback needs.
        cmd = [cmd[0], "tool", "install", "--reinstall", f"{DIST}=={target}"]
    try:
        proc = subprocess.run(  # noqa: S603
            cmd, capture_output=True, text=True, timeout=timeout, check=False
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, str(exc)
    tail = ((proc.stdout or "") + (proc.stderr or "")).strip()[-800:]
    if proc.returncode != 0:
        return False, tail
    # Exit 0 does not mean anything moved. `uv tool upgrade` on a tool
    # installed with an exact pin prints "Nothing to upgrade" and exits
    # 0; so does pip when the requirement is already satisfied. The only
    # honest test is what is installed now.
    if target and installed_versions().get(DIST) != target:
        return False, (
            f"{DIST} is still "
            f"{installed_versions().get(DIST)} after upgrading; {tail}"
        )
    return True, tail
