from __future__ import annotations

import json
import re
import subprocess
import sys
import time
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import install as install_mod
from . import paths
from .confine import make_owned, owned_dir, remove_owned, under
from .extensions import Extensions, InstallError, _norm, compute_version
from .policy import PolicyHolder, _release, job_of, policy_admits, runner_of
from .redact import child_env, redact

VERIFICATION = "verification"
COMPUTE_DIST = "mechbench-compute"
RUNNER_DIST = "mechbench"
GROUP = "mechbench.extensions"

UV_TIMEOUT_SECONDS = 900.0
CONFORMANCE_TIMEOUT_SECONDS = 3600.0
MESSAGE_CHARS = 4000
STEPS = ("fetch", "venv", "build", "install", "lock", "conformance", "upload")
BENCH_SEGMENT = re.compile(r"[A-Za-z0-9_~@:+-][A-Za-z0-9._~@:+-]{0,199}")

Runner = Callable[..., subprocess.CompletedProcess]


class StageError(Exception):
    def __init__(self, stage: str, message: str) -> None:
        super().__init__(message)
        self.stage = stage
        self.message = message


def scratch_root() -> Path:
    d = paths.extensions_dir() / "verify"
    d.mkdir(mode=0o700, parents=True, exist_ok=True)
    return d


def _release_of(version: str) -> str:
    return version.split("+", 1)[0]


def _tail(proc: subprocess.CompletedProcess, lines: int = 20) -> str:
    text = ((proc.stderr or "") + "\n" + (proc.stdout or "")).strip()
    kept = [ln for ln in text.splitlines() if ln.strip()][-lines:]
    return "\n".join(kept)[-MESSAGE_CHARS:] or f"exited {proc.returncode}"


def _clip(text: Any, n: int) -> str:
    return str(text)[:n]


def _finding(f: Mapping[str, Any]) -> dict[str, Any]:
    severity = f.get("severity")
    return {
        "code": _clip(f.get("code") or "UNKNOWN", 64),
        "at": _clip(f.get("at") or "", 500),
        "message": _clip(redact(f.get("message") or ""), MESSAGE_CHARS),
        "severity": severity if severity in ("error", "warning") else "error",
    }


def _example(e: Mapping[str, Any]) -> dict[str, Any]:
    status = e.get("status")
    kind = e.get("kind")
    return {
        "op": _clip(e.get("op") or "?", 300),
        "status": status
        if status in ("identical", "differs", "skipped", "failed")
        else "failed",
        "kind": _clip(kind, 300) if kind is not None else None,
        "satisfies": e.get("satisfies")
        if isinstance(e.get("satisfies"), bool)
        else None,
        "deterministic": e.get("deterministic")
        if isinstance(e.get("deterministic"), bool)
        else None,
        "seconds": [max(0.0, float(s)) for s in (e.get("seconds") or [])[:2]],
        "findings": [_finding(f) for f in (e.get("findings") or [])[:50]],
    }


def read_constraints(
    freeze: str, listed: list[Mapping[str, Any]], *, exclude: set[str]
) -> tuple[list[str], dict[str, Path]]:
    editable = {
        _norm(str(d["name"])): Path(str(d["editable_project_location"]))
        for d in listed
        if d.get("editable_project_location")
    }
    lines: list[str] = []
    for raw in freeze.splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", "-")) or " @ " in line or "==" not in line:
            continue
        name = _norm(line.split("==", 1)[0])
        if name in exclude or name in editable:
            continue
        lines.append(line)
    return lines, editable


def _wheel_version(path: Path) -> tuple[str, str]:
    name, version = path.name.split("-")[:2]
    return _norm(name), version


def _read_report(stdout: str) -> dict[str, Any]:
    start = stdout.find("{")
    while start != -1:
        try:
            found, _ = json.JSONDecoder().raw_decode(stdout[start:])
        except ValueError:
            start = stdout.find("{", start + 1)
            continue
        if isinstance(found, dict) and "declarations" in found:
            return found
        start = stdout.find("{", start + 1)
    raise ValueError("the conformance CLI printed no report")


def _refs_in(value: Any) -> list[str]:
    out: list[str] = []
    if isinstance(value, Mapping):
        if set(value) == {"$ref"} and isinstance(value["$ref"], Mapping):
            where = value["$ref"].get("bench")
            if isinstance(where, str):
                out.append(where)
        else:
            for v in value.values():
                out += _refs_in(v)
    elif isinstance(value, list):
        for v in value:
            out += _refs_in(v)
    return out


def bench_path(where: str) -> str:
    parts = where.split("/")
    if (len(where) > 500 or len(parts) < 2
            or not all(BENCH_SEGMENT.fullmatch(p) for p in parts)):
        raise StageError("conformance", f"an example's input {where[:200]!r} is not a "
                                        f"bench path")
    return where


def _decode(data: bytes) -> Any:
    try:
        found = json.loads(data)
    except ValueError:
        import cbor2

        found = cbor2.loads(data)
    if isinstance(found, dict) and "payload" in found and "provenance" in found:
        return found["payload"]
    return found


def extension_for_policy(ext: Mapping[str, Any]) -> dict[str, Any]:
    owner = ext.get("projectOwner")
    if not isinstance(owner, Mapping):
        org = ext.get("org")
        owner = {"kind": "org", "id": org} if org else {"kind": "user", "id": ext.get("owner")}
    return {
        "name": ext.get("name"),
        "projectOwner": dict(owner),
        "state": ext.get("state"),
        "party": ext.get("party"),
        "needs": list(ext.get("needs") or []),
        "approvedBy": list(ext.get("approvedBy") or []),
    }


@dataclass
class Verification:
    python: str = field(default_factory=lambda: sys.executable)
    model: bool = False
    run: Runner = subprocess.run
    uv: str | None = None
    extensions: Extensions | None = None
    log: Callable[[str], None] = lambda s: print(s, flush=True)

    def _uv(self) -> str:
        found = self.uv or install_mod.find_executable("uv")
        if found is None:
            raise StageError("venv", "uv cannot be found from here")
        return found

    def _call(
        self,
        stage: str,
        cmd: list[str],
        *,
        timeout: float = UV_TIMEOUT_SECONDS,
        cwd: Path | None = None,
    ) -> subprocess.CompletedProcess:
        self.log(f"[runner] {' '.join(cmd)}")
        try:
            proc = self.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
                cwd=cwd,
                env=child_env(),
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise StageError(stage, f"{cmd[0]} did not run to the end ({exc})") from exc
        if proc.returncode != 0:
            raise StageError(stage, _tail(proc))
        return proc

    def handle(
        self, api: Any, job: Mapping[str, Any], holder: PolicyHolder | None = None
    ) -> dict[str, Any] | None:
        spec = job.get("spec") or {}
        ext = spec.get("extension") or {}
        job_id = str(job.get("id"))
        if holder is not None:
            held = holder.held
            admit = (
                policy_admits(held.body, extension_for_policy(ext), job_of(job),
                              runner_of(api, holder, job))
                if held is not None
                else None
            )
            if admit is None or not admit.ok:
                _release(api, job, [admit] if admit is not None else [])
                return None
        report = self.verify(api, job_id, spec)
        api.complete_verification(job_id, report)
        verdict = (
            "passed"
            if report["failure"] is None and report.get("passed")
            else "did not pass"
        )
        self.log(
            f"[runner] verification {job_id} of "
            f"{ext.get('address')}@{ext.get('version')} {verdict}"
        )
        return report

    def verify(self, api: Any, job_id: str, spec: Mapping[str, Any]) -> dict[str, Any]:
        ext = spec.get("extension") or {}
        report: dict[str, Any] = {
            "compute": _release_of(compute_version()) or "unknown",
            "conformance": None,
            "findings": [],
            "examples": [],
            "wheel": None,
            "lock": None,
            "model": False,
            "failure": None,
        }
        scratch = owned_dir(scratch_root(), "job id", job_id)
        remove_owned(scratch)
        make_owned(scratch, job_id)
        step = 0

        def progress(stage: str) -> None:
            nonlocal step
            step = STEPS.index(stage)
            with suppress(Exception):
                api.report_progress(
                    job_id, step, len(STEPS), unit="steps", status="running"
                )

        try:
            progress("fetch")
            sdist = self._fetch_sdist(api, ext)
            progress("venv")
            uv = self._uv()
            venv = scratch / "venv"
            self._call("venv", [uv, "venv", "--python", self.python, str(venv)])
            scratch_py = str(venv / "bin" / "python")
            progress("build")
            wheel = self._build(uv, sdist, scratch / "dist")
            own = _norm(_wheel_version(wheel)[0])
            constraints, compute_req, links = self._constraints(
                uv, scratch, exclude={own}
            )
            report["compute"] = (
                compute_req.split("==", 1)[1]
                if "==" in compute_req
                else report["compute"]
            )
            progress("install")
            found = [a for d in links for a in ("--find-links", str(d))]
            self._call(
                "install",
                [
                    uv,
                    "pip",
                    "install",
                    "--python",
                    scratch_py,
                    "--constraint",
                    str(constraints),
                    *found,
                    compute_req,
                    str(wheel),
                ],
            )
            progress("lock")
            lock = self._lock(uv, scratch, scratch_py, wheel, own, constraints, found)
            progress("conformance")
            inputs = self._inputs(api, scratch / "inputs", ext.get("manifest") or {})
            entry = self._entry_point(scratch_py, own, ext)
            needs_model = any(str(n).startswith("model.") for n in self._needs(ext))
            report["model"] = bool(self.model and needs_model)
            got = self._conformance(scratch_py, entry, inputs, model=report["model"])
            report["conformance"] = {
                k: got[k]
                for k in ("compute", "declarations", "double_run", "installs_beside")
            }
            report["compute"] = str(got["compute"])
            report["passed"] = bool(got.get("passed"))
            report["findings"] = [
                _finding(f) for f in (got.get("findings") or [])[:500]
            ]
            report["examples"] = [
                _example(e) for e in (got.get("examples") or [])[:200]
            ]
            progress("upload")
            builds = str(spec.get("builds") or "")
            if not builds:
                raise StageError(
                    "upload",
                    "the job names no builds path to store the wheel and lock under",
                )
            base = f"{builds}/{re.sub(r'[^a-z0-9_-]', '-', job_id.lower())}"
            report["wheel"] = self._upload(
                api, f"{base}/wheel", wheel.read_bytes(), "wheel"
            )
            report["lock"] = self._upload(
                api, f"{base}/lock", lock.read_bytes(), "lock"
            )
        except StageError as exc:
            report["failure"] = {
                "stage": exc.stage,
                "message": _clip(redact(exc.message), MESSAGE_CHARS) or exc.stage,
            }
        except Exception as exc:  # noqa: BLE001
            stage = STEPS[step]
            report["failure"] = {
                "stage": stage,
                "message": _clip(redact(f"{type(exc).__name__}: {exc}"), MESSAGE_CHARS),
            }
        finally:
            remove_owned(scratch)
        return report

    def _needs(self, ext: Mapping[str, Any]) -> list[str]:
        manifest = ext.get("manifest") or {}
        needs = list(ext.get("needs") or manifest.get("needs") or [])
        for op in (manifest.get("provides") or {}).get("ops") or []:
            needs += list(op.get("needs") or [])
        return needs

    def _fetch_sdist(self, api: Any, ext: Mapping[str, Any]) -> Path:
        ref = (ext.get("package") or {}).get("sdist")
        if not isinstance(ref, str):
            raise StageError("fetch", "the job names no sdist")
        try:
            got = (self.extensions or Extensions(python=self.python))._fetch(api, ref)  # noqa: SLF001
        except InstallError as exc:
            raise StageError("fetch", str(exc)) from exc
        return got.path

    def _build(self, uv: str, sdist: Path, out: Path) -> Path:
        self._call("build", [uv, "build", "--wheel", "--out-dir", str(out), str(sdist)])
        wheels = sorted(out.glob("*.whl"))
        if len(wheels) != 1:
            raise StageError(
                "build", f"building {sdist.name} gave {len(wheels)} wheels, not one"
            )
        return wheels[0]

    def _constraints(
        self, uv: str, scratch: Path, *, exclude: set[str]
    ) -> tuple[Path, str, list[Path]]:
        freeze = self._call(
            "install", [uv, "pip", "freeze", "--python", self.python]
        ).stdout
        listed = json.loads(
            self._call(
                "install",
                [uv, "pip", "list", "--python", self.python, "--format", "json"],
            ).stdout
            or "[]"
        )
        lines, editable = read_constraints(freeze, listed, exclude=exclude)
        links: list[Path] = []
        compute_req = ""
        if editable:
            house = scratch / "wheelhouse"
            for name, where in sorted(editable.items()):
                if name == RUNNER_DIST or name in exclude:
                    continue
                before = set(house.glob("*.whl")) if house.is_dir() else set()
                self._call(
                    "install",
                    [uv, "build", "--wheel", "--out-dir", str(house), str(where)],
                )
                for built in set(house.glob("*.whl")) - before:
                    dist, version = _wheel_version(built)
                    lines.append(f"{dist}=={version}")
                    if dist == COMPUTE_DIST:
                        compute_req = f"{COMPUTE_DIST}=={version}"
            if house.is_dir():
                links.append(house)
        if not compute_req:
            pinned = [ln for ln in lines if _norm(ln.split("==", 1)[0]) == COMPUTE_DIST]
            if not pinned:
                raise StageError(
                    "install",
                    "this runner's environment holds no mechbench-compute "
                    "to verify beside",
                )
            compute_req = f"{COMPUTE_DIST}=={pinned[0].split('==', 1)[1].strip()}"
        path = scratch / "constraints.txt"
        path.write_text("\n".join(sorted(set(lines), key=str.lower)) + "\n")
        return path, compute_req, links

    def _lock(
        self,
        uv: str,
        scratch: Path,
        scratch_py: str,
        wheel: Path,
        own: str,
        constraints: Path,
        found: list[str],
    ) -> Path:
        wanted = scratch / "requirements.in"
        wanted.write_text(f"{own} @ {wheel.resolve().as_uri()}\n")
        lock = scratch / "lock.txt"
        self._call(
            "lock",
            [
                uv,
                "pip",
                "compile",
                str(wanted),
                "--constraint",
                str(constraints),
                *found,
                "--generate-hashes",
                "--python",
                scratch_py,
                "--no-emit-package",
                own,
                "--no-header",
                "--no-annotate",
                "--output-file",
                str(lock),
            ],
        )
        return lock

    def _inputs(self, api: Any, root: Path, manifest: Mapping[str, Any]) -> Path:
        wanted: list[str] = []
        for op in (manifest.get("provides") or {}).get("ops") or []:
            wanted += _refs_in(op.get("example_inputs") or {})
        targets = {where: under(root, f"{bench_path(where)}.json", what="object path",
                                ident=where)
                   for where in sorted(set(wanted))}
        root.mkdir(parents=True, exist_ok=True)
        for where, target in targets.items():
            target.parent.mkdir(parents=True, exist_ok=True)
            try:
                value = _decode(api.fetch_object(where))
            except Exception as exc:  # noqa: BLE001
                raise StageError(
                    "conformance",
                    f"an example's input {where} could not be read ({exc})",
                ) from exc
            target.write_text(json.dumps(value))
        return root

    def _entry_point(self, scratch_py: str, own: str, ext: Mapping[str, Any]) -> str:
        code = (
            "import importlib.metadata as m, json, sys; "
            "d = m.distribution(sys.argv[1]); "
            f"g = {GROUP!r}; "
            "print(json.dumps([e.name for e in d.entry_points if e.group == g]))"
        )
        proc = self._call("conformance", [scratch_py, "-c", code, own], timeout=120.0)
        names = json.loads(
            proc.stdout.strip().splitlines()[-1] if proc.stdout.strip() else "[]"
        )
        if names:
            return str(names[0])
        manifest = ext.get("manifest") or {}
        for op in (manifest.get("provides") or {}).get("ops") or []:
            if op.get("entry"):
                return str(op["entry"]).split(".", 1)[0]
        raise StageError("conformance", f"{own} declares no entry point in {GROUP}")

    def _conformance(
        self, scratch_py: str, entry: str, inputs: Path, *, model: bool
    ) -> dict[str, Any]:
        cmd = [
            scratch_py,
            "-m",
            "mechbench_compute.conformance",
            entry,
            "--inputs",
            str(inputs),
        ]
        if model:
            cmd.append("--model")
        self.log(f"[runner] {' '.join(cmd)}")
        start = time.monotonic()
        try:
            proc = self.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=CONFORMANCE_TIMEOUT_SECONDS,
                check=False,
                cwd=None,
                env=child_env(),
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise StageError(
                "conformance", f"the conformance run did not finish ({exc})"
            ) from exc
        if proc.returncode not in (0, 1):
            raise StageError("conformance", _tail(proc))
        try:
            got = _read_report(proc.stdout or "")
        except ValueError as exc:
            raise StageError("conformance", f"{exc}: {_tail(proc)}") from exc
        self.log(f"[runner] conformance ran in {time.monotonic() - start:.1f}s")
        return got

    def _upload(self, api: Any, path: str, data: bytes, kind: str) -> str:
        try:
            got = api.put_bytes(path, data, kind=kind)
        except Exception as exc:  # noqa: BLE001
            raise StageError("upload", f"{path} could not be stored ({exc})") from exc
        digest = str(got.get("hash") or "")
        if not digest.startswith("sha256:"):
            raise StageError("upload", f"{path} was stored without a hash")
        return f"~hash/{digest}"
