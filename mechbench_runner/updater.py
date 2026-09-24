from __future__ import annotations

import json
import os
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

from .paths import mechbench_dir

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
    UpdateState(stage="requested", target=target, previous=previous).save(path)


def take_pending_step(
    *,
    path: Path | None = None,
    report=None,
) -> bool:
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
    problems: list[str] = []
    try:
        from mechbench import cli  # noqa: F401

        from . import (  # noqa: F401
            channel,
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
    say("restarting into the new version")
    try:
        sys.stdout.flush()
        sys.stderr.flush()
    except Exception:  # noqa: BLE001
        pass
    os.execv(sys.executable, [sys.executable, "-m", "mechbench.cli", *sys.argv[1:]])
    return True


def update_now(report=None) -> int:
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
        _restart_if_stale(after, say)
        return 0
    for name, (was, now) in changed.items():
        say(f"  {name}: {was} -> {now}")

    from . import service

    try:
        if service.status().installed and service.kickstart():
            say("Restarted the background service on the new version.")
    except service.UnsupportedPlatformError:
        pass
    return 0


def _restart_if_stale(installed: dict, say) -> None:
    from . import install as install_mod
    from . import service

    try:
        st = service.status()
    except service.UnsupportedPlatformError:
        return
    if not (st.installed and st.running):
        return
    try:
        from .control import request

        running = str(request("status").get("runner_version") or "")
    except Exception:  # noqa: BLE001
        return
    disk = installed.get(install_mod.DIST) or ""
    if running and disk and running != disk:
        if service.kickstart():
            say(f"running service was v{running} while the disk has "
                f"v{disk}; restarted it onto the current code.")
        else:
            say(f"running service is v{running} but the disk has v{disk} "
                f"— restart it: mechbench uninstall-service && "
                f"mechbench install-service")

