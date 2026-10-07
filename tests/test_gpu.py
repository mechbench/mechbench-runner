from __future__ import annotations

from pathlib import Path

import pytest

from mechbench_runner import gpu

FIXTURES = Path(__file__).parent / "fixtures" / "nvidia_smi"
STATE = (*gpu.STATE_FIELDS, "clocks_event_reasons.active")
OLD_STATE = (*gpu.STATE_FIELDS, "clocks_throttle_reasons.active")


def row(name: str) -> list[str]:
    return [c.strip() for c in (FIXTURES / name).read_text().strip().split(",")]


class FakeSmi:
    def __init__(self, about: str = "gb10-about.csv", state: str = "gb10-state-busy.csv",
                 refuse: tuple[str, ...] = ()) -> None:
        self.values = dict(zip(gpu.ABOUT_FIELDS, row(about), strict=True))
        values = row(state)
        self.values.update(zip(STATE, values, strict=True))
        self.values["clocks_throttle_reasons.active"] = values[-1]
        self.refuse = set(refuse)
        self.calls: list[list[str]] = []

    def set_state(self, name: str) -> None:
        values = row(name)
        self.values.update(zip(STATE, values, strict=True))
        self.values["clocks_throttle_reasons.active"] = values[-1]

    def __call__(self, cmd: list[str]) -> str:
        self.calls.append(cmd)
        if cmd == ["nvidia-smi"]:
            return (FIXTURES / "gb10-table.txt").read_text()
        query = next((c for c in cmd if c.startswith("--query-gpu=")), None)
        if query is None:
            return ""
        fields = query.removeprefix("--query-gpu=").split(",")
        if set(fields) & self.refuse:
            return ""
        return ", ".join(self.values[f] for f in fields) + "\n"


def test_absent_cells_read_as_none_whatever_the_driver_writes():
    for raw in ("[N/A]", "N/A", "[Not Supported]", "", "  "):
        assert gpu.cell(raw) is None
    assert gpu.number("41.05 W") == 41.05 and gpu.number("[N/A]") is None


def test_a_throttle_mask_names_its_reasons():
    assert gpu.reasons("0x0000000000000060") == ["sw_thermal_slowdown", "hw_thermal_slowdown"]
    assert gpu.reasons("0x0000000000000001") == ["gpu_idle"]
    assert gpu.reasons("[N/A]") is None and gpu.reasons(4) == ["sw_power_cap"]


def test_the_gb10_identity_has_no_memory_total_and_names_its_cuda_driver():
    about = gpu.Smi(FakeSmi()).about()
    assert about["name"] == "NVIDIA GB10" and about["compute_capability"] == "12.1"
    assert about["driver"] == "580.95.05" and about["cuda_driver"] == "13.0"
    assert about["memory_total_bytes"] is None and about["memory_reported"] is False
    assert about["power_limit_w"] is None and about["persistence_mode"] == "Enabled"
    assert about["mig_mode"] is None and about["max_sm_clock_mhz"] == 3003


def test_a_discrete_gpu_reports_its_memory_and_power_limit():
    about = gpu.Smi(FakeSmi("h100-about.csv", "h100-state-power-capped.csv")).about()
    assert about["memory_total_bytes"] == 81559 * 2**20
    assert about["power_limit_w"] == 700.0 and about["mig_mode"] == "Disabled"


def test_a_throttled_sample_carries_its_clock_temperature_power_and_reasons():
    sh = FakeSmi(state="gb10-state-throttled.csv")
    state = gpu.Smi(sh).state()
    assert state["sm_clock_mhz"] == 1785 and state["temperature_c"] == 92
    assert state["power_w"] == 41.05 and state["utilization_percent"] == 100
    assert state["memory_used_bytes"] is None and state["reason_mask"] == 0x60
    assert state["throttling"] == ["hw_thermal_slowdown", "sw_thermal_slowdown"]


def test_power_capping_is_throttling_but_not_a_slowdown():
    state = gpu.Smi(FakeSmi("h100-about.csv", "h100-state-power-capped.csv")).state()
    assert state["throttling"] == ["sw_power_cap"]
    assert not set(state["throttling"]) & gpu.SLOWDOWN
    assert state["memory_used_bytes"] == 40211 * 2**20


def test_an_old_driver_without_clocks_event_reasons_falls_back_to_throttle_reasons():
    sh = FakeSmi(state="gb10-state-throttled.csv", refuse=("clocks_event_reasons.active",))
    smi = gpu.Smi(sh)
    assert smi.reason_field() == "clocks_throttle_reasons.active"
    assert smi.state()["throttling"] == ["hw_thermal_slowdown", "sw_thermal_slowdown"]


def test_a_field_the_driver_refuses_is_null_and_the_rest_still_read():
    sh = FakeSmi(refuse=("compute_cap",))
    smi = gpu.Smi(sh)
    about = smi.about()
    assert about["compute_capability"] is None and about["name"] == "NVIDIA GB10"
    before = len(sh.calls)
    smi.about()
    single = [c for c in sh.calls[before:] if "--query-gpu=compute_cap" in c]
    assert single == []


def test_without_nvidia_smi_every_reading_is_absent():
    smi = gpu.Smi(lambda cmd: "")
    assert smi.state() is None
    assert smi.about()["name"] is None and smi.about()["cuda_driver"] is None
    assert gpu.source(nvml=False, which=lambda _: None) is None


def test_the_summary_lists_each_throttle_reason_once():
    sh = FakeSmi()
    smi = gpu.Smi(sh)
    states = [smi.state()]
    sh.set_state("gb10-state-throttled.csv")
    states += [smi.state(), smi.state()]
    got = gpu.summarize(states)
    assert got["samples"] == 3 and got["throttling"] == ["hw_thermal_slowdown",
                                                         "sw_thermal_slowdown"]
    assert got["sm_clock_mhz"] == {"min": 1785, "max": 2490}


class FakeNvml:
    NVML_CLOCK_SM = 1
    NVML_TEMPERATURE_GPU = 0

    class Util:
        gpu = 87

    def nvmlDeviceGetHandleByIndex(self, i):  # noqa: N802
        return i

    def nvmlDeviceGetUtilizationRates(self, h):  # noqa: N802
        return self.Util()

    def nvmlDeviceGetMemoryInfo(self, h):  # noqa: N802
        raise RuntimeError("Not Supported")

    def nvmlDeviceGetPowerUsage(self, h):  # noqa: N802
        return 38700

    def nvmlDeviceGetClockInfo(self, h, kind):  # noqa: N802
        return 2490

    def nvmlDeviceGetTemperature(self, h, kind):  # noqa: N802
        return 63

    def nvmlDeviceGetCurrentClocksEventReasons(self, h):  # noqa: N802
        return 0x40


def test_nvml_reads_the_same_fields_and_a_refused_one_is_null():
    state = gpu.Nvml(FakeNvml()).state()
    assert state["utilization_percent"] == 87 and state["power_w"] == pytest.approx(38.7)
    assert state["memory_used_bytes"] is None and state["sm_clock_mhz"] == 2490
    assert state["throttling"] == ["hw_thermal_slowdown"]
