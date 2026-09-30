from __future__ import annotations

import json

from mechbench_runner import ambient, microbench
from mechbench_runner.ambient import NodeWatch, Sampler, model_bearing

GB = 2**30

PS_IDLE = """  101   2.0  90000 WindowServer
  202   0.5  40000 Finder
"""
PS_BUSY = """  101   2.0  90000 WindowServer
  303  85.0  60000 mds_stores
  404  40.0  %d ollama
""" % (18 * GB // 1024)

VM_STAT = """Mach Virtual Memory Statistics: (page size of 16384 bytes)
Pages free:                                    71101.
Pages occupied by compressor:                  99999.
Pageouts:                                       %d.
Swapouts:                                       10.
"""


def shell(ps: str = PS_IDLE, therm: str = "Note: No thermal warning level has been recorded\n",
          gpu: int = 3, pageouts: list[int] | None = None, batt: str = "Now drawing from 'AC Power'",
          lowpower: str = " lowpowermode         0"):
    outs = iter(pageouts or [5, 5, 5, 5])

    def sh(cmd: list[str]) -> str:
        head = " ".join(cmd)
        if head == "pmset -g therm":
            return therm
        if cmd[0] == "ioreg":
            return f'"PerformanceStatistics" = {{"Device Utilization %"={gpu}}}'
        if cmd[0] == "memory_pressure":
            return "System-wide memory free percentage: 61%\n"
        if head == "sysctl vm.swapusage":
            return "vm.swapusage: total = 2048.00M  used = 512.00M  free = 1536.00M"
        if cmd[0] == "vm_stat":
            return VM_STAT % next(outs)
        if cmd[0] == "ps":
            return ps
        if head == "pmset -g batt":
            return batt
        if head == "pmset -g":
            return lowpower
        return ""
    return sh


def sampled(sh, n: int = 2) -> dict:
    s = Sampler(sh=sh, me=1)
    s._power = ambient.power(sh)
    for _ in range(n):
        s.sample()
    return s.result()


class TestCounters:
    def test_an_idle_machine_is_quiet(self):
        counters = sampled(shell())
        assert counters["thermal"] == {"worst": "nominal"}
        assert counters["gpu_utilization"] == {"min": 3, "max": 3}
        assert counters["memory"]["free_percent"] == {"min": 61, "max": 61}
        assert counters["memory"]["swap_used_bytes"]["max"] == 512 * 2**20
        assert counters["busy"] == []
        assert counters["power"] == {"source": "ac", "low_power": False}
        amb, quiet = ambient.ambient(counters, {"memcopy": 0.99, "matmul": 1.01},
                                     {"memcopy": 0.97, "matmul": 1.0})
        assert quiet is True
        assert amb["canary_before"] == 0.99 and amb["canary_after"] == 0.97
        assert amb["reasons"] == []
        json.dumps(amb)

    def test_a_model_server_beside_it_is_not_quiet_and_says_why(self):
        counters = sampled(shell(ps=PS_BUSY))
        assert counters["busy"] == ["mds_stores", "ollama"]
        assert [p["name"] for p in counters["top"]] == ["mds_stores", "ollama", "WindowServer"]
        amb, quiet = ambient.ambient(counters, {"memcopy": 0.6, "matmul": 0.9}, None)
        assert quiet is False
        assert amb["reasons"] == [
            "the canary before the node ran at 0.60 of the idle baseline",
            "mds_stores was busy",
            "ollama was busy",
        ]

    def test_thermal_pressure_and_low_power_are_not_quiet(self):
        counters = sampled(shell(therm="CPU_Speed_Limit \t= 60\n",
                                 batt="Now drawing from 'Battery Power'",
                                 lowpower=" lowpowermode         1"))
        assert counters["thermal"] == {"worst": "serious", "cpu_speed_limit_min": 60}
        amb, quiet = ambient.ambient(counters, None, None)
        assert quiet is False
        assert amb["reasons"] == ["thermal state serious", "low power mode was on"]
        assert amb["canary_before"] is None

    def test_the_thread_samples_until_stopped(self):
        s = Sampler(sh=shell(), me=1, interval=0.01).start()
        s.stop()
        assert s.result()["samples"] >= 1

    def test_pageouts_are_counted_during_the_node(self):
        counters = sampled(shell(pageouts=[5, 5, 9]), n=3)
        assert counters["memory"]["pageouts_during"] == 4


class FakeSampler:
    def __init__(self) -> None:
        self.started = self.stopped = False

    def start(self):
        self.started = True
        return self

    def stop(self) -> None:
        self.stopped = True

    def result(self) -> dict:
        return {"samples": 1, "thermal": {"worst": "nominal"}, "busy": [], "power": {}}


class TestNodeWatch:
    def watch(self, bearing=("capture",)):
        reports: list[tuple[str, dict]] = []
        canaries = iter([{"memcopy": 1.0, "matmul": 0.98}, {"memcopy": 0.7, "matmul": 0.99}])
        w = NodeWatch(set(bearing), lambda n, f: reports.append((n, f)),
                      baseline=lambda: {"memcopy": {"seconds": 1}, "matmul": {"seconds": 1}},
                      canary=lambda base: next(canaries), sampler=FakeSampler,
                      settle=lambda: 0)
        return w, reports

    def test_compute_s_span_gets_both_canaries_and_the_counters(self):
        w, reports = self.watch()
        w.start("capture")
        w.span("capture", {"peak_memory_bytes": 10, "forwards": 3, "model_load_seconds": None,
                           "ambient": None, "quiet": None})
        w.done("capture")
        [(nid, fields)] = reports
        assert nid == "capture"
        assert fields["peak_memory_bytes"] == 10 and fields["forwards"] == 3
        assert "model_load_seconds" not in fields
        assert fields["ambient"]["canary_before"] == 0.98
        assert fields["ambient"]["canary_after"] == 0.7
        assert fields["quiet"] is False

    def test_without_compute_s_span_the_ambient_goes_at_done(self):
        w, reports = self.watch()
        w.start("capture")
        w.done("capture")
        [(nid, fields)] = reports
        assert set(fields) == {"ambient", "quiet"}

    def test_a_pure_node_reports_compute_s_span_and_no_ambient(self):
        w, reports = self.watch()
        w.start("table")
        w.span("table", {"peak_memory_bytes": 5, "ambient": None, "quiet": None})
        w.done("table")
        assert reports == [("table", {"peak_memory_bytes": 5})]

    def test_a_node_that_never_says_done_is_closed_by_the_next(self):
        w, reports = self.watch(bearing=("a", "b"))
        w.start("a")
        w.start("b")
        assert [r[0] for r in reports] == ["a"]
        assert reports[0][1]["ambient"]["canary_after"] is None

    def test_model_bearing_reads_the_graph(self):
        spec = {"graph": {"nodes": [
            {"id": "cap", "requirements": {"class": "mlx-local"}},
            {"id": "tab", "requirements": {"class": "pure"}},
            {"id": "ask", "requirements": {"class": "remote"}},
        ]}}
        assert model_bearing(spec) == {"cap"}
        assert model_bearing({}) == set()


class SlowRelease:
    def __init__(self, release_s: float = 0.3, held: int = 8 * GB) -> None:
        self.now = 0.0
        self.release_s = release_s
        self.held = held
        self.cleared = 0

    def clock(self) -> float:
        return self.now

    def sleep(self, s: float) -> None:
        self.now += s

    def clear(self) -> None:
        self.cleared += 1

    def read(self) -> int:
        if self.now < 0:
            return 0
        left = max(0.0, 1.0 - self.now / self.release_s)
        return int(self.held * left)

    def canary(self, _base) -> dict[str, float]:
        ratio = 0.02 if self.read() > 0 else 1.0
        return {"memcopy": ratio, "matmul": ratio}

    def watch(self, reports: list, settle) -> NodeWatch:
        return NodeWatch({"lora"}, lambda n, f: reports.append((n, f)),
                         baseline=lambda: {"memcopy": {"seconds": 1}, "matmul": {"seconds": 1}},
                         canary=self.canary, sampler=FakeSampler, settle=settle)


class TestSettle:
    def test_a_node_whose_release_is_slow_does_not_fail_quiet_on_it(self):
        fake = SlowRelease(release_s=0.3)
        fake.now = -1.0
        reports: list = []
        w = fake.watch(reports, lambda: ambient.settle(
            fake.clear, fake.read, clock=fake.clock, sleep=fake.sleep))
        w.start("lora")
        fake.now = 0.0
        w.span("lora", {"compute_seconds": 12.0})
        [(nid, fields)] = reports
        assert nid == "lora"
        assert fields["quiet"] is True, fields["ambient"]["reasons"]
        assert fields["ambient"]["canary_after"] == 1.0
        assert 300 <= fields["ambient"]["canary_after_delay_ms"] <= 350
        assert fake.cleared >= 1

    def test_without_the_settle_the_release_reads_as_a_loud_machine(self):
        fake = SlowRelease(release_s=0.3)
        fake.now = -1.0
        reports: list = []
        w = fake.watch(reports, lambda: 0)
        w.start("lora")
        fake.now = 0.0
        w.span("lora", {"compute_seconds": 12.0})
        [(_nid, fields)] = reports
        assert fields["quiet"] is False
        assert fields["ambient"]["canary_after"] == 0.02

    def test_the_settle_is_bounded(self):
        fake = SlowRelease(release_s=60.0)
        ms = ambient.settle(fake.clear, fake.read, clock=fake.clock, sleep=fake.sleep)
        assert 1000 <= ms <= 1050

    def test_memory_that_is_already_still_settles_at_once(self):
        fake = SlowRelease(release_s=0.3)
        fake.now = 5.0
        ms = ambient.settle(fake.clear, fake.read, clock=fake.clock, sleep=fake.sleep)
        assert ms <= 30

    def test_without_mlx_the_settle_is_a_no_op(self, monkeypatch):
        monkeypatch.setattr(ambient, "_mlx", lambda: (None, None))
        assert ambient.settle() <= 5

    def test_the_delay_is_in_the_ambient_and_not_a_reason(self):
        out, quiet = ambient.ambient({"thermal": {"worst": "nominal"}}, None, None, 240)
        assert out["canary_after_delay_ms"] == 240 and quiet


class TestCanary:
    def test_the_ratio_is_the_baseline_over_now(self):
        base = {"memcopy": {"seconds": 0.01}, "matmul": {"seconds": 0.04}}
        now = {"memcopy": 0.02, "matmul": 0.04}
        assert microbench.canary(base, measure_one=lambda n, b: now[n]) == {
            "memcopy": 0.5, "matmul": 1.0}

    def test_no_baseline_no_canary(self):
        assert microbench.canary(None, measure_one=lambda n, b: 1.0) is None

    def test_the_baseline_round_trips(self, tmp_path):
        path = tmp_path / "calibration" / "baseline.json"
        measured = {n: {"seconds": 0.01, "shape": "s", "spread": 0.1} for n in microbench.NAMES}
        microbench.save_baseline(measured, {"chip": "Apple M5 Ultra"}, "sha256:x", path)
        got = microbench.load_baseline(path)
        assert got["chip"] == "Apple M5 Ultra" and got["memcopy"]["seconds"] == 0.01
        path.write_text("{}")
        assert microbench.load_baseline(path) is None

    def test_spread_is_the_interquartile_range_in_seconds(self):
        assert microbench.spread([1.0, 1.0, 1.0, 1.0, 50.0]) == 0.0
        assert microbench.spread([1.0, 2.0, 3.0]) == 1.0
        assert microbench.spread([2.0]) == 0.0


def test_the_span_rides_a_progress_report():
    import httpx

    from mechbench_runner.api_client import ApiClient
    from mechbench_runner.config import Config

    seen: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen.update(json.loads(req.content))
        return httpx.Response(200, json={"ok": True})
    api = ApiClient(Config(api_base_url="http://127.0.0.1:1", api_key="mbk_test",
                           poll_interval_seconds=0.01, warm_model_id=None, runner_id="r"))
    api._client.close()
    api._client = httpx.Client(base_url="http://127.0.0.1:1",
                               transport=httpx.MockTransport(handler))
    api.report_progress("j_1", 1, 2, span={"node": "cap", "ambient": {"samples": 1},
                                           "quiet": True})
    assert seen == {"num": 1, "den": 2,
                    "span": {"node": "cap", "ambient": {"samples": 1}, "quiet": True}}


def test_the_runner_takes_compute_s_span_and_sends_it_with_the_ambient(monkeypatch):
    import inspect

    import pytest
    jr = pytest.importorskip("mechbench_runner.job_runner")
    from mechbench_runner.config import Config

    class StubControl:
        def __init__(self, *_a, **_k):
            pass

    monkeypatch.setattr(jr, "ControlServer", StubControl)
    r = jr.JobRunner(Config(api_base_url="http://127.0.0.1:1", api_key="k",
                            poll_interval_seconds=0.01, warm_model_id=None, runner_id="r"))
    if "on_node_span" in inspect.signature(jr.ProtocolExecutor).parameters:
        assert r._executor._on_node_span == r._node_span
    sent: list = []
    r._watch = NodeWatch({"cap"}, lambda n, f: sent.append((n, f)),
                         baseline=lambda: None, canary=lambda base: None,
                         sampler=FakeSampler)
    r._spool_node_start("cap", "sha256:fp")
    r._node_span("cap", {"forwards": 2, "ambient": None, "quiet": None})
    r._spool_node_done("cap", None, "sha256:fp")
    [(nid, fields)] = sent
    assert nid == "cap" and fields["forwards"] == 2 and fields["quiet"] is True
