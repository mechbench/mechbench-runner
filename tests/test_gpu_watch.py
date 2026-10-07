from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from mechbench_runner import ambient, gpu, gpu_watch, microbench
from mechbench_runner.job_runner import JobRunner
from tests.test_gpu import FakeSmi


class Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


def watch(states: list[str], clock: Clock, available: int | None = 64 * 2**30):
    sh = FakeSmi()
    smi = gpu.Smi(sh)
    w = gpu_watch.JobGpu(smi.state, interval=1.0, clock=clock, available=lambda: available)
    w.start_at = clock.now
    return w, sh, states


def run(w, sh, states, clock, nodes=()):
    for i, name in enumerate(states):
        for nid, at, edge in nodes:
            if at == i:
                (w.node_start if edge == "start" else w.node_end)(nid)
        sh.set_state(name)
        w.take()
        clock.now += 1.0


def test_the_job_summary_has_busy_fraction_idle_seconds_peaks_and_throttling():
    clock = Clock()
    states = ["gb10-state-idle.csv"] * 3 + ["gb10-state-busy.csv"] * 5 + [
        "gb10-state-throttled.csv"] * 2
    w, sh, states = watch(states, clock)
    run(w, sh, states, clock)
    body = w.stop()
    s = body["summary"]
    assert s["samples"] == 10 and s["busy_fraction"] == pytest.approx(0.7)
    assert s["idle_seconds"] == 3.0 and s["utilization_peak_percent"] == 100
    assert s["throttling"] == ["sw_thermal_slowdown", "hw_thermal_slowdown"]
    assert s["min_host_available_bytes"] == 64 * 2**30 and "peak_memory_used_bytes" not in s
    assert s["peak_temperature_c"] == 92
    assert body["format"] == gpu_watch.SERIES_FORMAT


def test_the_raw_series_round_trips_compressed():
    clock = Clock()
    w, sh, states = watch(["gb10-state-idle.csv", "gb10-state-throttled.csv"], clock)
    run(w, sh, states, clock)
    rows = gpu_watch.unpack(w.stop()["series"])
    assert [r["utilization_percent"] for r in rows] == [0, 100]
    assert rows[1]["reason_mask"] == 0x60 and rows[0]["t"] == 0.0
    assert rows[0]["host_available_mib"] == 64 * 1024


def test_each_node_gets_the_busy_fraction_of_its_own_seconds():
    clock = Clock()
    states = ["gb10-state-idle.csv"] * 4 + ["gb10-state-busy.csv"] * 4
    w, sh, states = watch(states, clock)
    run(w, sh, states, clock, nodes=[("load", 0, "start"), ("load", 3, "end"),
                                     ("read", 4, "start")])
    w.node_end("read")
    load, read = w.node_summary("load"), w.node_summary("read")
    assert load["busy_fraction"] == 0.0 and load["idle_seconds"] == 4.0
    assert read["busy_fraction"] == 1.0 and read["seconds"] == 4.0
    assert w.node_summary("never") is None


def test_sampling_is_off_without_a_gpu_or_when_set_to_zero(monkeypatch):
    monkeypatch.setattr(gpu, "source", lambda *a, **k: None)
    assert gpu_watch.JobGpu.for_machine({}) is None
    monkeypatch.setattr(gpu, "source", lambda *a, **k: gpu.Smi(FakeSmi()))
    assert gpu_watch.JobGpu.for_machine({gpu_watch.INTERVAL_ENV: "0"}) is None
    found = gpu_watch.JobGpu.for_machine({gpu_watch.INTERVAL_ENV: "2.5"})
    assert found is not None and found._interval == 2.5  # noqa: SLF001
    assert gpu_watch.interval_setting({gpu_watch.INTERVAL_ENV: "soon"}) == 1.0


def test_a_failing_sample_never_stops_the_watch():
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        raise RuntimeError("nvidia-smi hung")

    w = gpu_watch.JobGpu(flaky, interval=0.01).start()
    import time
    deadline = time.monotonic() + 5
    while calls["n"] < 2 and time.monotonic() < deadline:
        time.sleep(0.01)
    body = w.stop()
    assert calls["n"] >= 2 and body["summary"]["samples"] == 0


def test_the_series_object_sits_beside_the_job_results():
    assert gpu_watch.object_path("benji/lab/results/j_x") == "benji/lab/results/j_x/gpu-series"
    assert gpu_watch.object_path(None) is None and gpu_watch.object_path("x/y") is None


class Api:
    def __init__(self, fail: bool = False) -> None:
        self.put: list[tuple[str, bytes]] = []
        self.fail = fail

    def put_bytes(self, path, data, **kw):
        if self.fail:
            raise RuntimeError("403")
        self.put.append((path, data))
        return {}


def finishing(tmp_path, monkeypatch, api):
    monkeypatch.setattr(gpu_watch, "series_dir", lambda: tmp_path)
    clock = Clock()
    w, sh, states = watch(["gb10-state-busy.csv"] * 2, clock)
    run(w, sh, states, clock)
    holder = SimpleNamespace(_gpu=w)
    JobRunner._finish_gpu(holder, api, "j_1", "benji/lab/results/j_1")
    return holder


def test_the_job_end_writes_the_series_locally_and_stores_it_beside_the_results(tmp_path,
                                                                               monkeypatch):
    api = Api()
    holder = finishing(tmp_path, monkeypatch, api)
    local = json.loads((tmp_path / "j_1.json").read_text())
    assert local["job"] == "j_1" and local["summary"]["busy_fraction"] == 1.0
    assert api.put[0][0] == "benji/lab/results/j_1/gpu-series"
    assert json.loads(api.put[0][1])["series"] == local["series"]
    assert holder._gpu is None  # noqa: SLF001


def test_a_refused_upload_never_fails_the_job(tmp_path, monkeypatch, capsys):
    finishing(tmp_path, monkeypatch, Api(fail=True))
    assert "GPU series not stored" in capsys.readouterr().out
    assert (tmp_path / "j_1.json").exists()


class Busy:
    def __init__(self) -> None:
        self.events: list[tuple[str, str]] = []

    def node_start(self, nid):
        self.events.append(("start", nid))

    def node_end(self, nid):
        self.events.append(("end", nid))

    def node_summary(self, nid):
        return {"busy_fraction": 0.5, "seconds": 2.0}


class FakeSampler:
    def start(self):
        return self

    def stop(self):
        pass

    def result(self):
        return {"samples": 1}


def node_watch(bearing: set[str], busy: Busy | None):
    reports: list[tuple[str, dict]] = []
    w = ambient.NodeWatch(bearing, lambda nid, body: reports.append((nid, body)),
                          baseline=lambda: None, canary=lambda base: None, sampler=FakeSampler,
                          settle=lambda: 0, gpu=busy)
    return w, reports


def test_a_node_without_a_model_still_reports_its_gpu_busy_fraction_once():
    busy = Busy()
    w, reports = node_watch(set(), busy)
    w.start("tokenize")
    w.done("tokenize")
    w.done("tokenize")
    assert reports == [("tokenize", {"ambient": {"gpu_busy": {"busy_fraction": 0.5,
                                                              "seconds": 2.0}}})]
    assert busy.events[0] == ("start", "tokenize")


def test_a_model_node_carries_the_busy_fraction_inside_its_ambient():
    w, reports = node_watch({"read"}, Busy())
    w.start("read")
    w.span("read", {"tokens_in": 10})
    nid, body = reports[0]
    assert nid == "read" and body["tokens_in"] == 10
    assert body["ambient"]["gpu_busy"]["busy_fraction"] == 0.5 and body["ambient"]["samples"] == 1


def test_without_a_gpu_the_spans_are_as_they_were():
    w, reports = node_watch(set(), None)
    w.start("tokenize")
    w.done("tokenize")
    assert reports == []


def test_the_sampler_carries_the_gpu_and_a_slowdown_makes_the_node_not_quiet():
    sh = FakeSmi(state="gb10-state-throttled.csv")
    s = ambient.Sampler(sh=lambda cmd: "", smi=gpu.Smi(sh))
    s.sample()
    got = s.result()
    assert got["gpu"]["throttling"] == ["hw_thermal_slowdown", "sw_thermal_slowdown"]
    amb, quiet = ambient.ambient(got, None, None)
    assert not quiet and any("slowed its clocks" in r for r in amb["reasons"])


def test_a_sampler_on_a_machine_without_nvidia_smi_has_no_gpu_key(monkeypatch):
    monkeypatch.setattr(gpu, "present", lambda *a, **k: False)
    s = ambient.Sampler(sh=lambda cmd: "")
    s.sample()
    assert "gpu" not in s.result()


def test_the_canary_measures_on_the_backend_its_baseline_was_taken_on(monkeypatch):
    seen: list[str] = []

    def for_budget(name, budget, backend="mlx"):
        seen.append(backend)
        return 1.0

    monkeypatch.setattr(microbench, "for_budget", for_budget)
    base = {"memcopy": {"seconds": 1.0}, "matmul": {"seconds": 1.0}}
    microbench.canary(base)
    microbench.canary({**base, "backend": "torch"})
    assert seen == ["mlx", "mlx", "torch", "torch"]
