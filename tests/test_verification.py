from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

pytest.importorskip("mechbench_compute")

from mechbench_runner import install  # noqa: E402
from mechbench_runner import job_runner as jr  # noqa: E402
from mechbench_runner import verification as vf  # noqa: E402
from mechbench_runner.api_client import ApiError  # noqa: E402
from mechbench_runner.config import Config  # noqa: E402
from mechbench_runner.policy import HeldPolicy, PolicyHolder  # noqa: E402
from mechbench_runner.verification import Verification, read_constraints  # noqa: E402

FIXTURE = Path(__file__).parent / "fixtures" / "verify_ext"
INPUTS = Path(__file__).parent / "fixtures" / "verify_ext_inputs"
ADDRESS = "alice/lab/extensions/interp-extras"
BODY = {
    "extensions": {"install": "mine", "allow": [], "network": "none",
                   "require_approved": False},
    "upgrades": {"compute": "auto"},
    "gc": {"unused_days": 30},
    "pools": [],
}


def envelope(payload: dict) -> bytes:
    return json.dumps({"provenance": {"created_at": "2026-09-29T00:00:00Z",
                                      "produced_by": {"tool": "t", "version": "1"},
                                      "inputs": [], "schema_version": "1"},
                       "payload": payload}).encode()


class FakeApi:
    def __init__(self, blobs: dict[str, bytes] | None = None,
                 objects: dict[str, bytes] | None = None) -> None:
        self.blobs = dict(blobs or {})
        self.objects = dict(objects or {})
        self.stored: dict[str, tuple[bytes, str | None]] = {}
        self.completed: list[tuple[str, dict]] = []
        self.released: list[tuple[str, str, str]] = []
        self.progress: list[tuple[int, int]] = []

    def fetch_by_hash(self, ref: str) -> bytes:
        if ref not in self.blobs:
            raise ApiError(404, {"code": "NOT_FOUND"})
        return self.blobs[ref]

    def fetch_object(self, path: str) -> bytes:
        if path not in self.objects:
            raise ApiError(404, {"code": "NOT_FOUND"})
        return self.objects[path]

    def put_bytes(self, path: str, data: bytes, *, kind: str | None = None) -> dict:
        self.stored[path] = (data, kind)
        return {"path": path, "hash": f"sha256:{hashlib.sha256(data).hexdigest()}"}

    def complete_verification(self, job_id: str, report: dict) -> None:
        self.completed.append((job_id, report))

    def report_progress(self, job_id, num, den, **_kw) -> None:
        self.progress.append((num, den))

    def release_job(self, job_id, code, message, timeout=None):
        self.released.append((job_id, code, message))

    def whoami(self):
        return {"account": {"userId": "u_alice"}}


def ref_of(data: bytes) -> str:
    return f"~hash/sha256:{hashlib.sha256(data).hexdigest()}"


def manifest() -> dict:
    return {"owner": "alice", "project": "lab", "name": "interp-extras", "version": 2,
            "needs": [],
            "provides": {"ops": [{
                "name": "geometry/align", "needs": [],
                "example": {"method": "overlap"},
                "example_inputs": {"a": {"$ref": {"bench": "alice/lab/a"}},
                                   "b": {"$ref": {"bench": "alice/lab/b"}}},
                "entry": "mb_fixture_ext.ops.geometry.align"}]}}


def job(sdist_ref: str, *, state: str = "pending", owner: str = "u_alice") -> dict:
    return {"id": "j_verify1", "userId": "u_alice", "orgId": None,
            "protocolKind": "verification",
            "spec": {"extension": {
                "address": ADDRESS, "version": 2, "hash": "sha256:" + "b" * 64,
                "name": ADDRESS,
                "package": {"name": "mechbench-ext-interp-extras", "python": ">=3.11",
                            "sdist": sdist_ref},
                "needs": [], "state": state, "visibility": "private",
                "owner": owner, "org": None, "party": "third",
                "manifest": manifest()},
                "builds": "alice/lab/builds/interp-extras/v2"}}


def holder() -> PolicyHolder:
    h = PolicyHolder()
    h.held = HeldPolicy(id="pol_personal", version=1, body=BODY)
    h.owner_id = "u_alice"
    return h


def inputs() -> dict[str, bytes]:
    return {f"alice/lab/{p.stem}": envelope(json.loads(p.read_text()))
            for p in INPUTS.glob("*.json")}


def scratch_left() -> list[Path]:
    return list(vf.scratch_root().iterdir())




class Recording(Verification):
    def _lock(self, uv, scratch, scratch_py, wheel, own, constraints, found):
        lock = super()._lock(uv, scratch, scratch_py, wheel, own, constraints, found)
        self.constraints_text = constraints.read_text()
        self.lock_text = lock.read_text()
        return lock


def build_sdist(uv: str, out: Path) -> Path:
    src = out / "src"
    shutil.copytree(FIXTURE, src, ignore=shutil.ignore_patterns("__pycache__"))
    proc = subprocess.run([uv, "build", "--sdist", "--out-dir", str(out / "sdist"), str(src)],
                          capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        pytest.skip(f"uv could not build the fixture sdist here: {proc.stderr[-300:]}")
    return next((out / "sdist").glob("*.tar.gz"))


def pins(text: str) -> dict[str, str]:
    out = {}
    for line in text.splitlines():
        m = re.match(r"^([A-Za-z0-9][A-Za-z0-9._-]*)==([^\s\\;]+)", line.strip())
        if m:
            out[re.sub(r"[-_.]+", "-", m.group(1)).lower()] = m.group(2)
    return out


def test_the_whole_job_with_the_real_uv(tmp_path, monkeypatch):
    uv = shutil.which("uv")
    if uv is None:
        pytest.skip("uv is not on PATH here; the whole job needs the real uv")
    sdist = build_sdist(uv, tmp_path).read_bytes()
    api = FakeApi(blobs={ref_of(sdist): sdist}, objects=inputs())
    v = Recording(python=sys.executable, uv=uv, log=lambda _s: None)
    report = v.handle(api, job(ref_of(sdist)), holder())

    assert report is not None and api.completed == [("j_verify1", report)]
    assert report["failure"] is None, report["failure"]
    assert report["conformance"]["declarations"] == "passed"
    assert report["passed"] is True
    assert [(e["status"], e["satisfies"]) for e in report["examples"]] == [("identical", True)]
    assert report["model"] is False

    wheel, kind = api.stored[f"alice/lab/builds/interp-extras/v2/j_verify1/wheel"]
    assert kind == "wheel" and report["wheel"] == ref_of(wheel)
    with zipfile.ZipFile(__import__("io").BytesIO(wheel)) as z:
        assert any(n.endswith(".dist-info/WHEEL") for n in z.namelist())
        assert "mb_fixture_ext/ops/geometry/align.py" in z.namelist()
    lock, kind = api.stored["alice/lab/builds/interp-extras/v2/j_verify1/lock"]
    assert kind == "lock" and report["lock"] == ref_of(lock)
    assert lock.decode() == v.lock_text

    constraints = pins(v.constraints_text)
    locked = pins(v.lock_text)
    assert "mechbench-compute" in constraints
    assert locked["mechbench-compute"] == constraints["mechbench-compute"]
    moved = {n: (constraints[n], locked[n]) for n in locked if n in constraints
             and locked[n] != constraints[n]}
    assert moved == {}
    assert "mechbench-ext-interp-extras" not in locked
    assert "--hash=sha256:" in v.lock_text
    assert report["compute"] == constraints["mechbench-compute"]
    assert scratch_left() == []




class FakeUv:

    def __init__(self, fail: dict[str, str] | None = None, conformance: dict | None = None):
        self.fail = dict(fail or {})
        self.calls: list[list[str]] = []
        self.conformance = conformance or {
            "compute": "0.167.0", "declarations": "passed", "double_run": "identical",
            "installs_beside": "0.167.0", "passed": True, "findings": [],
            "examples": [{"op": "alice/lab/ops/geometry/align", "status": "identical",
                          "kind": "alice/lab/kinds/geometry/alignment", "satisfies": True,
                          "deterministic": True, "seconds": [0.1, 0.1], "findings": []}]}

    def which(self, cmd: list[str]) -> str:
        if cmd[1:3] == ["pip", "freeze"]:
            return "freeze"
        if cmd[1:3] == ["pip", "list"]:
            return "list"
        if cmd[1:3] == ["pip", "install"]:
            return "install"
        if cmd[1:3] == ["pip", "compile"]:
            return "lock"
        if cmd[1] == "venv":
            return "venv"
        if cmd[1] == "build":
            return "build"
        if cmd[1] == "-c":
            return "entry"
        return "conformance"

    def __call__(self, cmd, **_kw):
        self.calls.append(list(cmd))
        step = self.which(cmd)
        if step in self.fail:
            return subprocess.CompletedProcess(cmd, 1, "", self.fail[step])
        out = ""
        if step == "freeze":
            out = "numpy==2.4.6\nmechbench-compute==0.167.0\n-e file:///src/mechbench\n"
        elif step == "list":
            out = json.dumps([{"name": "mechbench", "version": "0.48.0",
                               "editable_project_location": "/src/mechbench"}])
        elif step == "build":
            d = Path(cmd[cmd.index("--out-dir") + 1])
            d.mkdir(parents=True, exist_ok=True)
            (d / "mechbench_ext_interp_extras-0.1.0-py3-none-any.whl").write_bytes(b"PK wheel")
        elif step == "lock":
            Path(cmd[cmd.index("--output-file") + 1]).write_text(
                "mechbench-compute==0.167.0 \\\n    --hash=sha256:" + "1" * 64 + "\n"
                "numpy==2.4.6 \\\n    --hash=sha256:" + "2" * 64 + "\n")
        elif step == "entry":
            out = '["interp-extras"]\n'
        elif step == "conformance":
            out = "some warning\n" + json.dumps(self.conformance, indent=2) + "\n"
            rc = 0 if self.conformance.get("passed") else 1
            return subprocess.CompletedProcess(cmd, rc, out, "")
        return subprocess.CompletedProcess(cmd, 0, out, "")


def tiny_sdist() -> bytes:
    import io
    import tarfile

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as t:
        data = b"Metadata-Version: 2.1\nName: mechbench-ext-interp-extras\nVersion: 0.1.0\n"
        info = tarfile.TarInfo("mechbench_ext_interp_extras-0.1.0/PKG-INFO")
        info.size = len(data)
        t.addfile(info, io.BytesIO(data))
    return buf.getvalue()


SDIST = tiny_sdist()


def run_faked(fake: FakeUv, *, blobs: bool = True, spec_job: dict | None = None,
              policy: PolicyHolder | None = None) -> tuple[FakeApi, dict | None]:
    sdist = SDIST
    api = FakeApi(blobs={ref_of(sdist): sdist} if blobs else {}, objects=inputs())
    v = Verification(python="/runner/bin/python", uv="/fake/uv", run=fake, log=lambda _s: None)
    return api, v.handle(api, spec_job or job(ref_of(sdist)), policy or holder())


def test_a_passing_run_reports_the_conformance_the_wheel_and_the_lock():
    fake = FakeUv()
    api, report = run_faked(fake)
    assert report["failure"] is None
    assert report["conformance"] == {"compute": "0.167.0", "declarations": "passed",
                                     "double_run": "identical", "installs_beside": "0.167.0"}
    assert report["wheel"] == ref_of(b"PK wheel")
    assert set(api.stored) == {"alice/lab/builds/interp-extras/v2/j_verify1/wheel",
                               "alice/lab/builds/interp-extras/v2/j_verify1/lock"}
    install_cmd = next(c for c in fake.calls if fake.which(c) == "install")
    assert "mechbench-compute==0.167.0" in install_cmd
    assert install_cmd[install_cmd.index("--python") + 1].endswith("venv/bin/python")
    compile_cmd = next(c for c in fake.calls if fake.which(c) == "lock")
    assert "--generate-hashes" in compile_cmd and "--constraint" in compile_cmd
    assert compile_cmd[compile_cmd.index("--no-emit-package") + 1] == "mechbench-ext-interp-extras"
    conf = next(c for c in fake.calls if fake.which(c) == "conformance")
    assert conf[1:4] == ["-m", "mechbench_compute.conformance", "interp-extras"]
    assert "--model" not in conf
    assert [n for n, _ in api.progress] == list(range(len(vf.STEPS)))
    assert scratch_left() == []


@pytest.mark.parametrize(("stage", "message"), [
    ("install", "Because mechbench-ext-interp-extras depends on numpy<2 and numpy==2.4.6, "
                "we can conclude that the requirements are unsatisfiable."),
    ("lock", "No solution found when resolving dependencies"),
    ("build", "error: invalid pyproject.toml"),
])
def test_a_failing_step_completes_the_job_with_the_resolvers_message(stage, message):
    api, report = run_faked(FakeUv(fail={stage: message}))
    assert api.completed and api.completed[0][1] is report
    assert report["failure"] == {"stage": stage, "message": message}
    assert report["conformance"] is None and report["wheel"] is None and report["lock"] is None
    assert api.stored == {}
    assert scratch_left() == []


def test_a_missing_sdist_is_a_fetch_failure():
    api, report = run_faked(FakeUv(), blobs=False)
    assert report["failure"]["stage"] == "fetch"
    assert "could not fetch" in report["failure"]["message"]
    assert scratch_left() == []


def test_failed_declarations_are_reported_not_hidden():
    conformance = {"compute": "0.167.0", "declarations": "failed", "double_run": "skipped",
                   "installs_beside": "0.167.0", "passed": False,
                   "findings": [{"code": "NO_RUN", "at": "geometry/align",
                                 "message": "defines no run", "severity": "error"}],
                   "examples": [{"op": "x", "status": "failed", "kind": None,
                                 "satisfies": None, "deterministic": None, "seconds": [],
                                 "findings": [{"code": "EXAMPLE_FAILED", "at": "x",
                                               "message": "boom", "severity": "error"}]}]}
    api, report = run_faked(FakeUv(conformance=conformance))
    assert report["failure"] is None
    assert report["passed"] is False
    assert report["conformance"]["declarations"] == "failed"
    assert report["findings"][0]["code"] == "NO_RUN"
    assert report["examples"][0]["status"] == "failed"


def test_a_missing_example_input_is_a_conformance_failure():
    sdist = SDIST
    api = FakeApi(blobs={ref_of(sdist): sdist}, objects={})
    v = Verification(python="/runner/bin/python", uv="/fake/uv", run=FakeUv(), log=lambda _s: None)
    report = v.handle(api, job(ref_of(sdist)), holder())
    assert report["failure"]["stage"] == "conformance"
    assert "alice/lab/a" in report["failure"]["message"]


def test_the_policy_refuses_a_strangers_draft_and_the_job_is_released():
    fake = FakeUv()
    api, report = run_faked(fake, spec_job=job(ref_of(SDIST), owner="u_bob"))
    assert report is None
    assert api.completed == []
    assert api.released and api.released[0][1] == "POLICY_MISMATCH"
    assert fake.calls == []


def test_constraints_leave_out_editables_and_the_package_itself():
    freeze = ("numpy==2.4.6\n-e file:///src/compute\nmechbench-ext-interp-extras==0.0.9\n"
              "somepkg @ file:///x.whl\n")
    listed = [{"name": "mechbench-compute", "version": "0.161.0",
               "editable_project_location": "/src/compute"}]
    lines, editable = read_constraints(freeze, listed, exclude={"mechbench-ext-interp-extras"})
    assert lines == ["numpy==2.4.6"]
    assert editable == {"mechbench-compute": Path("/src/compute")}


def test_the_claim_loop_hands_a_verification_job_to_the_verifier(monkeypatch):
    class StubControl:
        def __init__(self, *_a, **_k):
            self.path = "/tmp/x.sock"

        def start(self):
            return None

        def stop(self):
            return None

    monkeypatch.setattr(jr, "ControlServer", StubControl)
    config = Config(api_base_url="http://127.0.0.1:1", api_key="mbk_test",
                    poll_interval_seconds=0.01, warm_model_id=None,
                    runner_id="rnr_1", from_stored_credentials=True)
    r = jr.JobRunner(config)
    seen = []
    monkeypatch.setattr(jr.Verification, "handle",
                        lambda self, api, j, h: seen.append((j["id"], self.model, h)))
    r._handle(object(), job("~hash/sha256:" + "0" * 64))  # noqa: SLF001
    assert seen == [("j_verify1", False, r._policy)]  # noqa: SLF001


def test_install_run_is_never_reached(monkeypatch):
    def boom(*_a, **_k):
        raise AssertionError("verification must not use the installer's runner")

    monkeypatch.setattr(install, "_run", boom)
    api, report = run_faked(FakeUv())
    assert report["failure"] is None
