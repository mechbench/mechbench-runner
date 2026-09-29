from __future__ import annotations

import hashlib
import importlib
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import time
import zipfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from . import install as install_mod
from . import paths
from .api_client import ApiError, installed_path
from .policy import PolicyHolder, _release, policy_admits

INSTALL_FAILED = "INSTALL_FAILED"

GC_EVERY_SECONDS = 3600.0
UPGRADE_EVERY_SECONDS = 3600.0
UV_TIMEOUT_SECONDS = 900.0

PYPI = "https://pypi.org/pypi"
RUNNER_DIST = "mechbench"
COMPUTE_DIST = "mechbench-compute"


class InstallError(Exception):
    pass


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _age_days(iso: Any, now: float) -> float:
    if not isinstance(iso, str):
        return float("inf")
    try:
        then = datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return float("inf")
    return (now - then) / 86400.0


def failed_path() -> Path:
    return paths.extensions_dir() / "failed.json"


def cache_dir() -> Path:
    d = paths.extensions_dir() / "cache"
    d.mkdir(mode=0o700, exist_ok=True)
    return d


def _read(path: Path) -> dict[str, dict[str, Any]]:
    try:
        raw = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    if isinstance(raw, list):
        raw = {r.get("hash"): r for r in raw if isinstance(r, Mapping)}
    if not isinstance(raw, Mapping):
        return {}
    return {str(k): dict(v) for k, v in raw.items() if isinstance(v, Mapping)}


def _write(path: Path, data: Mapping[str, Any]) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True))
    os.replace(tmp, path)


def read_installed() -> dict[str, dict[str, Any]]:
    return _read(installed_path())


def write_installed(records: Mapping[str, Any]) -> None:
    _write(installed_path(), records)


def read_failed() -> dict[str, dict[str, Any]]:
    return _read(failed_path())


def digest_of(ref: str) -> str:
    tail = ref.rsplit("/", 1)[-1]
    algo, _, hexd = tail.partition(":")
    if algo != "sha256" or len(hexd) != 64:
        raise InstallError(f"{ref} is not a sha256 reference")
    return hexd


def compute_version() -> str:
    try:
        from mechbench_compute import __version__
    except ImportError:
        return ""
    return str(__version__)


def _dist_of_wheel(path: Path) -> tuple[str, str, str]:
    with zipfile.ZipFile(path) as z:
        info = next((n for n in z.namelist()
                     if n.count("/") == 1 and n.endswith(".dist-info/WHEEL")), None)
        if info is None:
            raise InstallError("the wheel has no .dist-info/WHEEL")
        stem = info.split("/", 1)[0][: -len(".dist-info")]
        tags = [line.split(":", 1)[1].strip()
                for line in z.read(info).decode().splitlines()
                if line.startswith("Tag:")]
    name, _, version = stem.partition("-")
    if not name or not version or not tags:
        raise InstallError(
            "the wheel's metadata names no distribution, version or tag")
    py, abi, plat = (sorted({t.split("-")[i] for t in tags}) for i in range(3))
    tag = "-".join(".".join(parts) for parts in (py, abi, plat))
    return name, version, f"{name}-{version}-{tag}.whl"


def _dist_of_sdist(path: Path) -> tuple[str, str, str]:
    try:
        with tarfile.open(path, "r:gz") as t:
            top = next((m.name.split("/", 1)[0] for m in t.getmembers()), "")
    except tarfile.TarError as exc:
        raise InstallError(f"the sdist is not a .tar.gz ({exc})") from exc
    name, _, version = top.rpartition("-")
    if not name or not version:
        raise InstallError("the sdist's top directory names no version")
    return name, version, f"{top}.tar.gz"


@dataclass
class Fetched:
    path: Path
    digest: str
    dist: str


class ComputeRegistry:
    def refresh(self) -> None:
        importlib.invalidate_caches()
        from mechbench_compute.registry import REGISTRY

        REGISTRY.refresh()

    def problem(self, address: str, version: int) -> str | None:
        from mechbench_compute.registry import INSTALLED

        loaded = INSTALLED.loaded.get(address)
        if loaded is not None and loaded.extension.version == version:
            return None
        owner_project = address.split("/extensions/", 1)[0]
        why = [m for k, m in INSTALLED.refused.items()
               if k == owner_project or address.startswith(f"{k}/") or k in address]
        return "; ".join(why) or (f"{address}@{version} is not loaded after install: "
                                  "no mechbench.extensions entry point provides it")


def restart_error() -> type[BaseException]:
    try:
        from mechbench_compute.registry import RestartRequired
    except ImportError:
        return type("NoRestartRequired", (Exception,), {})
    return RestartRequired


@dataclass
class Outcome:
    ok: bool
    restart: str | None = None


class Extensions:
    def __init__(self, *, python: str | None = None,
                 registry: Any = None,
                 wall: Callable[[], float] = time.time,
                 clock: Callable[[], float] = time.monotonic,
                 fetch_json: Callable[[str], Any] | None = None) -> None:
        self.python = python or sys.executable
        self.registry = registry or ComputeRegistry()
        self.wall = wall
        self.clock = clock
        self.fetch_json = fetch_json or _fetch_json
        self._last_gc: float | None = None
        self._last_upgrade: float | None = None
        self._report_route = True
        self._bad_upgrade: tuple[str, str] | None = None
        self._told_source = False

    def _uv(self) -> str:
        uv = install_mod.find_executable("uv")
        if uv is None:
            raise InstallError("uv cannot be found from here")
        return uv

    def _uv_run(self, args: list[str]) -> None:
        cmd = [self._uv(), "pip", *args]
        print(f"[runner] {' '.join(cmd)}", flush=True)
        try:
            proc = install_mod._run(cmd, timeout=UV_TIMEOUT_SECONDS)  # noqa: SLF001
        except (OSError, subprocess.SubprocessError) as exc:
            raise InstallError(f"uv did not run to the end ({exc})") from exc
        if proc.returncode != 0:
            tail = ((proc.stderr or "") + (proc.stdout or "")).strip()
            lines = [ln for ln in tail.splitlines() if ln.strip()]
            raise InstallError(" ".join(lines[-3:])[:400]
                               or f"uv exited {proc.returncode}")


    def install(self, api: Any, job: Mapping[str, Any], holder: PolicyHolder,
                runner: str) -> Outcome:
        items = list(job.get("install") or [])
        if not items:
            self.note_used(job)
            return Outcome(True)
        held = holder.held
        context: dict[str, Any] = {"runnerOwnerId": holder.owner(api),
                                   "jobCreatorId": job.get("userId")}
        if job.get("orgId") is not None:
            context["jobOrgId"] = job["orgId"]
        admits = [policy_admits(held.body, i, context) if held else None for i in items]
        if held is None or not all(a is not None and a.ok for a in admits):
            _release(api, job, [a for a in admits if a is not None])
            return Outcome(False)
        installed = read_installed()
        changed = False
        restart: str | None = None
        for item in items:
            pin = str(item.get("hash"))
            if pin in installed:
                continue
            label = f"{item.get('address')}@{item.get('version')}"
            try:
                self._refuse_known_failure(pin, held)
                restart = self._install_one(api, item, job, installed) or restart
                changed = True
            except Exception as exc:  # noqa: BLE001
                reason = (str(exc) if isinstance(exc, InstallError)
                          else f"{type(exc).__name__}: {exc}")
                self._fail(api, job, item, held, runner, reason)
                if changed:
                    self._report(api)
                return Outcome(False)
            print(f"[runner] installed {label} for job {job.get('id')}", flush=True)
        if changed:
            self._report(api)
        self.note_used(job)
        if restart is not None:
            return Outcome(False, restart)
        return Outcome(True)

    def _refuse_known_failure(self, pin: str, held: Any) -> None:
        prior = read_failed().get(pin)
        if prior is None:
            return
        same_policy = prior.get("policy") == {"id": held.id, "version": held.version}
        if same_policy and prior.get("compute") == compute_version():
            raise InstallError(f"it failed here before, under this policy and compute "
                                f"{prior.get('compute')}: {prior.get('reason')}")

    def _install_one(self, api: Any, item: Mapping[str, Any], job: Mapping[str, Any],
                     installed: dict[str, dict[str, Any]]) -> str | None:
        pkg = item.get("package") or {}
        ref = pkg.get("wheel") or pkg.get("sdist")
        if not isinstance(ref, str):
            raise InstallError("the claim names no wheel and no sdist")
        artifact = self._fetch(api, ref, wheel=bool(pkg.get("wheel")))
        lock = self._fetch(api, pkg["lock"], lock=True) if pkg.get("lock") else None
        if lock is not None:
            req = cache_dir() / f"{artifact.digest}.requirements.txt"
            own = re.compile(rf"^{re.escape(_norm(artifact.dist))}\s*(==|@|\[|;|$)")
            kept = [ln for ln in lock.path.read_text().splitlines()
                    if not own.match(_norm(ln.strip()))]
            kept.append(f"{artifact.dist} @ {artifact.path.resolve().as_uri()} "
                        f"--hash=sha256:{artifact.digest}")
            req.write_text("\n".join(kept) + "\n")
            self._uv_run(["install", "--python", self.python, "--require-hashes",
                          "-r", str(req)])
        else:
            self._uv_run(["install", "--python", self.python, str(artifact.path)])
        pin = str(item["hash"])
        address = str(item.get("address"))
        for old, rec in list(installed.items()):
            if (rec.get("address") == address
                    or rec.get("package_name") == artifact.dist):
                installed.pop(old)
        installed[pin] = {
            "hash": pin,
            "address": address,
            "version": item.get("version"),
            "name": item.get("name"),
            "package_name": artifact.dist,
            "installed_at": _now(),
            "by_job": job.get("id"),
            "used_at": _now(),
        }
        write_installed(installed)
        try:
            self.registry.refresh()
        except restart_error() as exc:
            return str(exc)
        problem = self.registry.problem(address, int(item.get("version") or 0))
        if problem is not None:
            installed.pop(pin, None)
            write_installed(installed)
            try:
                self._uv_run(["uninstall", "--python", self.python, artifact.dist])
            except InstallError as exc:
                print(f"[runner] could not uninstall {artifact.dist} ({exc})")
            raise InstallError(problem)
        return None

    def _fetch(self, api: Any, ref: str, *, wheel: bool = False,
               lock: bool = False) -> Fetched:
        digest = digest_of(ref)
        blob = cache_dir() / digest
        if not (blob.is_file() and _sha256(blob.read_bytes()) == digest):
            try:
                data = api.fetch_by_hash(ref)
            except ApiError as exc:
                raise InstallError(f"could not fetch {ref} ({exc})") from exc
            except httpx.HTTPError as exc:
                raise InstallError(f"could not fetch {ref} ({exc})") from exc
            got = _sha256(data)
            if got != digest:
                raise InstallError(f"{ref} arrived as sha256:{got}; refusing it")
            tmp = blob.with_suffix(".tmp")
            tmp.write_bytes(data)
            os.replace(tmp, blob)
        if lock:
            return Fetched(blob, digest, "")
        try:
            dist, _, filename = _dist_of_wheel(blob) if wheel else _dist_of_sdist(blob)
        except (zipfile.BadZipFile, UnicodeDecodeError) as exc:
            raise InstallError(f"{ref} is not a wheel ({exc})") from exc
        named = cache_dir() / digest[:16]
        named.mkdir(exist_ok=True)
        target = named / filename
        if not target.is_file():
            shutil.copyfile(blob, target)
        return Fetched(target, digest, dist)

    def _fail(self, api: Any, job: Mapping[str, Any], item: Mapping[str, Any],
              held: Any, runner: str, reason: str) -> None:
        pin = str(item.get("hash"))
        label = f"{item.get('address')}@{item.get('version')}"
        failed = read_failed()
        failed[pin] = {"address": item.get("address"), "version": item.get("version"),
                       "reason": reason, "compute": compute_version(),
                       "policy": {"id": held.id, "version": held.version}, "at": _now()}
        _write(failed_path(), failed)
        message = f"install of {label} failed on {runner}: {reason}"
        job_id = str(job.get("id"))
        print(f"[runner] job {job_id} released: {INSTALL_FAILED}: {message}",
              flush=True)
        try:
            api.release_job(job_id, INSTALL_FAILED, message)
        except Exception as exc:  # noqa: BLE001
            print(f"[runner] could not release job {job_id} ({exc})")

    def _report(self, api: Any) -> None:
        if not self._report_route or not hasattr(api, "report_installed"):
            return
        body = [{"address": r.get("address"), "version": r.get("version"),
                 "hash": r.get("hash"), "installedAt": r.get("installed_at"),
                 "byJob": r.get("by_job")} for r in read_installed().values()]
        try:
            api.report_installed(body)
        except ApiError as exc:
            if exc.status in (404, 405):
                self._report_route = False
                return
            print(f"[runner] could not report the installed extensions ({exc})")
        except Exception as exc:  # noqa: BLE001
            print(f"[runner] could not report the installed extensions ({exc})")

    def note_used(self, job: Mapping[str, Any]) -> None:
        installed = read_installed()
        if not installed:
            return
        text = json.dumps(job.get("spec") or {}, default=str)
        named = {str(i.get("hash")) for i in job.get("install") or []}
        hit = False
        for pin, rec in installed.items():
            if pin in named or pin in text or str(rec.get("address")) in text:
                rec["used_at"] = _now()
                hit = True
        if hit:
            write_installed(installed)


    def between_jobs(self, api: Any, holder: PolicyHolder) -> str | None:
        held = holder.held
        if held is None:
            return None
        self.collect(api, held.body)
        return self.self_upgrade(held.body)

    def collect(self, api: Any, body: Mapping[str, Any],
                force: bool = False) -> list[str]:
        now = self.clock()
        if (not force and self._last_gc is not None
                and now - self._last_gc < GC_EVERY_SECONDS):
            return []
        self._last_gc = now
        days = (body.get("gc") or {}).get("unused_days")
        installed = read_installed()
        removed: list[str] = []
        for pin, rec in list(installed.items()):
            why = None
            last = rec.get("used_at") or rec.get("installed_at")
            if isinstance(days, (int, float)) and _age_days(last, self.wall()) >= days:
                why = f"unused for {days} days"
            elif self._withdrawn(api, rec):
                why = "withdrawn"
            if why is None:
                continue
            name = rec.get("package_name")
            try:
                if name:
                    self._uv_run(["uninstall", "--python", self.python, str(name)])
            except InstallError as exc:
                print(f"[runner] could not uninstall {name} ({exc})")
                continue
            installed.pop(pin)
            removed.append(pin)
            label = f"{rec.get('address')}@{rec.get('version')}"
            print(f"[runner] uninstalled {label} ({why})", flush=True)
        if removed:
            write_installed(installed)
            self._report(api)
        return removed

    def _withdrawn(self, api: Any, rec: Mapping[str, Any]) -> bool:
        if not hasattr(api, "extension_detail"):
            return False
        try:
            detail = api.extension_detail(str(rec.get("address")),
                                          int(rec.get("version") or 0))
        except Exception:  # noqa: BLE001
            return False
        manifest = detail.get("manifest") if isinstance(detail, Mapping) else None
        state = (manifest or {}).get("state") if isinstance(manifest, Mapping) else None
        return state == "withdrawn"

    def self_upgrade(self, body: Mapping[str, Any], force: bool = False) -> str | None:
        if (body.get("upgrades") or {}).get("compute") != "auto":
            return None
        now = self.clock()
        if (not force and self._last_upgrade is not None
                and now - self._last_upgrade < UPGRADE_EVERY_SECONDS):
            return None
        self._last_upgrade = now
        where = install_mod.detect()
        if where.method == "source":
            if not self._told_source:
                print("[runner] upgrades: auto, but this runner runs from a source "
                      "checkout; it does not upgrade itself")
                self._told_source = True
            return None
        try:
            target = self.newest()
        except Exception as exc:  # noqa: BLE001
            print(f"[runner] could not read PyPI for upgrades ({exc})")
            return None
        if target is None or target == self._bad_upgrade:
            return None
        have = install_mod.installed_versions()
        runner_v, compute_v = target
        if not (_newer(runner_v, have.get(RUNNER_DIST))
                or _newer(compute_v, have.get(COMPUTE_DIST))):
            return None
        previous = (have.get(RUNNER_DIST), have.get(COMPUTE_DIST))
        pins = [f"{RUNNER_DIST}=={runner_v}", f"{COMPUTE_DIST}=={compute_v}"]
        refresh = ["--refresh-package", RUNNER_DIST, "--refresh-package", COMPUTE_DIST]
        try:
            self._uv_run(["install", "--python", self.python, *refresh, *pins])
            self._self_check()
        except InstallError as exc:
            self._bad_upgrade = target
            print(f"[runner] upgrade to {RUNNER_DIST} {runner_v}, {COMPUTE_DIST} "
                  f"{compute_v} failed ({exc}); "
                  f"staying on {previous[0]}, {previous[1]}",
                  flush=True)
            if all(previous) and "(absent)" not in previous:
                try:
                    self._uv_run(["install", "--python", self.python,
                                  f"{RUNNER_DIST}=={previous[0]}",
                                  f"{COMPUTE_DIST}=={previous[1]}"])
                except InstallError as exc2:
                    print(f"[runner] could not restore the previous versions ({exc2})")
            return None
        reason = (f"upgraded {RUNNER_DIST} {previous[0]} -> {runner_v}, "
                  f"{COMPUTE_DIST} {previous[1]} -> {compute_v} under upgrades: auto")
        print(f"[runner] {reason}", flush=True)
        return reason

    def _self_check(self) -> None:
        cmd = [self.python, "-c",
               "import mechbench_runner.job_runner, mechbench_compute.protocol"]
        try:
            proc = install_mod._run(cmd, timeout=300.0)  # noqa: SLF001
        except Exception as exc:  # noqa: BLE001
            raise InstallError(f"the self-check did not run ({exc})") from exc
        if proc.returncode != 0:
            tail = ((proc.stderr or "") + (proc.stdout or "")).strip()
            raise InstallError(f"the new versions do not import: {tail[-300:]}")

    def newest(self) -> tuple[str, str] | None:
        from packaging.requirements import Requirement
        from packaging.version import InvalidVersion, Version

        runner = self.fetch_json(f"{PYPI}/{RUNNER_DIST}/json")
        runner_v = str(runner["info"]["version"])
        spec = None
        for line in runner["info"].get("requires_dist") or []:
            req = Requirement(line)
            if req.name == COMPUTE_DIST and req.marker is None:
                spec = req.specifier
        compute = self.fetch_json(f"{PYPI}/{COMPUTE_DIST}/json")
        candidates: list[Version] = []
        for text, files in (compute.get("releases") or {}).items():
            try:
                v = Version(text)
            except InvalidVersion:
                continue
            if v.is_prerelease or not files or all(f.get("yanked") for f in files):
                continue
            if spec is None or spec.contains(v):
                candidates.append(v)
        if not candidates:
            return None
        return runner_v, str(max(candidates))


def _newer(candidate: str, have: str | None) -> bool:
    from packaging.version import InvalidVersion, Version

    if not have or have == "(absent)":
        return False
    try:
        return Version(candidate) > Version(have.split("+", 1)[0])
    except InvalidVersion:
        return False


def _norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _fetch_json(url: str) -> Any:
    res = httpx.get(url, timeout=httpx.Timeout(15.0), follow_redirects=True)
    res.raise_for_status()
    return res.json()
