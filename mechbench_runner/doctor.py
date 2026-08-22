"""`mechbench-runner doctor` — will this machine actually work?

Answering that with a checklist rather than letting someone discover it
through a stack trace three minutes into a model download. Every check
is cheap, none of them loads a model, and the whole thing runs on a
machine that cannot import the compute layer at all — which is exactly
the machine most likely to need it.

Checks are ordered so the first failure is the most useful one: there is
no point reporting a missing model on a machine with no backend to run
it on.
"""

from __future__ import annotations

import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

from . import credentials, machine
from .api_client import ApiClient, ApiError
from .config import Config

OK = "ok"
WARN = "warn"
FAIL = "fail"

_MARK = {OK: "✓", WARN: "!", FAIL: "✗"}


@dataclass
class Check:
    name: str
    status: str
    detail: str
    #: Shown only when the check is not OK — what to actually do.
    fix: str | None = None

    def render(self) -> str:
        line = f"  {_MARK[self.status]} {self.name:<22} {self.detail}"
        if self.fix and self.status != OK:
            line += f"\n      → {self.fix}"
        return line


def run(config: Config) -> int:
    checks: list[Check] = [
        _python(),
        _platform(),
    ]
    backend = _backend()
    checks.append(backend)
    checks.append(_compute())
    checks.extend(_account(config))
    checks.extend(_models())
    checks.append(_disk())

    print("mechbench-runner doctor\n")
    for check in checks:
        print(check.render())

    failures = [c for c in checks if c.status == FAIL]
    warnings = [c for c in checks if c.status == WARN]
    print()
    if failures:
        print(
            f"{len(failures)} problem{'s' if len(failures) > 1 else ''} to fix "
            f"before this machine can run jobs."
        )
        return 1
    if warnings:
        print("Ready to run jobs. Some things worth a look above.")
        return 0
    print("Ready to run jobs.")
    return 0


# --- the machine -------------------------------------------------------------


def _python() -> Check:
    v = sys.version_info
    rendered = f"{v.major}.{v.minor}.{v.micro}"
    if (v.major, v.minor) < (3, 11):
        return Check(
            "python", FAIL, f"{rendered} (need 3.11+)",
            "Install Python 3.11 or newer and reinstall mechbench-runner.",
        )
    return Check("python", OK, rendered)


def _platform() -> Check:
    return Check(
        "platform", OK,
        f"{machine.describe_platform()} · {machine.hostname()}",
    )


def _backend() -> Check:
    """The substrate question, asked of the compute layer rather than
    guessed at here — and answerable even when there is no substrate."""
    try:
        from mechbench_compute import backends
    except ImportError as exc:
        return Check(
            "compute backend", FAIL,
            f"mechbench-compute is not installed ({exc})",
            "pip install mechbench-compute",
        )

    active = backends.active()
    if active is None:
        supported = ", ".join(b.platform_label for b in backends.BACKENDS)
        return Check(
            "compute backend", FAIL,
            f"none available on {backends.describe_platform()}",
            f"Supported today: {supported}. The platform-independent "
            f"half of mechbench-compute still works for reading results.",
        )
    return Check(
        "compute backend", OK,
        f"{active.label} [{_version_of(active.module)}]",
    )


def _compute() -> Check:
    try:
        import mechbench_compute
    except ImportError as exc:
        return Check(
            "mechbench-compute", FAIL, str(exc), "pip install mechbench-compute"
        )
    version = getattr(mechbench_compute, "__version__", "unknown")
    return Check("mechbench-compute", OK, version)


def _version_of(module_name: str) -> str:
    from importlib.metadata import PackageNotFoundError, version

    root = module_name.split(".")[0]
    try:
        return version(root)
    except PackageNotFoundError:
        return "version unknown"


# --- the account -------------------------------------------------------------


def _account(config: Config) -> list[Check]:
    stored = credentials.load()
    if not config.api_key:
        return [
            Check(
                "credentials", FAIL, "this machine is not signed in",
                "mechbench-runner login",
            )
        ]

    source = (
        f"stored for {stored.name}" if stored and config.from_stored_credentials
        else "MECHBENCH_API_KEY"
    )
    checks = [Check("credentials", OK, f"{source} → {config.api_base_url}")]

    try:
        with ApiClient(config) as api:
            data = api.whoami()
    except ApiError as exc:
        if exc.status == 401:
            return checks + [
                Check(
                    "api", FAIL, "the credential was rejected",
                    "This machine has been signed out. Run `mechbench-runner login`.",
                )
            ]
        if exc.status == 400:
            return checks + [
                Check(
                    "api", WARN, "reachable, but this key is not a runner's",
                    "Jobs can still be claimed. `mechbench-runner login` registers "
                    "the machine so it appears on the website.",
                )
            ]
        return checks + [Check("api", FAIL, f"{exc}", "Check MECHBENCH_API_URL.")]
    except Exception as exc:  # noqa: BLE001 — an unreachable API reads as one
        return checks + [
            Check(
                "api", FAIL, f"unreachable: {exc}",
                f"Is {config.api_base_url} correct, and is this machine online?",
            )
        ]

    runner = data.get("runner") or {}
    account = data.get("account") or {}
    checks.append(
        Check(
            "api",
            OK,
            f"{account.get('handle')} · {runner.get('name')} "
            f"· {data.get('scopeLabel')}",
        )
    )
    return checks


# --- the weights -------------------------------------------------------------


def _models() -> list[Check]:
    try:
        from mechbench_compute import inventory
    except ImportError:
        return [Check("model cache", WARN, "unavailable without mechbench-compute")]

    try:
        repos = inventory.scan()
    except Exception as exc:  # noqa: BLE001 — an unreadable cache is a warning
        return [Check("model cache", WARN, f"could not be read: {exc}")]

    if not repos:
        return [
            Check(
                "model cache", WARN, "empty",
                "Weights download on the first job that needs them; the first "
                "run will be slow rather than broken.",
            )
        ]

    total = sum(r.disk_bytes for r in repos)
    revisions = sum(len(r.revisions) for r in repos)
    checks = [
        Check(
            "model cache", OK,
            f"{len(repos)} models, {revisions} revisions, "
            f"{inventory.format_bytes(total)}",
        )
    ]

    reclaimable = sum(r.reclaimable_bytes for r in repos)
    superseded = sum(len(r.superseded) for r in repos)
    if superseded:
        # Deliberately reporting what deleting would actually return, not
        # the sum of the revisions' sizes: they share blobs, and the
        # difference between those two numbers is usually enormous.
        checks.append(
            Check(
                "unused revisions",
                WARN if reclaimable > 1_000_000_000 else OK,
                f"{superseded} not pointed at by any ref, "
                f"{inventory.format_bytes(reclaimable)} reclaimable",
                "mechbench-runner models --prune",
            )
        )
    return checks


def _disk() -> Check:
    try:
        from mechbench_compute.hub import hf_hub_cache

        path = Path(hf_hub_cache())
    except Exception:  # noqa: BLE001
        path = Path.home()
    while not path.exists() and path != path.parent:
        path = path.parent

    usage = shutil.disk_usage(path)
    free_gb = usage.free / 1e9
    detail = f"{free_gb:.0f} GB free where models are cached"
    if free_gb < 20:
        return Check(
            "disk", FAIL, detail,
            "A single model is commonly 10-25 GB. Free some space first.",
        )
    if free_gb < 60:
        return Check(
            "disk", WARN, detail,
            "Enough for one more model, not several.",
        )
    return Check("disk", OK, detail)
