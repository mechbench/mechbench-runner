from __future__ import annotations

import functools
import hashlib
import importlib.metadata
import json
import platform
import re
import subprocess
import sys
from typing import Any

STACK_PACKAGES: dict[str, str] = {
    "mlx": "mlx",
    "mlx_lm": "mlx-lm",
    "mlx_vlm": "mlx-vlm",
    "torch": "torch",
}


def run(cmd: list[str], timeout: float = 10.0) -> str:
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                             check=False)
    except (OSError, subprocess.SubprocessError):
        return ""
    return out.stdout if out.returncode == 0 else ""


def chip() -> str | None:
    if sys.platform == "darwin":
        name = run(["sysctl", "-n", "machdep.cpu.brand_string"]).strip()
        if name:
            return name[:80]
    try:
        from mechbench_compute.seeds import hardware_class
        name = hardware_class().get("chip")
    except Exception:  # noqa: BLE001
        name = None
    if name:
        return str(name)[:80]
    return (platform.processor() or platform.machine() or None)


_CORES = re.compile(r'"gpu-core-count"\s*=\s*(\d+)')


def gpu_cores() -> int | None:
    if sys.platform != "darwin":
        return None
    found = _CORES.search(run(["ioreg", "-r", "-d", "1", "-c", "IOAccelerator"]))
    if found:
        return int(found.group(1))
    raw = run(["system_profiler", "-json", "SPDisplaysDataType"], timeout=30.0)
    try:
        displays = json.loads(raw).get("SPDisplaysDataType", [])
    except ValueError:
        return None
    for d in displays:
        cores = d.get("sppci_cores")
        if cores is not None and str(cores).isdigit():
            return int(cores)
    return None


def os_build() -> str:
    if sys.platform == "darwin":
        version = platform.mac_ver()[0] or run(["sw_vers", "-productVersion"]).strip()
        build = run(["sw_vers", "-buildVersion"]).strip()
        return f"macOS {version} ({build})" if build else f"macOS {version}"
    return platform.platform()[:80]


def stack() -> dict[str, str | None]:
    out: dict[str, str | None] = {}
    for key, dist in STACK_PACKAGES.items():
        try:
            out[key] = importlib.metadata.version(dist)
        except importlib.metadata.PackageNotFoundError:
            out[key] = None
    return out


def advertised() -> dict[str, Any]:
    try:
        from mechbench_compute.backends import advertise
        return dict(advertise())
    except Exception:  # noqa: BLE001
        return {}


def backends() -> list[str]:
    return [str(b) for b in advertised().get("backends") or []]


def accelerators() -> dict[str, list[str]] | None:
    found = advertised()
    if not found:
        return None
    pairs = found.get("accelerators")
    if isinstance(pairs, dict):
        return {str(a): [str(b) for b in names] for a, names in pairs.items()}
    has = [str(b) for b in found.get("backends") or []]
    return {str(found["accelerator"]): has} if found.get("accelerator") and has else {}


def architecture_levels_by_backend() -> dict[str, dict[str, str]] | None:
    try:
        from mechbench_compute import support
        by_backend = getattr(support, "architecture_levels_by_backend", None)
        if by_backend is None:
            return None
        return {str(b): {str(k): str(v) for k, v in sorted(levels.items())}
                for b, levels in by_backend().items()}
    except Exception:  # noqa: BLE001
        return None


def architecture_levels() -> dict[str, str]:
    try:
        from mechbench_compute import support
        levels = getattr(support, "architecture_levels", None)
        if levels is not None:
            return {str(k): str(v) for k, v in levels().items()}
        return {a["modelType"]: a["level"] for a in support.local_architectures()}
    except Exception:  # noqa: BLE001
        return {}


def compute_version() -> str:
    try:
        from mechbench_compute import __version__
    except ImportError:
        return ""
    return str(__version__)


@functools.lru_cache(maxsize=1)
def identity() -> dict[str, Any]:
    levels = architecture_levels()
    out: dict[str, Any] = {
        "chip": chip(),
        "gpu_cores": gpu_cores(),
        "os": os_build(),
        "python": platform.python_version(),
        "stack": stack(),
        "backends": backends(),
        "accelerators": accelerators(),
        "architectures": sorted(levels),
        "architecture_levels": dict(sorted(levels.items())),
        "architecture_levels_by_backend": architecture_levels_by_backend(),
    }
    return {k: v for k, v in out.items() if v is not None}


def stack_components(*, checkpoint: str | None = None,
                     dtype: str | None = None) -> dict[str, str | None]:
    me = identity()
    st = me.get("stack", {})
    return {
        "compute": compute_version() or None,
        "mlx": st.get("mlx"),
        "mlx_lm": st.get("mlx_lm"),
        "mlx_vlm": st.get("mlx_vlm"),
        "torch": st.get("torch"),
        "os": me.get("os"),
        "python": me.get("python"),
        "checkpoint": checkpoint,
        "dtype": dtype,
    }


def fingerprint(components: dict[str, str | None]) -> str:
    text = "\n".join(f"{k}={components[k] or ''}" for k in sorted(components))
    return "sha256:" + hashlib.sha256(text.encode()).hexdigest()
