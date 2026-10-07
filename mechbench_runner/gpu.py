from __future__ import annotations

import csv
import io
import re
import shutil
from collections.abc import Callable, Sequence
from typing import Any

from .identity import run as _run

Run = Callable[[list[str]], str]

SMI = "nvidia-smi"
REASON_FIELDS = ("clocks_event_reasons.active", "clocks_throttle_reasons.active")
ABOUT_FIELDS = ("name", "compute_cap", "driver_version", "memory.total", "power.limit",
                "power.default_limit", "power.max_limit", "persistence_mode",
                "mig.mode.current", "clocks.max.sm", "clocks.max.memory", "pci.bus_id")
STATE_FIELDS = ("clocks.sm", "clocks.max.sm", "temperature.gpu", "power.draw", "power.limit",
                "utilization.gpu", "memory.used")

REASONS: tuple[tuple[int, str], ...] = (
    (0x1, "gpu_idle"),
    (0x2, "applications_clocks_setting"),
    (0x4, "sw_power_cap"),
    (0x8, "hw_slowdown"),
    (0x10, "sync_boost"),
    (0x20, "sw_thermal_slowdown"),
    (0x40, "hw_thermal_slowdown"),
    (0x80, "hw_power_brake_slowdown"),
    (0x100, "display_clock_setting"),
)
THROTTLING = frozenset({"sw_power_cap", "hw_slowdown", "sw_thermal_slowdown",
                        "hw_thermal_slowdown", "hw_power_brake_slowdown"})
SLOWDOWN = THROTTLING - {"sw_power_cap"}

ABSENT = re.compile(r"^\[?(n/?a|not supported|unknown error|insufficient permissions)\]?$", re.I)
NUMBER = re.compile(r"^-?\d+(\.\d+)?")
CUDA_VERSION = re.compile(r"CUDA Version:\s*([\d.]+)")


def run(cmd: list[str]) -> str:
    return _run(cmd, timeout=10.0)


def present(which: Callable[[str], str | None] = shutil.which) -> bool:
    return which(SMI) is not None


def cell(raw: str | None) -> str | None:
    if raw is None:
        return None
    text = raw.strip()
    if not text or ABSENT.match(text):
        return None
    return text


def number(raw: str | None) -> float | None:
    text = cell(raw)
    if text is None:
        return None
    found = NUMBER.match(text)
    return float(found.group(0)) if found else None


def parse_csv(out: str, fields: Sequence[str]) -> list[dict[str, str | None]]:
    rows: list[dict[str, str | None]] = []
    for line in csv.reader(io.StringIO(out), skipinitialspace=True):
        if not line or len(line) != len(fields):
            continue
        rows.append({f: cell(v) for f, v in zip(fields, line, strict=True)})
    return rows


def mask_of(raw: str | int | None) -> int | None:
    if raw is None or isinstance(raw, int):
        return raw
    text = cell(raw)
    if text is None:
        return None
    try:
        return int(text, 16) if text.lower().startswith("0x") else int(text)
    except ValueError:
        return None


def reasons(raw: str | int | None) -> list[str] | None:
    value = mask_of(raw)
    if value is None:
        return None
    return [name for bit, name in REASONS if value & bit]


class Smi:
    def __init__(self, sh: Run = run, index: int = 0) -> None:
        self._sh = sh
        self._index = index
        self._unsupported: set[str] = set()
        self._reason_field: str | None = None

    def _query(self, fields: Sequence[str]) -> dict[str, str | None] | None:
        out = self._sh([SMI, f"--id={self._index}", f"--query-gpu={','.join(fields)}",
                        "--format=csv,noheader,nounits"])
        rows = parse_csv(out, fields)
        return rows[0] if rows else None

    def query(self, fields: Sequence[str]) -> dict[str, str | None]:
        wanted = [f for f in fields if f not in self._unsupported]
        got = self._query(wanted) if wanted else {}
        if got is not None:
            return {f: got.get(f) for f in fields}
        out: dict[str, str | None] = {}
        for f in wanted:
            one = self._query([f])
            if one is None:
                self._unsupported.add(f)
                out[f] = None
            else:
                out[f] = one[f]
        return {f: out.get(f) for f in fields}

    def reason_field(self) -> str | None:
        if self._reason_field is None:
            for f in REASON_FIELDS:
                if f in self._unsupported:
                    continue
                if self._query([f]) is not None:
                    self._reason_field = f
                    break
                self._unsupported.add(f)
        return self._reason_field

    def cuda_driver(self) -> str | None:
        found = CUDA_VERSION.search(self._sh([SMI]))
        return found.group(1) if found else None

    def about(self) -> dict[str, Any]:
        raw = self.query(ABOUT_FIELDS)
        total_mib = number(raw.get("memory.total"))
        return {
            "name": raw.get("name"),
            "compute_capability": raw.get("compute_cap"),
            "driver": raw.get("driver_version"),
            "cuda_driver": self.cuda_driver(),
            "memory_total_bytes": int(total_mib * 2**20) if total_mib is not None else None,
            "memory_reported": raw.get("memory.total") is not None,
            "power_limit_w": number(raw.get("power.limit")),
            "power_default_limit_w": number(raw.get("power.default_limit")),
            "power_max_limit_w": number(raw.get("power.max_limit")),
            "persistence_mode": raw.get("persistence_mode"),
            "mig_mode": raw.get("mig.mode.current"),
            "max_sm_clock_mhz": number(raw.get("clocks.max.sm")),
            "max_memory_clock_mhz": number(raw.get("clocks.max.memory")),
            "pci_bus_id": raw.get("pci.bus_id"),
        }

    def state(self) -> dict[str, Any] | None:
        field = self.reason_field()
        fields = (*STATE_FIELDS, *((field,) if field else ()))
        raw = self.query(fields)
        if all(v is None for v in raw.values()):
            return None
        mask = mask_of(raw.get(field)) if field else None
        return described(
            sm=number(raw.get("clocks.sm")), max_sm=number(raw.get("clocks.max.sm")),
            temperature=number(raw.get("temperature.gpu")), power=number(raw.get("power.draw")),
            power_limit=number(raw.get("power.limit")),
            utilization=number(raw.get("utilization.gpu")),
            used_mib=number(raw.get("memory.used")), mask=mask)


def described(*, sm: float | None, max_sm: float | None, temperature: float | None,
              power: float | None, power_limit: float | None, utilization: float | None,
              used_mib: float | None, mask: int | None) -> dict[str, Any]:
    active = reasons(mask)
    return {
        "sm_clock_mhz": sm,
        "max_sm_clock_mhz": max_sm,
        "temperature_c": temperature,
        "power_w": power,
        "power_limit_w": power_limit,
        "utilization_percent": utilization,
        "memory_used_bytes": int(used_mib * 2**20) if used_mib is not None else None,
        "reason_mask": mask,
        "reasons": active,
        "throttling": sorted(set(active or ()) & THROTTLING) if active is not None else None,
    }


class Nvml:
    def __init__(self, nvml: Any, index: int = 0) -> None:
        self._nvml = nvml
        self._handle = nvml.nvmlDeviceGetHandleByIndex(index)

    @classmethod
    def open(cls, index: int = 0) -> Nvml | None:
        try:
            import pynvml
            pynvml.nvmlInit()
            return cls(pynvml, index)
        except Exception:  # noqa: BLE001
            return None

    def _read(self, name: str, *args: Any) -> Any:
        fn = getattr(self._nvml, name, None)
        if fn is None:
            return None
        try:
            return fn(self._handle, *args)
        except Exception:  # noqa: BLE001
            return None

    def state(self) -> dict[str, Any] | None:
        n = self._nvml
        util = self._read("nvmlDeviceGetUtilizationRates")
        mem = self._read("nvmlDeviceGetMemoryInfo")
        power = self._read("nvmlDeviceGetPowerUsage")
        limit = self._read("nvmlDeviceGetEnforcedPowerLimit")
        mask = (self._read("nvmlDeviceGetCurrentClocksEventReasons")
                if hasattr(n, "nvmlDeviceGetCurrentClocksEventReasons")
                else self._read("nvmlDeviceGetCurrentClocksThrottleReasons"))
        sm_clock = getattr(n, "NVML_CLOCK_SM", 1)
        return described(
            sm=self._read("nvmlDeviceGetClockInfo", sm_clock),
            max_sm=self._read("nvmlDeviceGetMaxClockInfo", sm_clock),
            temperature=self._read("nvmlDeviceGetTemperature",
                                   getattr(n, "NVML_TEMPERATURE_GPU", 0)),
            power=power / 1000 if power is not None else None,
            power_limit=limit / 1000 if limit is not None else None,
            utilization=float(util.gpu) if util is not None else None,
            used_mib=mem.used / 2**20 if mem is not None else None,
            mask=int(mask) if mask is not None else None)


def source(index: int = 0, *, nvml: bool = True, which: Callable[[str], str | None] = shutil.which,
           ) -> Nvml | Smi | None:
    if nvml:
        found = Nvml.open(index)
        if found is not None:
            return found
    return Smi(index=index) if present(which) else None


def summarize(states: Sequence[dict[str, Any]]) -> dict[str, Any] | None:
    if not states:
        return None

    def span(key: str) -> dict[str, float] | None:
        values = [s[key] for s in states if s.get(key) is not None]
        return {"min": min(values), "max": max(values)} if values else None

    seen: list[str] = []
    for s in states:
        for r in s.get("throttling") or ():
            if r not in seen:
                seen.append(r)
    return {
        "samples": len(states),
        "sm_clock_mhz": span("sm_clock_mhz"),
        "temperature_c": span("temperature_c"),
        "power_w": span("power_w"),
        "utilization_percent": span("utilization_percent"),
        "throttling": seen,
    }
