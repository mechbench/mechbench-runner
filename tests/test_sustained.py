from __future__ import annotations

import pytest

from mechbench_runner import gpu, sustained

FLOPS = 2 * 8192**3


def chunks(seconds: float, step_at) -> list[dict[str, float]]:
    out = []
    t = 0.25
    while t < seconds:
        out.append({"t": t, "step_seconds": step_at(t)})
        t += 0.5
    return out


def sample(t: float, reasons: int) -> dict:
    return {"t": t, **gpu.described(sm=2400.0 if not reasons else 1800.0, max_sm=3003.0,
                                    temperature=60.0 + t / 10, power=40.0, power_limit=None,
                                    utilization=100.0, used_mib=None, mask=reasons)}


def test_a_machine_that_holds_its_clocks_reports_steady_equal_to_burst():
    got = sustained.analyze(chunks(300, lambda t: 0.1), [sample(t, 0) for t in range(300)], FLOPS)
    assert got["steady_over_burst"] == pytest.approx(1.0)
    assert got["throttled"] == "no" and got["throttle_began_seconds"] is None
    assert got["slowed_at_seconds"] is None
    assert got["burst_flops_per_second"] == pytest.approx(FLOPS / 0.1)


def test_a_machine_that_throttles_reports_when_and_how_far():
    def step_at(t: float) -> float:
        return 0.1 if t < 120 else 0.125

    samples = [sample(t, 0x20 if t >= 118 else 0) for t in range(300)]
    got = sustained.analyze(chunks(300, step_at), samples, FLOPS)
    assert got["steady_over_burst"] == pytest.approx(0.8)
    assert got["throttled"] == "yes"
    assert got["throttle_began_seconds"] == 118 and got["slowdown_began_seconds"] == 118
    assert 119 <= got["slowed_at_seconds"] <= 121
    assert got["gpu"]["throttling"] == ["sw_thermal_slowdown"]
    assert got["gpu"]["sm_clock_mhz"] == {"min": 1800.0, "max": 2400.0}


def test_power_capping_alone_counts_as_throttling_but_not_as_a_slowdown():
    samples = [sample(t, 0x4) for t in range(60)]
    got = sustained.analyze(chunks(60, lambda t: 0.1), samples, FLOPS)
    assert got["throttled"] == "yes" and got["throttle_began_seconds"] == 0
    assert got["slowdown_began_seconds"] is None


def test_without_gpu_samples_the_judgement_rests_on_throughput_alone():
    steady = sustained.analyze(chunks(60, lambda t: 0.1), [], FLOPS)
    slowed = sustained.analyze(chunks(120, lambda t: 0.1 if t < 30 else 0.2), [], FLOPS)
    assert steady["throttled"] == "unknown" and slowed["throttled"] == "yes"


def test_a_short_run_still_splits_burst_from_steady():
    got = sustained.analyze(chunks(20, lambda t: 0.1), [], FLOPS)
    assert got["chunks"] > 0 and got["burst"] and got["steady"]


def test_the_probe_runs_for_its_minutes_and_samples_the_gpu_while_it_runs():
    calls = {"n": 0}

    def step() -> None:
        calls["n"] += 1

    def timed(fn) -> float:
        fn()
        return 0.001

    got = sustained.sustained(step, timed, flops=FLOPS, minutes=0.01, interval=0.05,
                              sample=lambda: gpu.described(
                                  sm=2400.0, max_sm=None, temperature=50.0, power=None,
                                  power_limit=None, utilization=100.0, used_mib=None, mask=0),
                              chunk_seconds=0.05)
    assert calls["n"] > 1 and got["chunks"] >= 1
    assert got["gpu"]["samples"] >= 1 and got["series"][0]["sm_clock_mhz"] == 2400.0
    assert "burst" in got and "steady" in got
