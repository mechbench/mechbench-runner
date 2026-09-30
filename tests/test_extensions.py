from __future__ import annotations

import hashlib
import io
import json
import shutil
import subprocess
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

pytest.importorskip("mechbench_compute")

from mechbench_runner import api_client, install, paths  # noqa: E402
from mechbench_runner import extensions as ext_mod  # noqa: E402
from mechbench_runner import job_runner as jr  # noqa: E402
from mechbench_runner.api_client import ApiError, advertise  # noqa: E402
from mechbench_runner.config import Config  # noqa: E402
from mechbench_runner.exits import EXIT_RESTART  # noqa: E402
from mechbench_runner.extensions import Extensions  # noqa: E402
from mechbench_runner.policy import HeldPolicy, PolicyHolder  # noqa: E402

BODY = {
    "extensions": {"install": "mine", "allow": [], "network": "none",
                   "require_approved": False},
    "upgrades": {"compute": "auto"},
    "gc": {"unused_days": 30},
    "pools": [],
}
ADDRESS = "u_alice/lab/extensions/tiny"
FAKE_UV = "/fake/bin/uv"
PY = "/fake/env/bin/python"


def tiny_wheel(version: str = "1.0.0") -> bytes:
    files = {
        "tinyext/__init__.py": "X = 1\n",
        f"tinyext-{version}.dist-info/METADATA":
            f"Metadata-Version: 2.1\nName: tinyext\nVersion: {version}\n",
        f"tinyext-{version}.dist-info/WHEEL":
            "Wheel-Version: 1.0\nGenerator: test\nRoot-Is-Purelib: true\n"
            "Tag: py3-none-any\n",
        f"tinyext-{version}.dist-info/entry_points.txt":
            "[mechbench.extensions]\ntiny = tinyext:EXTENSION\n",
    }
    record = [f"{n},," for n in files] + [f"tinyext-{version}.dist-info/RECORD,,"]
    files[f"tinyext-{version}.dist-info/RECORD"] = "\n".join(record) + "\n"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, text in files.items():
            z.writestr(name, text)
    return buf.getvalue()


def ref_of(data: bytes) -> str:
    return f"~hash/sha256:{hashlib.sha256(data).hexdigest()}"


def pin_of(n: int) -> str:
    return "sha256:" + f"{n:x}" * 64


def item(wheel: bytes, *, lock: bytes | None = None, version: int = 1,
         wheel_ref: str | None = None) -> dict:
    return {"address": ADDRESS, "version": version, "hash": pin_of(version),
            "name": "u_alice/lab/tiny",
            "package": {"name": "tinyext", "python": ">=3.11",
                        "sdist": "~hash/sha256:" + "0" * 64,
                        "wheel": wheel_ref or ref_of(wheel),
                        "lock": ref_of(lock) if lock is not None else None},
            "needs": [], "state": "draft", "visibility": "private",
            "owner": "u_alice", "org": None, "party": "third"}


class FakeApi:
    def __init__(self, claims=None, blobs=None) -> None:
        self.claims = list(claims or [])
        self.blobs = dict(blobs or {})
        self.released: list[tuple[str, str, str]] = []
        self.interrupted: list[str] = []
        self.failed: list[tuple[str, str]] = []
        self.details: dict[str, dict] = {}
        self.fetched: list[str] = []

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return None

    def fetch_policy(self):
        return {"policyId": "pol_personal", "version": 1, "body": BODY}

    def whoami(self):
        return {"account": {"userId": "u_alice"}}

    def claim_next_job(self, *_a):
        if not self.claims:
            raise ApiError(401, {"code": "UNAUTHORIZED"})
        return self.claims.pop(0)

    def fetch_by_hash(self, ref: str) -> bytes:
        self.fetched.append(ref)
        if ref not in self.blobs:
            raise ApiError(404, {"code": "NOT_FOUND"})
        return self.blobs[ref]

    def report_installed(self, _installed):
        raise ApiError(404, {"code": "NOT_FOUND"})

    def extension_detail(self, address: str, version: int):
        found = self.details.get(f"{address}@{version}")
        if found is None:
            raise ApiError(404, {"code": "NOT_FOUND"})
        return found

    def release_job(self, job_id, code, message, timeout=None):
        self.released.append((job_id, code, message))

    def interrupt_job(self, job_id, message, timeout=None):
        self.interrupted.append(job_id)

    def fail_job(self, job_id, message, timeout=None):
        self.failed.append((job_id, message))

    def list_jobs(self):
        return []

    def live_leased(self):
        return []


class FakeRegistry:
    def __init__(self, restart: str | None = None, problem: str | None = None):
        self.refreshes = 0
        self.restart = restart
        self._problem = problem
        self.seen_installed: list[dict] = []

    def refresh(self):
        self.refreshes += 1
        self.seen_installed.append(ext_mod.read_installed())
        if self.restart:
            from mechbench_compute.registry import RestartRequired
            raise RestartRequired(self.restart)

    def problem(self, _address, _version):
        return self._problem


class FakeUv:
    def __init__(self, fail: str | None = None) -> None:
        self.calls: list[list[str]] = []
        self.fail = fail
        self.requirements: list[str] = []

    def __call__(self, cmd, *, timeout):  # noqa: ARG002
        self.calls.append(list(cmd))
        if "-r" in cmd:
            self.requirements.append(Path(cmd[cmd.index("-r") + 1]).read_text())
        if self.fail and cmd[0] == FAKE_UV:
            return subprocess.CompletedProcess(cmd, 1, "", self.fail)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    def uv_calls(self):
        return [c for c in self.calls if c[0] == FAKE_UV]


@pytest.fixture()
def uv(monkeypatch):
    fake = FakeUv()
    monkeypatch.setattr(install, "_run", fake)
    monkeypatch.setattr(install, "find_executable",
                        lambda name: FAKE_UV if name == "uv" else None)
    return fake


@pytest.fixture()
def holder(tmp_path):
    h = PolicyHolder(tmp_path / "policy.json")
    h.held = HeldPolicy("pol_personal", 1, dict(BODY))
    h.owner_id = "u_alice"
    return h


def job(items, jid="j_1", **extra):
    return {"id": jid, "userId": "u_alice", "orgId": None,
            "policy": {"id": "pol_personal", "version": 1},
            "spec": {"graph": {"nodes": [
                {"id": "align", "block": "u_alice/lab/ops/geo/align"}]}},
            "install": items, **extra}


class TestInstall:
    def test_an_admitted_install_is_fetched_verified_installed_and_recorded(
            self, uv, holder):
        wheel = tiny_wheel()
        it = item(wheel)
        api = FakeApi(blobs={it["package"]["wheel"]: wheel})
        reg = FakeRegistry()
        out = Extensions(python=PY, registry=reg).install(api, job([it]), holder, "m1")
        assert out.ok and out.restart is None
        [cmd] = uv.uv_calls()
        assert cmd[:5] == [FAKE_UV, "pip", "install", "--python", PY]
        assert cmd[5].endswith("/tinyext-1.0.0-py3-none-any.whl")
        installed = json.loads(api_client.installed_path().read_text())
        rec = installed[it["hash"]]
        assert set(rec) == {"hash", "address", "version", "name", "package_name",
                            "installed_at", "by_job", "used_at"}
        assert (rec["hash"], rec["address"], rec["version"], rec["package_name"],
                rec["by_job"]) == (it["hash"], ADDRESS, 1, "tinyext", "j_1")
        assert reg.refreshes == 1
        assert reg.seen_installed[0][it["hash"]]["address"] == ADDRESS
        assert api.released == []

    def test_compute_reads_the_record_this_writes(self, uv, holder, monkeypatch,
                                                  tmp_path):
        from mechbench_compute import registry
        wheel = tiny_wheel()
        it = item(wheel)
        Extensions(python=PY, registry=FakeRegistry()).install(
            FakeApi(blobs={it["package"]["wheel"]: wheel}), job([it]), holder, "m1")
        home = tmp_path / "home"
        home.mkdir()
        (home / ".mechbench").symlink_to(paths.mechbench_dir())
        monkeypatch.setattr(registry.Path, "home", lambda: home)
        seen = [r for r in registry.read_installed()
                if (r.get("address"), r.get("version")) == (ADDRESS, 1)]
        assert seen and seen[0]["hash"] == it["hash"]

    def test_with_a_lock_every_line_carries_a_hash(self, uv, holder):
        wheel = tiny_wheel()
        lock = (b"numpy==2.0.0 --hash=sha256:" + b"a" * 64 + b"\n"
                b"tinyext==1.0.0 --hash=sha256:" + b"b" * 64 + b"\n")
        it = item(wheel, lock=lock)
        api = FakeApi(blobs={it["package"]["wheel"]: wheel,
                             it["package"]["lock"]: lock})
        Extensions(python=PY, registry=FakeRegistry()).install(api, job([it]),
                                                               holder, "m1")
        [cmd] = uv.uv_calls()
        assert cmd[:7] == [FAKE_UV, "pip", "install", "--python", PY,
                           "--require-hashes", "-r"]
        req = uv.requirements[0].splitlines()
        assert req[0].startswith("numpy==2.0.0 --hash=sha256:")
        assert not any(line.startswith("tinyext==") for line in req)
        digest = hashlib.sha256(wheel).hexdigest()
        assert req[-1].startswith("tinyext @ file://")
        assert req[-1].endswith(f"--hash=sha256:{digest}")

    def test_a_hash_mismatch_releases_the_job_and_marks_the_hash_failed(
            self, uv, holder):
        wheel = tiny_wheel()
        it = item(wheel)
        api = FakeApi(blobs={it["package"]["wheel"]: wheel + b"tampered"})
        reg = FakeRegistry()
        out = Extensions(python=PY, registry=reg).install(api, job([it]), holder, "m1")
        assert not out.ok
        [(jid, code, message)] = api.released
        assert (jid, code) == ("j_1", "INSTALL_FAILED")
        assert message.startswith(f"install of {ADDRESS}@1 failed on m1: ")
        assert "refusing it" in message
        failed = json.loads(ext_mod.failed_path().read_text())
        from mechbench_compute import __version__
        assert failed[it["hash"]]["compute"] == __version__
        assert failed[it["hash"]]["policy"] == {"id": "pol_personal", "version": 1}
        assert uv.uv_calls() == [] and reg.refreshes == 0
        assert not api_client.installed_path().exists()

    def test_a_resolver_failure_releases_with_uvs_reason_and_is_not_retried(
            self, uv, holder):
        uv.fail = "No solution found when resolving dependencies:\n  numpy>=9"
        wheel = tiny_wheel()
        it = item(wheel)
        api = FakeApi(blobs={it["package"]["wheel"]: wheel})
        ext = Extensions(python=PY, registry=FakeRegistry())
        assert not ext.install(api, job([it]), holder, "m1").ok
        assert api.released[0][1] == "INSTALL_FAILED"
        assert "No solution found" in api.released[0][2]
        assert "No solution found" in ext_mod.read_failed()[it["hash"]]["reason"]
        assert not ext.install(api, job([it], "j_2"), holder, "m1").ok
        assert len(uv.uv_calls()) == 1
        assert "failed here before" in api.released[1][2]
        holder.held = HeldPolicy("pol_personal", 2, dict(BODY))
        uv.fail = None
        assert ext.install(api, job([it], "j_3"), holder, "m1").ok
        assert len(uv.uv_calls()) == 2

    def test_a_load_failure_after_install_uninstalls_and_releases(self, uv, holder):
        wheel = tiny_wheel()
        it = item(wheel)
        api = FakeApi(blobs={it["package"]["wheel"]: wheel})
        reg = FakeRegistry(problem="extension 'tiny' was refused at load: boom")
        assert not Extensions(python=PY, registry=reg).install(api, job([it]),
                                                               holder, "m1").ok
        assert uv.uv_calls()[-1][:3] == [FAKE_UV, "pip", "uninstall"]
        assert "boom" in api.released[0][2]
        assert ext_mod.read_installed() == {}

    def test_the_double_check_refuses_what_the_policy_does_not_admit(
            self, uv, holder):
        holder.held = HeldPolicy("pol_personal", 1,
                                 {**BODY, "extensions": {**BODY["extensions"],
                                                         "install": "locked"}})
        wheel = tiny_wheel()
        it = item(wheel)
        api = FakeApi(blobs={it["package"]["wheel"]: wheel})
        assert not Extensions(python=PY, registry=FakeRegistry()).install(
            api, job([it]), holder, "m1").ok
        assert api.released[0][1] == "POLICY_MISMATCH"
        assert uv.uv_calls() == [] and api.fetched == []

    def test_advertise_says_installs_and_carries_the_pins(self, uv, holder):
        wheel = tiny_wheel()
        it = item(wheel)
        Extensions(python=PY, registry=FakeRegistry()).install(
            FakeApi(blobs={it["package"]["wheel"]: wheel}), job([it]), holder, "m1")
        caps = advertise()
        assert caps["installs"] is True
        assert caps["installed"] == [it["hash"]]


class StubControl:
    def __init__(self, _state, path=None):
        self.path = path or "/tmp/stub.sock"

    def start(self):
        return None

    def stop(self):
        return None


@pytest.fixture()
def make_runner(monkeypatch):
    monkeypatch.setattr(jr, "ControlServer", StubControl)

    def make(registry):
        config = Config(api_base_url="http://127.0.0.1:1", api_key="mbk_test",
                        poll_interval_seconds=0.01, warm_model_id=None,
                        runner_id="rnr_1", runner_name="m1",
                        from_stored_credentials=True)
        r = jr.JobRunner(config)
        monkeypatch.setattr(r, "_claim_control_socket", lambda: None)
        monkeypatch.setattr(r, "_reconcile_jobs", lambda _api: None)
        handled: list[str] = []
        monkeypatch.setattr(r, "_handle", lambda _api, j: handled.append(j["id"]))
        r.handled = handled
        r._extensions = Extensions(python=PY, registry=registry)
        return r

    monkeypatch.setattr(install, "detect", lambda *a, **k: install.Installation(
        "source", None, "a checkout"))
    return make


class TestClaimLoop:
    def test_install_then_run(self, uv, make_runner, monkeypatch):
        wheel = tiny_wheel()
        it = item(wheel)
        api = FakeApi([job([it])], blobs={it["package"]["wheel"]: wheel})
        monkeypatch.setattr(jr, "ApiClient", lambda *_a, **_k: api)
        r = make_runner(FakeRegistry())
        r.run()
        assert r.handled == ["j_1"]
        assert it["hash"] in ext_mod.read_installed()

    def test_a_version_change_restarts_and_the_job_resumes_after(
            self, uv, make_runner, monkeypatch):
        wheel = tiny_wheel("2.0.0")
        it = item(wheel, version=2)
        api = FakeApi([job([it])], blobs={it["package"]["wheel"]: wheel})
        monkeypatch.setattr(jr, "ApiClient", lambda *_a, **_k: api)
        r = make_runner(FakeRegistry(restart="u_alice/lab/extensions/tiny changed"))
        assert r.run() == EXIT_RESTART
        assert r.handled == []
        assert api.interrupted == ["j_1"]
        assert api.released == []
        assert it["hash"] in ext_mod.read_installed()
        again = FakeApi([job([], resume=True)])
        monkeypatch.setattr(jr, "ApiClient", lambda *_a, **_k: again)
        r2 = make_runner(FakeRegistry())
        r2.run()
        assert r2.handled == ["j_1"]


def _days_ago(n: float) -> str:
    return (datetime.now(UTC) - timedelta(days=n)).isoformat(timespec="seconds")


class TestCollect:
    def seed(self):
        ext_mod.write_installed({
            pin_of(1): {"hash": pin_of(1), "address": "a/b/extensions/old", "version": 1,
                        "package_name": "old-ext", "installed_at": _days_ago(60),
                        "used_at": _days_ago(31)},
            pin_of(2): {"hash": pin_of(2), "address": "a/b/extensions/fresh",
                        "version": 1, "package_name": "fresh-ext",
                        "installed_at": _days_ago(60), "used_at": _days_ago(2)},
            pin_of(3): {"hash": pin_of(3), "address": "a/b/extensions/gone",
                        "version": 4, "package_name": "gone-ext",
                        "installed_at": _days_ago(1), "used_at": _days_ago(1)},
        })

    def test_unused_and_withdrawn_are_uninstalled_at_most_hourly(self, uv):
        self.seed()
        api = FakeApi()
        api.details["a/b/extensions/gone@4"] = {"manifest": {"state": "withdrawn"}}
        clock = [1000.0]
        ext = Extensions(python=PY, registry=FakeRegistry(), clock=lambda: clock[0])
        assert sorted(ext.collect(api, BODY)) == [pin_of(1), pin_of(3)]
        assert [c[2:] for c in uv.uv_calls()] == [
            ["uninstall", "--python", PY, "old-ext"],
            ["uninstall", "--python", PY, "gone-ext"]]
        assert list(ext_mod.read_installed()) == [pin_of(2)]
        ext_mod.write_installed({**ext_mod.read_installed(), pin_of(1): {
            "hash": pin_of(1), "package_name": "old-ext", "used_at": _days_ago(40)}})
        clock[0] += 600
        assert ext.collect(api, BODY) == []
        clock[0] += 3600
        assert ext.collect(api, BODY) == [pin_of(1)]

    def test_a_job_that_names_an_extension_keeps_it(self, uv):
        self.seed()
        ext = Extensions(python=PY, registry=FakeRegistry())
        ext.note_used({"spec": {"graph": {"nodes": [
            {"block": "a/b/ops/x/y@sha256:" + "1" * 64}]}}, "install": []})
        ext.note_used({"spec": {"note": "a/b/extensions/old"}})
        assert pin_of(1) not in ext.collect(FakeApi(), BODY)


PYPI_RUNNER = {"info": {"version": "0.48.0",
                        "requires_dist": ["mechbench-compute>=0.165.0,<0.170",
                                          "pytest>=8; extra == 'dev'"]}}
PYPI_COMPUTE = {"releases": {v: [{"yanked": False}] for v in
                             ("0.166.0", "0.167.0", "0.169.2", "0.170.0", "0.171.0rc1")}}


class TestSelfUpgrade:
    @pytest.fixture()
    def pypi(self, monkeypatch):
        fetched: list[str] = []

        def fetch(url):
            fetched.append(url)
            return PYPI_RUNNER if url.endswith("/mechbench/json") else PYPI_COMPUTE

        monkeypatch.setattr(install, "detect", lambda *a, **k: install.Installation(
            "uv-tool", [FAKE_UV, "tool", "upgrade", "mechbench"], "uv"))
        monkeypatch.setattr(install, "installed_versions", lambda: {
            "mechbench": "0.47.0", "mechbench-compute": "0.167.0",
            "mechbench-schema": "0.17.0"})
        return fetch, fetched

    def test_auto_installs_the_newest_pair_and_asks_for_a_restart(self, uv, pypi):
        fetch, fetched = pypi
        ext = Extensions(python=PY, registry=FakeRegistry(), fetch_json=fetch)
        reason = ext.self_upgrade(BODY)
        assert reason and "0.47.0 -> 0.48.0" in reason and "0.167.0 -> 0.169.2" in reason
        [cmd] = uv.uv_calls()
        assert cmd == [FAKE_UV, "pip", "install", "--python", PY,
                       "--refresh-package", "mechbench",
                       "--refresh-package", "mechbench-compute",
                       "mechbench==0.48.0", "mechbench-compute==0.169.2"]
        assert uv.calls[-1][:2] == [PY, "-c"]
        assert ext.self_upgrade(BODY) is None
        assert len(fetched) == 2

    def test_hold_does_nothing(self, uv, pypi):
        fetch, fetched = pypi
        ext = Extensions(python=PY, registry=FakeRegistry(), fetch_json=fetch)
        assert ext.self_upgrade({**BODY, "upgrades": {"compute": "hold"}}) is None
        assert fetched == [] and uv.calls == []

    def test_a_failed_self_check_restores_the_previous_pair(self, uv, pypi, monkeypatch):
        fetch, _ = pypi

        def run(cmd, *, timeout):
            uv.calls.append(list(cmd))
            return subprocess.CompletedProcess(cmd, 1 if cmd[0] == PY else 0, "",
                                               "ImportError: nope")

        monkeypatch.setattr(install, "_run", run)
        ext = Extensions(python=PY, registry=FakeRegistry(), fetch_json=fetch)
        assert ext.self_upgrade(BODY) is None
        assert uv.calls[-1][-2:] == ["mechbench==0.47.0", "mechbench-compute==0.167.0"]
        assert ext.self_upgrade(BODY, force=True) is None
        assert sum(1 for c in uv.calls if "mechbench==0.48.0" in c) == 1

    def test_the_loop_restarts_after_an_upgrade(self, uv, pypi, make_runner,
                                               monkeypatch):
        fetch, _ = pypi
        monkeypatch.setattr(install, "detect", lambda *a, **k: install.Installation(
            "uv-tool", [FAKE_UV], "uv"))
        api = FakeApi([job([])])
        monkeypatch.setattr(jr, "ApiClient", lambda *_a, **_k: api)
        r = make_runner(FakeRegistry())
        r._extensions.fetch_json = fetch
        assert r.run() == EXIT_RESTART
        assert r.handled == []
        assert any("mechbench==0.48.0" in c for c in uv.uv_calls())


@pytest.mark.skipif(shutil.which("uv") is None, reason="uv is not installed")
def test_the_real_uv_installs_the_fixture_wheel_into_a_scratch_env(
        tmp_path, holder, monkeypatch):
    real_uv = shutil.which("uv")
    venv = tmp_path / "env"
    subprocess.run([real_uv, "venv", "-q", str(venv)], check=True)
    python = str(venv / "bin" / "python")

    def run(cmd, *, timeout):
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                              check=False)

    monkeypatch.setattr(install, "_run", run)
    monkeypatch.setattr(install, "find_executable", lambda _n: real_uv)
    wheel = tiny_wheel()
    lock = b"# resolved against compute; no dependencies\n"
    it = item(wheel, lock=lock)
    api = FakeApi(blobs={it["package"]["wheel"]: wheel, it["package"]["lock"]: lock})
    out = Extensions(python=python, registry=FakeRegistry()).install(
        api, job([it]), holder, "m1")
    assert out.ok, api.released
    probe = subprocess.run([python, "-c", "import tinyext; print(tinyext.X)"],
                           capture_output=True, text=True, check=True)
    assert probe.stdout.strip() == "1"
