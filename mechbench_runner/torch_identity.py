from __future__ import annotations

import ctypes
import ctypes.util
import glob
import importlib.metadata
import os
import platform
import re
import shutil
import sys
from pathlib import Path
from typing import Any

from . import gpu
from .identity import run, stack_components

PACKAGES = ("torch", "transformers", "nnsight", "safetensors", "accelerate", "numpy")
CUDART_ATTRIBUTES = {
    "integrated": 18,
    "can_map_host_memory": 19,
    "pageable_memory_access": 88,
    "concurrent_managed_access": 89,
    "can_use_host_pointer_for_registered_memory": 91,
    "pageable_memory_access_uses_host_page_tables": 100,
    "direct_managed_memory_access_from_host": 101,
}
SAME_POOL_TOLERANCE = 0.1
STATED_BANDWIDTH = {"NVIDIA GB10": 273e9}


def version(dist: str) -> str | None:
    try:
        return importlib.metadata.version(dist)
    except importlib.metadata.PackageNotFoundError:
        return None


def pick_device(requested: str | None = None) -> Any:
    import torch

    if requested:
        return torch.device(requested)
    return torch.device("cuda" if torch.cuda.is_available() else "cpu")


def accelerator_of(device: Any) -> str:
    from mechbench_compute.torch_backend.loading import read_accelerator

    return read_accelerator(device)


def is_cuda(device: Any) -> bool:
    return getattr(device, "type", str(device)) == "cuda"


def device_index(device: Any) -> int:
    import torch

    if not is_cuda(device):
        return 0
    return device.index if device.index is not None else torch.cuda.current_device()


def gpu_name(device: Any) -> str | None:
    import torch

    return torch.cuda.get_device_name(device_index(device)) if is_cuda(device) else None


def sm(capability: tuple[int, int] | None) -> str | None:
    return f"sm_{capability[0]}{capability[1]}" if capability else None


def describe_device(device: Any) -> str:
    import torch

    if not is_cuda(device):
        return str(getattr(device, "type", device))
    cap = torch.cuda.get_device_capability(device_index(device))
    return f"{gpu_name(device)} ({sm(cap)})"


def cpu_model(sh: Any = run) -> str | None:
    if sys.platform == "darwin":
        name = sh(["sysctl", "-n", "machdep.cpu.brand_string"]).strip()
        return name or None
    names: list[str] = []
    try:
        text = Path("/proc/cpuinfo").read_text()
    except OSError:
        text = ""
    for m in re.finditer(r"^model name\s*:\s*(.+)$", text, re.M):
        if m.group(1).strip() not in names:
            names.append(m.group(1).strip())
    if not names:
        for m in re.finditer(r"^\s*Model name:\s*(.+)$", sh(["lscpu"]), re.M):
            if m.group(1).strip() not in names:
                names.append(m.group(1).strip())
    return " + ".join(names) if names else (platform.processor() or None)


def host_memory_total() -> int | None:
    try:
        text = Path("/proc/meminfo").read_text()
    except OSError:
        text = ""
    found = re.search(r"^MemTotal:\s*(\d+)\s*kB", text, re.M)
    if found:
        return int(found.group(1)) * 1024
    if sys.platform == "darwin":
        raw = run(["sysctl", "-n", "hw.memsize"]).strip()
        return int(raw) if raw.isdigit() else None
    return None


def disk(path: Path) -> dict[str, Any]:
    probe = path
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    try:
        usage = shutil.disk_usage(probe)
    except OSError as e:
        return {"path": str(path), "absent": str(e)}
    return {"path": str(path), "total_bytes": usage.total, "free_bytes": usage.free}


def hf_cache() -> Path:
    home = os.environ.get("HF_HUB_CACHE") or os.environ.get("HUGGINGFACE_HUB_CACHE")
    if home:
        return Path(home)
    return Path(os.environ.get("HF_HOME") or Path.home() / ".cache" / "huggingface") / "hub"


def settings() -> dict[str, Any]:
    import torch

    out: dict[str, Any] = {
        "float32_matmul_precision": torch.get_float32_matmul_precision(),
        "deterministic_algorithms": torch.are_deterministic_algorithms_enabled(),
        "cublas_workspace_config": os.environ.get("CUBLAS_WORKSPACE_CONFIG"),
    }
    out["matmul_allow_tf32"] = bool(torch.backends.cuda.matmul.allow_tf32)
    out["matmul_allow_bf16_reduced_precision"] = bool(
        torch.backends.cuda.matmul.allow_bf16_reduced_precision_reduction)
    out["cudnn_allow_tf32"] = bool(torch.backends.cudnn.allow_tf32)
    out["cudnn_deterministic"] = bool(torch.backends.cudnn.deterministic)
    out["cudnn_benchmark"] = bool(torch.backends.cudnn.benchmark)
    return out


def sdpa_backends(device: Any) -> dict[str, Any]:
    import torch

    cuda = torch.backends.cuda
    out: dict[str, Any] = {
        "flash": {"enabled": cuda.flash_sdp_enabled()},
        "efficient": {"enabled": cuda.mem_efficient_sdp_enabled()},
        "math": {"enabled": cuda.math_sdp_enabled(), "available": True},
        "cudnn": {"enabled": cuda.cudnn_sdp_enabled()},
    }
    if is_cuda(device):
        flash = getattr(cuda, "is_flash_attention_available", None)
        out["flash"]["available"] = bool(flash()) if flash else None
        out["cudnn"]["available"] = bool(torch.backends.cudnn.is_available())
    else:
        out["flash"]["available"] = None
        out["cudnn"]["available"] = False
        out["note"] = "on CPU the kernel a backend names is chosen by torch, not by CUDA"
    return out


def find_cudart() -> Any:
    import torch

    base = Path(torch.__file__).parent
    found = [*glob.glob(str(base / "lib" / "libcudart*.so*")),
             *glob.glob(str(base.parent / "nvidia" / "*" / "lib" / "libcudart.so*"))]
    named = ctypes.util.find_library("cudart")
    for path in [*found, *([named] if named else [])]:
        try:
            return ctypes.CDLL(path)
        except OSError:
            continue
    return None


def cudart_attributes(index: int) -> dict[str, Any]:
    lib = find_cudart()
    if lib is None:
        return {"absent": "libcudart was not found beside torch or on the loader path"}
    out: dict[str, Any] = {}
    for name, code in CUDART_ATTRIBUTES.items():
        value = ctypes.c_int(0)
        rc = lib.cudaDeviceGetAttribute(ctypes.byref(value), code, index)
        out[name] = bool(value.value) if rc == 0 else None
    return out


def unified_memory(props_total: int | None, host_total: int | None,
                   attributes: dict[str, Any]) -> tuple[bool | None, str]:
    if attributes.get("integrated"):
        return True, "the device is integrated with the host (cudaDevAttrIntegrated)"
    if props_total and host_total:
        same = abs(props_total - host_total) <= SAME_POOL_TOLERANCE * host_total
        how = (f"the device's total memory ({props_total} bytes) and the host's "
               f"({host_total} bytes) {'are' if same else 'are not'} one pool by size")
        return same, how
    return None, "neither the integrated attribute nor both totals could be read"


def torch_identity(device: Any, smi: gpu.Smi | None = None,
                   smi_present: bool | None = None) -> dict[str, Any]:
    import torch

    absent: dict[str, str] = {}
    host_total = host_memory_total()
    out: dict[str, Any] = {
        "backend": "torch",
        "accelerator": accelerator_of(device),
        "device": describe_device(device),
        "os": platform.platform()[:80],
        "python": platform.python_version(),
        "machine": platform.machine(),
        "cpu": {"model": cpu_model(), "cores": os.cpu_count()},
        "host_memory_bytes": host_total,
        "versions": {p: version(p) for p in PACKAGES},
        "cuda_runtime": torch.version.cuda,
        "settings": settings(),
        "sdpa": sdpa_backends(device),
        "disk": {"hf_cache": disk(hf_cache()), "home": disk(Path.home())},
    }
    if is_cuda(device):
        index = device_index(device)
        props = torch.cuda.get_device_properties(index)
        cap = (props.major, props.minor)
        attributes = cudart_attributes(index)
        integrated = getattr(props, "is_integrated", None)
        if attributes.get("integrated") is None and integrated is not None:
            attributes["integrated"] = bool(integrated)
        unified, how = unified_memory(int(props.total_memory), host_total, attributes)
        nccl = None
        try:
            v = torch.cuda.nccl.version()
            nccl = ".".join(str(x) for x in v) if isinstance(v, tuple) else str(v)
        except Exception as e:  # noqa: BLE001
            absent["nccl"] = str(e)[:200]
        out.update({
            "gpu": {
                "name": props.name,
                "compute_capability": f"{cap[0]}.{cap[1]}",
                "sm": sm(cap),
                "multiprocessors": props.multi_processor_count,
                "total_memory_bytes": int(props.total_memory),
                "stated_bandwidth_bytes_per_second": STATED_BANDWIDTH.get(props.name),
            },
            "unified_memory": unified,
            "unified_memory_basis": how,
            "memory_attributes": attributes,
            "cudnn": torch.backends.cudnn.version() if torch.backends.cudnn.is_available() else None,
            "nccl": nccl,
            "fp8": cap >= (8, 9),
        })
        if (smi_present if smi_present is not None else gpu.present()):
            out["nvidia_smi"] = (smi or gpu.Smi(index=index)).about()
        else:
            absent["nvidia_smi"] = "nvidia-smi is not on PATH"
    else:
        absent["gpu"] = f"the device is {device}, not CUDA"
    out["absent"] = absent
    return out


def torch_components(me: dict[str, Any], *, checkpoint: str | None = None,
                     dtype: str | None = None) -> dict[str, str | None]:
    versions = me.get("versions") or {}
    smi = me.get("nvidia_smi") or {}
    cudnn = me.get("cudnn")
    return {
        **stack_components(checkpoint=checkpoint, dtype=dtype),
        "backend": "torch",
        "accelerator": me.get("accelerator"),
        "transformers": versions.get("transformers"),
        "nnsight": versions.get("nnsight"),
        "safetensors": versions.get("safetensors"),
        "cuda": me.get("cuda_runtime"),
        "cudnn": str(cudnn) if cudnn is not None else None,
        "nccl": me.get("nccl"),
        "driver": smi.get("driver"),
    }
