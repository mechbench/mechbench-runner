from __future__ import annotations

import csv
import io
import re
import shutil
import sys
from collections.abc import Callable, Sequence
from typing import Any

from .identity import run as _run

Run = Callable[[list[str]], str]
Reading = tuple[int | None, int | None]
Reader = tuple[str, Callable[[], "Reading | str"]]

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


def memory_from(readers: Sequence[Reader]) -> dict[str, Any]:
    reasons: list[str] = []
    for name, read in readers:
        try:
            got = read()
        except Exception as e:  # noqa: BLE001
            got = f"{name} did not answer ({e})"
        if isinstance(got, str):
            reasons.append(got)
            continue
        used, free = got
        if used is None and free is None:
            reasons.append(f"{name} reports the device's memory as N/A")
            continue
        return {"used_bytes": used, "free_bytes": free, "source": name, "reason": None}
    return {"used_bytes": None, "free_bytes": None, "source": None,
            "reason": "; ".join(reasons) or "no reader of the device's memory"}


def nvml_memory(nvml: Any, handle: Any) -> Reading | str:
    info = nvml.nvmlDeviceGetMemoryInfo(handle)
    return int(info.used), int(info.free)


def torch_memory(index: int = 0) -> Reading | str:
    torch = sys.modules.get("torch")
    if torch is None:
        return "torch is not loaded in this process"
    if not torch.cuda.is_initialized():
        return "torch has not initialized CUDA in this process"
    free, total = torch.cuda.mem_get_info(index)
    return int(total) - int(free), int(free)


def fallbacks(index: int = 0) -> list[Reader]:
    held: dict[str, Any] = {}

    def from_nvml() -> Reading | str:
        if "nvml" not in held:
            held["nvml"] = Nvml.open(index)
        found = held["nvml"]
        if found is None:
            return "pynvml is not installed or NVML did not start"
        return nvml_memory(found._nvml, found._handle)  # noqa: SLF001

    return [("nvml", from_nvml), ("torch", lambda: torch_memory(index))]


class Smi:
    def __init__(self, sh: Run = run, index: int = 0,
                 memory: Sequence[Reader] | None = None) -> None:
        self._sh = sh
        self._index = index
        self._unsupported: set[str] = set()
        self._reason_field: str | None = None
        self._memory = list(memory) if memory is not None else fallbacks(index)

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
        used_mib = number(raw.get("memory.used"))

        def from_smi() -> Reading | str:
            if used_mib is None:
                return "nvidia-smi reports memory.used as N/A"
            return int(used_mib * 2**20), None

        return described(
            sm=number(raw.get("clocks.sm")), max_sm=number(raw.get("clocks.max.sm")),
            temperature=number(raw.get("temperature.gpu")), power=number(raw.get("power.draw")),
            power_limit=number(raw.get("power.limit")),
            utilization=number(raw.get("utilization.gpu")),
            memory=memory_from([("nvidia-smi", from_smi), *self._memory]), mask=mask)


def described(*, sm: float | None, max_sm: float | None, temperature: float | None,
              power: float | None, power_limit: float | None, utilization: float | None,
              mask: int | None, memory: dict[str, Any] | None = None) -> dict[str, Any]:
    memory = memory or memory_from(())
    active = reasons(mask)
    return {
        "sm_clock_mhz": sm,
        "max_sm_clock_mhz": max_sm,
        "temperature_c": temperature,
        "power_w": power,
        "power_limit_w": power_limit,
        "utilization_percent": utilization,
        "memory_used_bytes": memory["used_bytes"],
        "memory_free_bytes": memory["free_bytes"],
        "memory_source": memory["source"],
        "memory_reason": memory["reason"],
        "reason_mask": mask,
        "reasons": active,
        "throttling": sorted(set(active or ()) & THROTTLING) if active is not None else None,
    }


class Nvml:
    def __init__(self, nvml: Any, index: int = 0,
                 memory: Sequence[Reader] | None = None) -> None:
        self._nvml = nvml
        self._handle = nvml.nvmlDeviceGetHandleByIndex(index)
        self._memory = (list(memory) if memory is not None
                        else [("torch", lambda: torch_memory(index))])

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
            memory=memory_from([("nvml", lambda: nvml_memory(n, self._handle)),
                                *self._memory]),
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
        "memory": memory_summary(states),
        "sm_clock_mhz": span("sm_clock_mhz"),
        "temperature_c": span("temperature_c"),
        "power_w": span("power_w"),
        "utilization_percent": span("utilization_percent"),
        "throttling": seen,
    }


def memory_summary(states: Sequence[dict[str, Any]]) -> dict[str, Any]:
    def span(key: str) -> dict[str, int] | None:
        values = [s[key] for s in states if s.get(key) is not None]
        return {"min": min(values), "max": max(values)} if values else None

    sources: list[str] = []
    for s in states:
        if s.get("memory_source") and s["memory_source"] not in sources:
            sources.append(s["memory_source"])
    if not sources:
        reasons = [s["memory_reason"] for s in states if s.get("memory_reason")]
        return {"used_bytes": None, "free_bytes": None, "sources": [],
                "reason": reasons[-1] if reasons else "no sample read the device's memory"}
    return {"used_bytes": span("memory_used_bytes"), "free_bytes": span("memory_free_bytes"),
            "sources": sources, "reason": None}
