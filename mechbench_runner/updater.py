"""Self-update, as a state machine on disk (task 000296).

**A process cannot safely replace its own running code**, so the upgrade
happens at *startup*, before any of the code it is about to replace has
been imported — not in a helper spawned on the way out, which would race
the supervisor's restart. At startup it is serialised by construction:
one process, and it has not loaded anything yet.

The state lives in `~/.mechbench/update.json` because it has to survive
the restart that does the work:

    requested  a person approved an update; upgrade, then verify
    verify     new code is running; check it, or roll back
    rollback   the new version failed; restore the old one

The worst thing this feature can produce is a machine that needed no
attention until it bricked itself, so every path out of a failure ends
with a runner that starts. An upgrade that cannot be verified is undone,
and one that fails twice stops trying and says so rather than looping
against the supervisor.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

from .paths import mechbench_dir

#: Two attempts. A third would be a loop, and a supervisor will restart
#: us as fast as we exit.
MAX_ATTEMPTS = 2


def state_path() -> Path:
    return mechbench_dir() / "update.json"


@dataclass
class UpdateState:
    stage: str
    target: str
    previous: str
    attempts: int = 0
    error: str | None = None

    def save(self, path: Path | None = None) -> None:
        p = path or state_path()
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(self), indent=2))
        os.replace(tmp, p)


def load(path: Path | None = None) -> UpdateState | None:
    p = path or state_path()
    try:
        data = json.loads(p.read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or "stage" not in data:
        return None
    try:
        return UpdateState(
            stage=str(data["stage"]),
            target=str(data.get("target") or ""),
            previous=str(data.get("previous") or ""),
            attempts=int(data.get("attempts") or 0),
            error=data.get("error"),
        )
    except (TypeError, ValueError):
        return None


def clear(path: Path | None = None) -> None:
    (path or state_path()).unlink(missing_ok=True)


def request(target: str, previous: str, path: Path | None = None) -> None:
    """Record an approved update. The next start performs it."""
    UpdateState(stage="requested", target=target, previous=previous).save(path)


# --- the startup hook --------------------------------------------------------


def take_pending_step(
    *,
    path: Path | None = None,
    report=None,
) -> bool:
    """Advance the update if one is in flight. True if we re-exec'd.

    Called before anything heavy is imported. `report` is an optional
    callback for a human-readable line; the channel is not up yet, so
    the log is the only place this can be said.
    """
    st = load(path)
    if st is None:
        return False

    say = report or (lambda msg: print(f"[update] {msg}"))

    if st.attempts >= MAX_ATTEMPTS:
        say(
            f"giving up on {st.target}: {st.attempts} attempts failed "
            f"({st.error or 'no detail'}). Staying on {st.previous}."
        )
        clear(path)
        return False

    from . import install as install_mod

    where = install_mod.detect()

    if st.stage == "requested":
        if not where.upgradable:
            say(f"cannot self-upgrade here — {where.advice}")
            clear(path)
            return False
        st.attempts += 1
        st.stage = "verify"
        st.save(path)
        say(f"upgrading {st.previous} -> {st.target} via {where.method}")
        ok, tail = install_mod.run_upgrade(where, st.target)
        if not ok:
            st.stage = "rollback"
            st.error = tail[-300:]
            st.save(path)
            say(f"upgrade failed; rolling back to {st.previous}")
        return _reexec(say)

    if st.stage == "verify":
        problems = _self_check()
        if not problems:
            say(f"now on {install_mod.installed_versions()}")
            clear(path)
            return False
        st.stage = "rollback"
        st.error = "; ".join(problems)[:300]
        st.save(path)
        say(f"{st.target} does not work here ({st.error}); rolling back")
        install_mod.run_upgrade(where, st.previous)
        return _reexec(say)

    if st.stage == "rollback":
        problems = _self_check()
        if problems:
            say(f"rolled back to {st.previous} and it still fails: {problems}")
        else:
            say(f"rolled back to {st.previous}; update to {st.target} abandoned")
        clear(path)
        return False

    clear(path)
    return False


def _self_check() -> list[str]:
    """Cheap proof that the code that just landed can actually run.

    Deliberately not `doctor`: this must not fail because a machine is
    offline or its disk is full — only because the *install* is broken.
    """
    problems: list[str] = []
    try:
        from . import (  # noqa: F401
            channel,
            cli,
            config,
            job_runner,  # noqa: F401
        )
    except Exception as exc:  # noqa: BLE001
        problems.append(f"import failed: {exc}")
    try:
        from .config import Config

        Config.from_env()
    except Exception as exc:  # noqa: BLE001
        problems.append(f"config failed: {exc}")
    return problems


def _reexec(say) -> bool:
    """Replace this process with the freshly installed code."""
    say("restarting into the new version")
    try:
        sys.stdout.flush()
        sys.stderr.flush()
    except Exception:  # noqa: BLE001
        pass
    os.execv(sys.executable, [sys.executable, "-m", "mechbench_runner.cli", *sys.argv[1:]])
    return True  # unreachable


# --- the manual path ---------------------------------------------------------


def update_now(report=None) -> int:
    """`mechbench-runner update` — upgrade this machine right now.

    The web UI route (approve, exit 75, upgrade at the next start) exists
    because a *service* cannot upgrade itself while running. Run by hand
    there is no such constraint: this process is not the one being
    supervised, so it can upgrade and then restart the service that is.
    """
    say = report or (lambda m: print(m))
    from . import install as install_mod

    where = install_mod.detect()
    if not where.upgradable:
        say(where.advice)
        return 1

    before = install_mod.installed_versions()
    say(f"upgrading via {where.method}…")
    ok, tail = install_mod.run_upgrade(where)
    after = install_mod.installed_versions()

    if not ok:
        say(f"upgrade failed: {tail}")
        return 1

    changed = {k: (before[k], v) for k, v in after.items() if before.get(k) != v}
    if not changed:
        say(f"already on {after.get(install_mod.DIST)}; nothing to do.")
        return 0
    for name, (was, now) in changed.items():
        say(f"  {name}: {was} -> {now}")

    from . import agent

    try:
        if agent.status().installed and agent.kickstart():
            say("Restarted the background service on the new version.")
    except agent.UnsupportedPlatformError:
        pass
    return 0
