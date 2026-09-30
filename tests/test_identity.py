from __future__ import annotations

import importlib.metadata

import pytest

from mechbench_runner import identity

IOREG = '''+-o AGXAcceleratorG16X  <class AGXAcceleratorG16X>
    {
      "gpu-core-count" = 80
      "PerformanceStatistics" = {"Device Utilization %"=0}
    }
'''

SYSTEM_PROFILER = '{"SPDisplaysDataType": [{"sppci_cores": "60", "sppci_model": "Apple M3 Ultra"}]}'


def _fake(outputs: dict[str, str]):
    seen: list[list[str]] = []

    def run(cmd, timeout=10.0):
        seen.append(cmd)
        return outputs.get(cmd[0] + " " + cmd[-1], "")
    return run, seen


@pytest.fixture(autouse=True)
def _darwin(monkeypatch):
    monkeypatch.setattr(identity.sys, "platform", "darwin")
    identity.identity.cache_clear()
    yield
    identity.identity.cache_clear()


def test_the_machine_from_sysctl_ioreg_and_sw_vers(monkeypatch):
    run, _ = _fake({
        "sysctl machdep.cpu.brand_string": "Apple M5 Ultra\n",
        "ioreg IOAccelerator": IOREG,
        "sw_vers -buildVersion": "25A354\n",
    })
    monkeypatch.setattr(identity, "run", run)
    monkeypatch.setattr(identity.platform, "mac_ver", lambda: ("26.0", ("", "", ""), "arm64"))
    monkeypatch.setattr(identity.platform, "python_version", lambda: "3.12.8")
    versions = {"mlx": "0.29.1", "mlx-lm": "0.28.0", "mlx-vlm": "0.3.4"}

    def version(dist):
        if dist not in versions:
            raise importlib.metadata.PackageNotFoundError(dist)
        return versions[dist]
    monkeypatch.setattr(identity.importlib.metadata, "version", version)
    monkeypatch.setattr(identity, "backends", lambda: ["mlx"])
    monkeypatch.setattr(identity, "architecture_levels",
                        lambda: {"llama": "core", "gemma4": "full"})
    assert identity.identity() == {
        "chip": "Apple M5 Ultra",
        "gpu_cores": 80,
        "os": "macOS 26.0 (25A354)",
        "python": "3.12.8",
        "stack": {"mlx": "0.29.1", "mlx_lm": "0.28.0", "mlx_vlm": "0.3.4",
                  "torch": None},
        "backends": ["mlx"],
        "architectures": ["gemma4", "llama"],
        "architecture_levels": {"gemma4": "full", "llama": "core"},
    }


def test_gpu_cores_fall_back_to_system_profiler(monkeypatch):
    run, seen = _fake({"system_profiler SPDisplaysDataType": SYSTEM_PROFILER})
    monkeypatch.setattr(identity, "run", run)
    assert identity.gpu_cores() == 60
    assert [c[0] for c in seen] == ["ioreg", "system_profiler"]


def test_a_machine_that_answers_nothing_leaves_the_fields_out(monkeypatch):
    run, _ = _fake({})
    monkeypatch.setattr(identity, "run", run)
    monkeypatch.setattr(identity, "chip", lambda: None)
    assert "gpu_cores" not in identity.identity()
    assert "chip" not in identity.identity()


def test_it_is_asked_once_per_process(monkeypatch):
    run, seen = _fake({"ioreg IOAccelerator": IOREG})
    monkeypatch.setattr(identity, "run", run)
    identity.identity()
    n = len(seen)
    identity.identity()
    assert len(seen) == n


def test_the_fingerprint_is_over_the_sorted_components():
    a = {"mlx": "0.29.1", "compute": "0.170.0", "dtype": "bfloat16"}
    b = {"dtype": "bfloat16", "compute": "0.170.0", "mlx": "0.29.1"}
    assert identity.fingerprint(a) == identity.fingerprint(b)
    assert identity.fingerprint(a) != identity.fingerprint({**a, "mlx": "0.29.2"})
    assert identity.fingerprint(a).startswith("sha256:")
