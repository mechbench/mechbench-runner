from __future__ import annotations

import os

import pytest

torch = pytest.importorskip("torch")

from mechbench_runner import numerics, torch_identity  # noqa: E402

GRACE_LSCPU = """Architecture:             aarch64
  CPU op-mode(s):         64-bit
  Byte Order:             Little Endian
CPU(s):                   20
Vendor ID:                ARM
  Model name:             Cortex-X925
    Thread(s) per core:   1
  Model name:             Cortex-A725
    Thread(s) per core:   1
"""


def test_a_grace_cpu_is_named_by_its_cores_from_lscpu(monkeypatch):
    monkeypatch.setattr(torch_identity.sys, "platform", "linux")
    monkeypatch.setattr(torch_identity.Path, "read_text", lambda self: "processor\t: 0\n")
    assert torch_identity.cpu_model(lambda cmd: GRACE_LSCPU) == "Cortex-X925 + Cortex-A725"


def test_unified_memory_is_read_from_the_integrated_flag_or_the_two_totals():
    gib = 2**30
    assert torch_identity.unified_memory(None, None, {"integrated": True})[0] is True
    same, how = torch_identity.unified_memory(int(119.6 * gib), int(119.7 * gib), {})
    assert same is True and "one pool" in how
    assert torch_identity.unified_memory(80 * gib, 512 * gib, {"integrated": False})[0] is False
    assert torch_identity.unified_memory(None, None, {})[0] is None


def test_on_cpu_the_identity_names_what_is_absent_and_the_settings_in_force():
    me = torch_identity.torch_identity(torch.device("cpu"), smi_present=False)
    assert me["backend"] == "torch" and me["accelerator"] == "cpu" and me["device"] == "cpu"
    assert "gpu" in me["absent"] and "gpu" not in me
    assert me["versions"]["torch"] and me["versions"]["transformers"]
    assert set(me["sdpa"]) >= {"flash", "efficient", "math", "cudnn"}
    assert me["settings"]["deterministic_algorithms"] is False
    assert me["disk"]["hf_cache"].get("total_bytes") or me["disk"]["hf_cache"].get("absent")


def test_the_torch_fingerprint_names_the_libraries_the_mlx_one_does_not():
    me = torch_identity.torch_identity(torch.device("cpu"), smi_present=False)
    comps = torch_identity.torch_components(me, checkpoint="m@1", dtype="bfloat16")
    assert comps["backend"] == "torch" and comps["checkpoint"] == "m@1"
    assert {"transformers", "nnsight", "safetensors", "cuda", "cudnn", "nccl", "driver"} <= set(comps)


def test_deterministic_settings_are_restored_after_the_run(monkeypatch):
    monkeypatch.delenv("CUBLAS_WORKSPACE_CONFIG", raising=False)
    with numerics.deterministic():
        assert torch.are_deterministic_algorithms_enabled()
        assert os.environ["CUBLAS_WORKSPACE_CONFIG"] == ":4096:8"
    assert not torch.are_deterministic_algorithms_enabled()
    assert "CUBLAS_WORKSPACE_CONFIG" not in os.environ


def test_compare_reads_max_abs_max_rel_and_argmax_agreement():
    got = numerics.compare([[0.0, 1.0], [2.0, 0.0]], [[0.0, 1.5], [0.0, 2.0]])
    assert got["identical"] is False and got["max_abs"] == 2.0
    assert got["argmax_agreement"] == 0.5
    assert numerics.compare([1.0, 2.0], [1.0, 2.0])["identical"] is True
