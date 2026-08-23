"""Installing the runner as a service the operating system supervises.

This writes a launchd agent on macOS or a systemd **user** unit on
Linux, and then gets out of the way. It deliberately does not supervise
anything itself: both platforms already do that, and a second supervisor
underneath the first gives two processes with different ideas about what
"dead" means (task 000293).

What we are responsible for is telling the platform the right policy,
and the whole policy is the exit-code contract in `exits.py`:

* `KeepAlive{SuccessfulExit: false}` / `Restart=on-failure` — come back
  from a crash, stay stopped after a deliberate exit.
* `ThrottleInterval` / `RestartSec` — do not spin.

The command is `sys.executable -m mechbench_runner.cli run` rather than
whatever `mechbench-runner` resolves to on `PATH`. A service has no
shell profile, so `PATH` is not ours to rely on — and pinning the
interpreter pins the environment the runner was installed into, which
is the one holding its dependencies.

No credential is written into the unit. It comes from
`~/.mechbench/config.toml`, and a key in a plist is a key in a backup.
"""

from __future__ import annotations

import os
import plistlib
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from .paths import mechbench_dir

LABEL = "ai.mechbench.runner"
UNIT_NAME = "mechbench-runner.service"

#: Seconds launchd waits between restarts, and systemd's RestartSec.
THROTTLE_SECONDS = 10

#: Long enough for a job in flight to finish on SIGTERM before the
#: supervisor escalates. A layer-ablation sweep is a couple of minutes.
STOP_TIMEOUT_SECONDS = 300


class UnsupportedPlatformError(RuntimeError):
    pass


@dataclass(frozen=True)
class AgentStatus:
    installed: bool
    loaded: bool
    running: bool
    path: Path
    detail: str


def is_macos() -> bool:
    return sys.platform == "darwin"


def is_linux() -> bool:
    return sys.platform.startswith("linux")


def unit_path() -> Path:
    if is_macos():
        return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"
    if is_linux():
        return Path.home() / ".config" / "systemd" / "user" / UNIT_NAME
    raise UnsupportedPlatformError(
        f"there is no service manager we know how to use on {sys.platform}. "
        f"Run `mechbench-runner run` under whatever supervises processes "
        f"here, or use `mechbench-runner supervise` if there is nothing."
    )


def program_arguments() -> list[str]:
    return [sys.executable, "-m", "mechbench_runner.cli", "run"]


def boot_log() -> Path:
    """Where the platform's own stdout goes.

    Small by construction: the runner replaces stdout with its rotating
    log as soon as it starts, so only failures *before* that reach here —
    which is exactly what you want to read when it will not start.
    """
    d = mechbench_dir() / "logs"
    d.mkdir(mode=0o700, parents=True, exist_ok=True)
    return d / "agent-boot.log"


# --- writing the unit --------------------------------------------------------


def launchd_plist() -> dict[str, object]:
    return {
        "Label": LABEL,
        "ProgramArguments": program_arguments(),
        "RunAtLoad": True,
        # The exit contract, expressed the only way launchd understands:
        # restart unless the process said it meant to stop.
        "KeepAlive": {"SuccessfulExit": False},
        "ThrottleInterval": THROTTLE_SECONDS,
        "ExitTimeOut": STOP_TIMEOUT_SECONDS,
        # Background: lower priority than anything the user is looking at.
        "ProcessType": "Background",
        "StandardOutPath": str(boot_log()),
        "StandardErrorPath": str(boot_log()),
        "WorkingDirectory": str(Path.home()),
    }


def systemd_unit() -> str:
    args = " ".join(program_arguments())
    return f"""[Unit]
Description=mechbench runner
Documentation=https://mechbench.ai
After=network-online.target

[Service]
Type=simple
ExecStart={args}
# The exit contract (mechbench_runner/exits.py): 0 is deliberate and
# must stay stopped, anything else is a fault worth restarting.
Restart=on-failure
RestartSec={THROTTLE_SECONDS}
TimeoutStopSec={STOP_TIMEOUT_SECONDS}
# Give up loudly rather than spinning.
StartLimitBurst=5
StartLimitIntervalSec=600

[Install]
WantedBy=default.target
"""


# --- operations --------------------------------------------------------------


def install() -> AgentStatus:
    path = unit_path()
    path.parent.mkdir(parents=True, exist_ok=True)

    if is_macos():
        path.write_bytes(plistlib.dumps(launchd_plist()))
        # bootout first so install is idempotent: reinstalling over a
        # loaded agent otherwise keeps the old command running.
        _run(["launchctl", "bootout", _domain(), str(path)], check=False)
        result = _run(["launchctl", "bootstrap", _domain(), str(path)], check=False)
        if result.returncode != 0:
            # Older macOS, or a domain that refuses bootstrap.
            result = _run(["launchctl", "load", "-w", str(path)], check=False)
        if result.returncode != 0:
            return AgentStatus(True, False, False, path,
                               f"written, but could not be loaded: {_msg(result)}")
    else:
        path.write_text(systemd_unit())
        _run(["systemctl", "--user", "daemon-reload"], check=False)
        result = _run(["systemctl", "--user", "enable", "--now", UNIT_NAME],
                      check=False)
        if result.returncode != 0:
            return AgentStatus(True, False, False, path,
                               f"written, but could not be enabled: {_msg(result)}")

    return _settled_status()


def _settled_status(attempts: int = 10, pause: float = 0.3) -> AgentStatus:
    """Status once the service has had a moment to actually start.

    `bootstrap` and `enable --now` return before the process is up, so
    reading status immediately reports "loaded, not currently running" —
    which, printed the instant someone answers "yes, start it
    automatically", reads as a failure rather than as a race.
    """
    import time

    st = status()
    for _ in range(attempts):
        if st.running or not st.loaded:
            return st
        time.sleep(pause)
        st = status()
    return st


def uninstall() -> AgentStatus:
    path = unit_path()
    if is_macos():
        _run(["launchctl", "bootout", _domain(), str(path)], check=False)
        _run(["launchctl", "unload", "-w", str(path)], check=False)
    else:
        _run(["systemctl", "--user", "disable", "--now", UNIT_NAME], check=False)
    existed = path.exists()
    path.unlink(missing_ok=True)
    if is_linux():
        _run(["systemctl", "--user", "daemon-reload"], check=False)
    return AgentStatus(
        installed=False,
        loaded=False,
        running=False,
        path=path,
        detail="removed" if existed else "there was nothing installed",
    )


def status() -> AgentStatus:
    path = unit_path()
    if not path.exists():
        return AgentStatus(False, False, False, path, "not installed")

    if is_macos():
        printed = _run(["launchctl", "print", f"{_domain()}/{LABEL}"], check=False)
        loaded = printed.returncode == 0
        running = loaded and "state = running" in printed.stdout
        pid = _extract(printed.stdout, "pid = ")
        detail = (
            f"running (pid {pid})" if running
            else "loaded, not currently running" if loaded
            else "installed but not loaded"
        )
        return AgentStatus(True, loaded, running, path, detail)

    enabled = _run(["systemctl", "--user", "is-enabled", UNIT_NAME], check=False)
    active = _run(["systemctl", "--user", "is-active", UNIT_NAME], check=False)
    loaded = enabled.returncode == 0
    running = active.stdout.strip() == "active"
    return AgentStatus(
        True, loaded, running, path,
        f"{active.stdout.strip() or 'unknown'}, {enabled.stdout.strip() or 'disabled'}",
    )


def kickstart() -> bool:
    """Start it now, or restart it if it is already up.

    Needed after `login`: a runner that exited because it was signed out
    exited *deliberately*, so the supervisor is correctly leaving it
    alone and nothing else will bring it back.
    """
    if not unit_path().exists():
        return False
    if is_macos():
        r = _run(["launchctl", "kickstart", "-k", f"{_domain()}/{LABEL}"], check=False)
        return r.returncode == 0
    r = _run(["systemctl", "--user", "restart", UNIT_NAME], check=False)
    return r.returncode == 0


def linger_hint() -> str | None:
    """On Linux a user service stops at logout unless lingering is on —
    which is the difference between a headless box that works and one
    that works until you close the SSH session."""
    if not is_linux():
        return None
    user = os.environ.get("USER") or os.environ.get("LOGNAME") or ""
    check = _run(["loginctl", "show-user", user, "--property=Linger"], check=False)
    if "Linger=yes" in check.stdout:
        return None
    return (
        f"This service stops when you log out. To keep it running on a "
        f"headless machine:\n    sudo loginctl enable-linger {user}"
    )


# --- plumbing ----------------------------------------------------------------


def _domain() -> str:
    return f"gui/{os.getuid()}"


def _run(cmd: list[str], *, check: bool) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(  # noqa: S603
            cmd, capture_output=True, text=True, check=check, timeout=30
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return subprocess.CompletedProcess(cmd, 127, "", str(exc))


def _msg(result: subprocess.CompletedProcess[str]) -> str:
    return (result.stderr or result.stdout or "no output").strip().splitlines()[0]


def _extract(text: str, marker: str) -> str:
    for line in text.splitlines():
        if marker in line:
            return line.split(marker, 1)[1].strip().rstrip(";")
    return "?"
