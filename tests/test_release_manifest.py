from __future__ import annotations

import base64
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from signed_manifest import (
    COMPUTE_WHEEL,
    MLX_MARKER,
    PUB,
    RUNNER_WHEEL,
    Recorder,
    sha,
    sign,
    signed,
    unsigned,
    wheels,
)

from mechbench_runner import install, release_manifest
from mechbench_runner.release_manifest import ManifestError

URL = "https://api.example.test/releases/manifest"
PY = "/fake/env/bin/python"
FAKE_UV = "/fake/bin/uv"


@pytest.fixture()
def installed(monkeypatch):
    have = {"mechbench": "0.52.0", "mechbench-compute": "0.174.0",
            "mechbench-schema": "0.18.1"}
    monkeypatch.setattr(install, "installed_versions", lambda: dict(have))
    monkeypatch.setattr(install, "find_executable",
                        lambda name: FAKE_UV if name == "uv" else None)
    return have


def run_upgrade(body, **kw):
    run = kw.pop("run", Recorder())
    got = release_manifest.upgrade(PY, URL, fetch=lambda _u: body,
                                   get_bytes=kw.pop("get_bytes", wheels), run=run,
                                   key=kw.pop("key", PUB), say=lambda _m: None, **kw)
    return got, run


class TestCanonicalForm:
    def test_keys_sort_at_every_depth_without_whitespace_or_the_signature(self):
        body = {"schema": 1, "b": {"z": [1, "é"], "a": True}, "a": None,
                "signature": "x"}
        assert release_manifest.canonical(body) == (
            '{"a":null,"b":{"a":true,"z":[1,"é"]},"schema":1}'.encode())


class TestSignature:
    def test_a_signed_manifest_verifies(self):
        m = release_manifest.verify(signed(), PUB)
        assert m.target == ("0.53.0", "0.175.0")
        assert m.allow_downgrade is False

    def test_a_tampered_manifest_is_refused(self, installed):
        body = signed()
        body["runner"] = {**body["runner"], "version": "0.53.1"}
        with pytest.raises(ManifestError, match="does not verify"):
            release_manifest.verify(body, PUB)
        got, run = run_upgrade(body)
        assert not got.ok and not got.changed
        assert got.message.startswith("release manifest refused")
        assert run.calls == []

    def test_a_swapped_hash_is_refused(self):
        body = signed()
        body["locked"][1]["sha256"] = ["d" * 64]
        with pytest.raises(ManifestError, match="does not verify"):
            release_manifest.verify(body, PUB)

    def test_an_unsigned_manifest_is_refused(self, installed):
        got, run = run_upgrade(unsigned())
        assert "not signed" in got.message and run.calls == []

    def test_another_key_is_refused(self):
        other = sign(unsigned(), Ed25519PrivateKey.generate())
        with pytest.raises(ManifestError, match="does not verify"):
            release_manifest.verify(other, PUB)

    def test_a_build_without_a_release_key_trusts_nothing(self, tmp_path, installed):
        empty = tmp_path / "release_key.pub"
        empty.write_text("")
        assert release_manifest.public_key(empty) is None
        got, run = run_upgrade(signed(), key=None)
        assert got.ok is False and run.calls == []

    def test_the_shipped_key_file_loads_when_it_holds_a_key(self, tmp_path):
        pem = PUB.public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo)
        (tmp_path / "k.pub").write_bytes(pem)
        key = release_manifest.public_key(tmp_path / "k.pub")
        release_manifest.verify(signed(), key)

    def test_a_bad_base64_signature_is_refused(self):
        with pytest.raises(ManifestError, match="not base64"):
            release_manifest.verify({**unsigned(), "signature": "!!"}, PUB)


class TestShape:
    @pytest.mark.parametrize("over, why", [
        ({"schema": 2}, "schema"),
        ({"runner": {"version": "0.53.0+local",
                     "wheel": {"url": "https://x/mechbench-0.53.0-py3-none-any.whl",
                               "sha256": "a" * 64}}}, "strict"),
        ({"runner": {"version": "0.53.0",
                     "wheel": {"url": "http://x/mechbench-0.53.0-py3-none-any.whl",
                               "sha256": "a" * 64}}}, "https"),
        ({"runner": {"version": "0.53.0",
                     "wheel": {"url": "https://x/evil-0.53.0-py3-none-any.whl",
                               "sha256": "a" * 64}}}, "not a mechbench 0.53.0 wheel"),
        ({"locked": [{"name": "mechbench", "version": "0.1.0",
                      "sha256": ["a" * 64]}]}, "outside its own entry"),
        ({"locked": [{"name": "httpx", "version": "0.28.1",
                      "marker": "python_version > '3'\n--index-url https://evil",
                      "sha256": ["a" * 64]}]}, "marker"),
        ({"locked": [{"name": "httpx", "version": "0.28.1", "sha256": []}]},
         "no sha256"),
        ({"allow_downgrade": "yes"}, "boolean"),
    ])
    def test_a_signed_manifest_of_the_wrong_shape_is_still_refused(self, over, why):
        with pytest.raises(ManifestError, match=why):
            release_manifest.verify(signed(**over), PUB)


class TestInstallRule:
    def test_the_happy_path_installs_exactly_the_manifest(self, installed):
        got, run = run_upgrade(signed())
        assert got.ok and got.changed
        assert "0.52.0 -> 0.53.0" in got.message and "0.174.0 -> 0.175.0" in got.message
        [cmd] = run.calls
        assert cmd[:7] == [FAKE_UV, "pip", "install", "--python", PY,
                           "--require-hashes", "-r"]
        req = cmd[7]
        wdir = release_manifest.release_dir() / "wheels"
        runner = wdir / sha(RUNNER_WHEEL)[:16] / "mechbench-0.53.0-py3-none-any.whl"
        compute = (wdir / sha(COMPUTE_WHEEL)[:16]
                   / "mechbench_compute-0.175.0-py3-none-any.whl")
        assert Path(req).read_text() == (
            "httpx==0.28.1 \\\n"
            f"    --hash=sha256:{'a' * 64} \\\n"
            f"    --hash=sha256:{'b' * 64}\n"
            f"mlx==0.32.3 ; {MLX_MARKER} \\\n"
            f"    --hash=sha256:{'c' * 64}\n"
            f"mechbench @ {runner.resolve().as_uri()} \\\n"
            f"    --hash=sha256:{sha(RUNNER_WHEEL)}\n"
            f"mechbench-compute @ {compute.resolve().as_uri()} \\\n"
            f"    --hash=sha256:{sha(COMPUTE_WHEEL)}\n")
        assert runner.read_bytes() == RUNNER_WHEEL

    def test_a_hash_mismatch_aborts_before_install(self, installed):
        def swapped(url):
            if "mechbench_compute" in url:
                return b"someone else's wheel"
            return wheels(url)

        got, run = run_upgrade(signed(), get_bytes=swapped)
        assert not got.ok and not got.changed
        assert "arrived as sha256" in got.message
        assert run.calls == []

    def test_pip_is_the_fallback_without_uv(self, installed, monkeypatch):
        monkeypatch.setattr(install, "find_executable", lambda _n: None)
        _, run = run_upgrade(signed())
        assert run.calls[0][:6] == [PY, "-m", "pip", "install", "--require-hashes",
                                    "-r"]

    def test_the_manifest_already_installed_is_nothing_to_do(self, installed):
        installed.update({"mechbench": "0.53.0", "mechbench-compute": "0.175.0"})
        got, run = run_upgrade(signed())
        assert got.ok and not got.changed and got.message == ""
        assert run.calls == []

    def test_an_older_manifest_is_refused_without_allow_downgrade(self, installed):
        installed.update({"mechbench": "0.54.0"})
        got, run = run_upgrade(signed())
        assert not got.ok and "older than the installed 0.54.0" in got.message
        assert run.calls == []

    def test_an_older_manifest_is_followed_with_allow_downgrade(self, installed):
        installed.update({"mechbench": "0.54.0"})
        got, run = run_upgrade(signed(allow_downgrade=True))
        assert got.ok and got.changed and len(run.calls) == 1

    def test_an_update_naming_another_version_installs_nothing(self, installed):
        got, run = run_upgrade(signed(), requested="0.60.0")
        assert not got.ok and "names 0.53.0" in got.message and run.calls == []

    def test_a_failed_target_is_skipped_quietly(self, installed):
        got, run = run_upgrade(signed(), skip=("0.53.0", "0.175.0"))
        assert got.ok and not got.changed and run.calls == []

    def test_the_installer_failing_is_reported(self, installed):
        got, _ = run_upgrade(signed(), run=Recorder(fail={"--require-hashes"}))
        assert not got.ok and not got.changed and "failed" in got.message


class TestRestore:
    def test_the_previous_hash_locked_install_is_restored(self, installed):
        run_upgrade(signed())
        installed.update({"mechbench": "0.53.0", "mechbench-compute": "0.175.0"})
        _, run = run_upgrade(signed(runner={
            "version": "0.53.1",
            "wheel": {"url": "https://f/mechbench-0.53.1-py3-none-any.whl",
                      "sha256": sha(b"next")}}),
            get_bytes=lambda u: b"next" if "0.53.1" in u else wheels(u))
        first = release_manifest._read_state()["previous"]  # noqa: SLF001
        back = Recorder()
        assert release_manifest.restore_previous(PY, ("0.53.0", "0.175.0"), run=back,
                                                 say=lambda _m: None)
        assert back.calls == [[FAKE_UV, "pip", "install", "--python", PY,
                               "--require-hashes", "-r", first]]

    def test_without_a_record_the_earlier_pins_are_reinstalled(self, installed):
        back = Recorder()
        said: list[str] = []
        assert release_manifest.restore_previous(PY, ("0.52.0", "0.174.0"), run=back,
                                                 say=said.append)
        assert back.calls == [[FAKE_UV, "pip", "install", "--python", PY,
                               "mechbench==0.52.0", "mechbench-compute==0.174.0"]]
        assert any("by pin" in s for s in said)


def test_signatures_are_base64_of_64_bytes():
    assert len(base64.b64decode(signed()["signature"])) == 64
