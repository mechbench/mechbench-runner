from __future__ import annotations

import json

import pytest

from mechbench_runner import calibrate as cal
from mechbench_runner.config import Config
from mechbench_runner.verbs import Ctx, VerbError, invoke

STACK = "sha256:" + "ab" * 32
COLLECTION = {
    "kind": "collection",
    "item_kind": "platform/calibration",
    "key": list(cal.KEY),
    "machine": {"chip": "Apple M5 Ultra", "memory_gb": 256, "gpu_cores": 80,
                "os": "macOS 26.0 (25A354)", "python": "3.12.8"},
    "stack_components": {"compute": "0.171.0"},
    "taken_at": "2026-09-30T00:00:00Z",
    "quiet": True,
    "items": [{"id": "matmul:variant=4096x4096x4096", "chip": "Apple M5 Ultra", "stack": STACK,
               "model": "", "dtype": "bfloat16", "primitive": "matmul",
               "shape": {"variant": "4096x4096x4096"}, "shape_key": "variant=4096x4096x4096",
               "seconds": 0.01, "peak_memory_bytes": 1, "repeats": 10, "spread": 0.001,
               "warmup_seconds": 0.02}],
}


class Bench:
    def __init__(self) -> None:
        self.emitted: list[tuple[str, dict]] = []

    def emit(self, path, payload, inputs=()):
        self.emitted.append((path, payload))
        return {"path": path, "hash": "sha256:x"}


@pytest.fixture
def ctx(monkeypatch):
    c = Ctx(Config(api_base_url="http://127.0.0.1:1", api_key="mbk_test",
                   poll_interval_seconds=0.01, warm_model_id=None, runner_id="r"))
    bench = Bench()
    monkeypatch.setattr(c, "bench", lambda: bench)
    monkeypatch.setattr(cal, "calibrate", lambda model, **kw: {**COLLECTION, "model": model})
    monkeypatch.setattr(cal, "default_model", lambda: None)
    c.fake_bench = bench
    return c


def test_it_answers_the_collection_and_writes_it_where_told(ctx, tmp_path):
    out = tmp_path / "cal.json"
    got = invoke(ctx, "runner", "calibrate", {"model": "m/e2b", "out": str(out)})
    assert got["collection"]["model"] == "m/e2b"
    assert json.loads(out.read_text())["items"][0]["primitive"] == "matmul"
    assert ctx.fake_bench.emitted == []


def test_with_no_model_cached_it_says_only_the_micro_benchmarks_ran(ctx):
    got = invoke(ctx, "runner", "calibrate", {})
    assert "only the micro-benchmarks ran" in got["note"]


def test_push_stores_it_under_the_project_by_chip_and_fingerprint(ctx):
    got = invoke(ctx, "runner", "calibrate", {"model": "m/e2b", "push": True,
                                              "into": "benji/lab"})
    path = "benji/lab/calibration/m5-ultra-" + "ab" * 6
    assert got["pushed"]["path"] == path
    assert ctx.fake_bench.emitted[0][0] == path


def test_push_needs_a_project(ctx):
    with pytest.raises(VerbError):
        invoke(ctx, "runner", "calibrate", {"push": True})


def test_the_item_kind_falls_back_to_records_until_compute_declares_it(monkeypatch):
    lexicon = pytest.importorskip("mechbench_compute.lexicon")
    monkeypatch.setattr(lexicon, "BY_KIND", {})
    assert cal.item_kind() == "records/record"
    monkeypatch.setattr(lexicon, "BY_KIND", {"platform/calibration": object()})
    assert cal.item_kind() == "platform/calibration"


@pytest.mark.parametrize("config,dtype", [
    ({"torch_dtype": "bfloat16"}, "bfloat16"),
    ({"quantization": {"bits": 4, "group_size": 64}}, "q4"),
    ({"text_config": {"dtype": "float16"}}, "float16"),
    ({}, "unknown"),
])
def test_the_dtype_is_read_from_the_config(tmp_path, config, dtype):
    (tmp_path / "config.json").write_text(json.dumps(config))
    assert cal.dtype_of(tmp_path) == dtype


def test_the_page_cache_is_evicted_and_read_back(tmp_path):
    f = tmp_path / "w.safetensors"
    f.write_bytes(b"\0" * (1 << 20))
    f.read_bytes()
    assert cal.evict(f) is True
    assert cal.resident_fraction(f) is not None


def test_a_record_has_every_field_the_kind_requires():
    runs = [0.010, 0.012, 0.011, 0.013]
    r = cal.record("load", {"variant": "cold"}, runs, 0.5, peak_memory=7, bytes_moved=1000)
    assert r["shape_key"] == "variant=cold" and r["id"] == "load:variant=cold"
    assert r["seconds"] == 0.0115 and r["repeats"] == 4
    assert r["spread"] == pytest.approx(0.0015)
    assert r["bytes_per_second"] == pytest.approx(1000 / 0.0115)
    fwd = cal.record("forward", {"n": 128, "b": 1}, runs, 0.5, peak_memory=7, bytes_moved=1000)
    assert fwd["shape_key"] == "n=128,b=1" and "bytes_per_second" not in fwd


def test_it_validates_as_compute_declares_the_kind():
    kinds = pytest.importorskip("mechbench_compute.lexicon.kinds")
    if "platform/calibration" not in kinds.BY_KIND:
        pytest.skip("compute before 0.171.0 declares no platform/calibration")
    jsonschema = pytest.importorskip("jsonschema")
    from mechbench_compute.platform_kinds import item_schema
    schema = item_schema(kinds.BY_KIND["platform/calibration"])
    runs = [0.010, 0.012, 0.011]
    for r in (cal.record("capture", {"n": 128, "k": 4}, runs, 0.0, peak_memory=7, layers=[0, 34]),
              cal.record("memcopy", {"variant": "256MiB"}, runs, 0.1, peak_memory=7,
                         bytes_moved=10)):
        jsonschema.validate({**r, "chip": "Apple M2 Pro", "stack": STACK, "model": "",
                             "dtype": "bfloat16"}, schema)
