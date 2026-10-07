from __future__ import annotations

import importlib.util
import json
from dataclasses import replace

import pytest

if importlib.util.find_spec("nnsight") is None or importlib.util.find_spec("transformers") is None:
    pytest.skip("compute's torch extra is not installed", allow_module_level=True)
torch = pytest.importorskip("torch")

from mechbench_runner import calibrate as cal  # noqa: E402
from mechbench_runner import (  # noqa: E402
    numerics,
    torch_calibrate,
    torch_model,
    torch_probes,
)
from mechbench_runner.config import Config  # noqa: E402
from mechbench_runner.verbs import Ctx, invoke  # noqa: E402

WORDS = ("<pad>", "<unk>", "<start>", "<end>", "the", "lighthouse", "stood", "at", "end",
         "of", "breakwater,", "and", "every", "evening", "keeper", "climbed", "its")
GRID = replace(torch_probes.CPU_GRID, copy_bytes=4 * 2**20, matmul_sizes=(128,),
               attention_n=(32,), attention_heads=(4, 2, 16), prefill_n=(8, 16),
               prefill_b=(1, 2), max_prefill_tokens=24, decode_t=(8, 64), decode_b=(1, 2),
               decode_new_tokens=3, ladder_n=12, lora_n=24, residual_tokens=6,
               shape_budget_seconds=0.2)


def build(where):
    from tokenizers import Tokenizer, models, pre_tokenizers
    from transformers import (
        Gemma3ForCausalLM,
        Gemma3TextConfig,
        PreTrainedTokenizerFast,
    )

    tok = Tokenizer(models.WordLevel({w: i for i, w in enumerate(WORDS)}, unk_token="<unk>"))
    tok.pre_tokenizer = pre_tokenizers.WhitespaceSplit()
    fast = PreTrainedTokenizerFast(tokenizer_object=tok, pad_token="<pad>", unk_token="<unk>",
                                   bos_token="<start>", eos_token="<end>")
    torch.manual_seed(0)
    model = Gemma3ForCausalLM(Gemma3TextConfig(
        hidden_size=32, num_hidden_layers=4, intermediate_size=64, num_attention_heads=4,
        num_key_value_heads=2, head_dim=8, vocab_size=64, sliding_window=8,
        query_pre_attn_scalar=8, max_position_embeddings=48))
    model.save_pretrained(where)
    fast.save_pretrained(where)
    return str(where)


@pytest.fixture(scope="module")
def tiny(tmp_path_factory):
    return build(tmp_path_factory.mktemp("tiny"))


@pytest.fixture(scope="module")
def collection(tiny, tmp_path_factory):
    out = tmp_path_factory.mktemp("res") / "torch.json"
    said: list[str] = []
    got = torch_calibrate.calibrate_torch(tiny, repeats=2, grid=GRID, smi=False,
                                          residuals=str(out), say=said.append)
    return got, out, said


def by_probe(collection, probe):
    return [r for r in collection["items"] if r.get("probe") == probe]


def variant(row):
    return row["shape"].get("variant")


def test_every_row_carries_backend_accelerator_device_and_the_stack(collection):
    got, _, _ = collection
    assert got["backend"] == "torch" and got["items"]
    stack = got["items"][0]["stack"]
    for r in got["items"]:
        assert r["backend"] == "torch" and r["accelerator"] == "cpu" and r["device"] == "cpu"
        assert r["stack"] == stack and r["chip"]
    assert got["stack_components"]["backend"] == "torch"
    assert got["stack_components"]["transformers"] and got["stack_components"]["nnsight"]
    assert len({r["id"] for r in got["items"]}) == len(got["items"])


def test_every_row_validates_as_compute_declares_the_kind(collection):
    kinds = pytest.importorskip("mechbench_compute.lexicon.kinds")
    jsonschema = pytest.importorskip("jsonschema")
    from mechbench_compute.platform_kinds import item_schema

    schema = item_schema(kinds.BY_KIND["platform/calibration"])
    for r in collection[0]["items"]:
        jsonschema.validate(r, schema)


def test_the_machine_probes_run_on_cpu_and_the_cuda_ones_skip_with_a_reason(collection):
    got, _, _ = collection
    assert {variant(r) for r in by_probe(got, "canary")} == {"256MiB", "512x512x512"}
    assert by_probe(got, "bandwidth")[0]["bytes_per_second"] > 0
    assert {r["dtype"] for r in by_probe(got, "matmul")} >= {"bfloat16", "float32"}
    assert {variant(r) for r in by_probe(got, "launch")} == {
        "launch-roundtrip", "launch-chain8", "launch-enqueue256"}
    eager = [r for r in by_probe(got, "attention") if variant(r) == "attention-eager"]
    sdpa = [r for r in by_probe(got, "attention") if variant(r) == "attention-sdpa-math"]
    assert eager and sdpa and sdpa[0]["max_abs_diff_vs_eager"] < 0.1
    skipped = {s["probe"]: s["reason"] for s in got["skipped"]}
    assert "CUDA" in skipped["sustained"] and "one memory" in skipped["transfer"]
    reasons = [s for s in got["skipped"] if s["probe"] == "matmul"]
    assert {s["shape"]["dtype"] for s in reasons} == {"tf32", "float8_e4m3fn"}


def test_load_reports_peak_memory_and_whether_it_reached_the_device(collection):
    got, _, _ = collection
    loads = {variant(r): r for r in got["items"] if r["primitive"] == "load"}
    assert set(loads) == {"cold", "warm"}
    for r in loads.values():
        assert r["peak_host_rss_bytes"] > 0 and r["loads_to_device"] == "host"
        assert r["bytes_per_second"] > 0 and "system_peak_over_weights" in r


def test_prefill_runs_at_batch_and_skips_past_the_token_cap(collection):
    got, _, _ = collection
    shapes = {(r["shape"]["n"], r["shape"]["b"]) for r in by_probe(got, "prefill")}
    assert shapes == {(8, 1), (8, 2), (16, 1)}
    assert any("cap" in s["reason"] for s in got["skipped"] if s["probe"] == "prefill")


def test_decode_runs_at_batch_through_compute_s_batched_generation(collection):
    got, _, _ = collection
    rows = by_probe(got, "decode")
    assert {(r["shape"]["t"], r["shape"]["b"]) for r in rows} == {(8, 1), (8, 2)}
    assert all(r["path"] == "compute" and r["tokens_per_second"] > 0 for r in rows)
    assert all(r["prefill_seconds"] > 0 for r in rows)
    assert all(r["bandwidth_fraction"] is not None for r in rows)
    assert any("positions" in s["reason"] for s in got["skipped"] if s["probe"] == "decode")


def test_the_instrumentation_ladder_has_every_rung_as_a_ratio_to_plain(collection):
    got, _, _ = collection
    rungs = {(r["primitive"], variant(r), r["shape"].get("k")): r for r in by_probe(got, "ladder")}
    assert set(rungs) == {
        ("forward", "plain", None), ("forward", "nnsight", None),
        ("capture", "resid_post", 1), ("capture", "resid_post", 4),
        ("intervene", "ablate-mlp", None), ("forward", "per-head", 1),
        ("forward", "per-head", 4), ("forward", "attn-weights-eager", None)}
    assert rungs[("forward", "plain", None)]["ratio_to_plain"] == 1.0
    capture = rungs[("capture", "resid_post", 4)]
    assert capture["bytes_captured"] == 4 * 12 * 32 * 2
    assert capture["bytes_per_layer_per_ktok"] > 0 and "seconds_per_layer_per_ktok" in capture
    assert all(r["ratio_to_plain"] > 0 for r in rungs.values())
    assert {variant(r) for r in by_probe(got, "attribution")} == {
        "attribute-layer", "attribute-sublayer"}


def test_the_lora_step_is_timed_by_compute_with_and_without_checkpointing(collection):
    got, _, _ = collection
    steps = {variant(r): r for r in by_probe(got, "lora")}
    assert set(steps) == {"plain", "checkpointed"}
    assert steps["plain"]["gradient_checkpointing"] is False
    assert steps["checkpointed"]["gradient_checkpointing"] is True
    for r in steps.values():
        assert r["repeats"] == torch_model.LORA_STEPS and r["tokens_per_second"] > 0
        assert set(r["projected"]) == {"60", "240", "720"}
        assert r["projected_seconds"] == r["projected"]["60"]
        assert r["shape"] == {"n": GRID.lora_n, "b": GRID.lora_b, "variant": variant(r)}
    assert got["summary"]["training_affordable"] in ("yes", "no")


def test_numerics_compare_repeats_paths_and_precision(collection):
    got, _, _ = collection
    rows = {variant(r): r for r in by_probe(got, "numerics")}
    assert rows["repeat-default"]["identical"] is True
    assert rows["repeat-deterministic"]["identical"] is True
    assert rows["repeat-deterministic"]["cost_ratio"] > 0
    assert rows["path-per-head-vs-fused"]["argmax_agreement"] == 1.0
    assert "max_abs" in rows["path-eager-vs-default"]
    precision = rows["precision-vs-float32"]
    assert [p["point"] for p in precision["per_layer"]][:2] == ["embed", "blocks.0.resid_post"]
    assert precision["restored_identical"] is True


def test_the_residual_export_diffs_against_itself_as_identical(collection, tiny):
    got, path, _ = collection
    body = numerics.read_export(path)
    assert body["backend"] == "torch" and len(body["tokens"]) == GRID.residual_tokens
    assert body["points"][0] == "embed" and "logits" in body["points"]
    same = numerics.diff_exports(body, body)
    assert same["comparable"] and same["first_difference"] is None
    assert all(r["max_abs"] == 0.0 for r in same["per_layer"])


def test_a_diff_locates_the_first_point_where_two_backends_part(tmp_path):
    import numpy as np

    def export(values, tokens, backend):
        return numerics.export({k: np.array(v) for k, v in values.items()}, tokens,
                               {"backend": backend})

    home = export({"embed": [[1.0, 2.0]], "blocks.0.resid_post": [[1.0, 2.0]],
                   "blocks.1.resid_post": [[1.0, 2.0]]}, [3, 4], "mlx")
    box = export({"embed": [[1.0, 2.0]], "blocks.0.resid_post": [[1.0, 2.5]],
                  "blocks.1.resid_post": [[1.0, 3.0]]}, [3, 4], "torch")
    diff = numerics.diff_exports(box, home)
    assert diff["first_difference"] == "blocks.0.resid_post"
    assert diff["against"]["backend"] == "mlx"
    assert numerics.diff_exports(box, export({"embed": [[0.0]]}, [9], "mlx"))["comparable"] is False
    (tmp_path / "x.json").write_text(json.dumps({"format": "nope"}))
    with pytest.raises(ValueError):
        numerics.read_export(tmp_path / "x.json")


def test_against_runs_the_home_tokens_and_carries_the_difference(tiny, tmp_path):
    import numpy as np

    home = tmp_path / "home.json"
    tokens = [4, 5, 6, 7]
    home.write_text(json.dumps(numerics.export(
        {"embed": np.zeros((4, 32)), "blocks.0.resid_post": np.zeros((4, 32))}, tokens,
        {"backend": "mlx"})))
    grid = replace(GRID, matmul_sizes=(), attention_n=(), prefill_n=(), decode_t=())
    got = torch_calibrate.calibrate_torch(tiny, repeats=1, grid=grid, smi=False,
                                          sustained_minutes=0, against=str(home))
    cross = got["model"]["cross_backend"]
    assert cross["comparable"] and cross["first_difference"] == "embed"
    assert [r["point"] for r in cross["per_layer"]] == ["embed", "blocks.0.resid_post"]
    assert {r["shape"]["n"] for r in by_probe(got, "numerics")} == {4}


def test_the_summary_prints_the_gate_numbers(collection):
    got, _, said = collection
    s = got["summary"]
    assert s["bandwidth_bytes_per_second"] > 0 and s["load"]["loads_to_device"] == "host"
    assert s["decode_affordable"] == "unmeasured"
    assert s["training"]["projected_seconds"] > 0
    assert any(line.startswith("bandwidth") for line in said)
    assert any(line.startswith("decode at batch 32: unmeasured") for line in said)
    assert any(line.startswith("training: ") and "60 steps" in line for line in said)


def test_each_model_block_carries_the_canary_taken_before_it(collection):
    got, _, _ = collection
    prefill = by_probe(got, "prefill")[0]
    assert set(prefill["ambient"]["canary"]) == {"memcopy", "matmul"}
    assert "gpu" not in prefill["ambient"]


def test_without_compute_s_generation_and_training_decode_falls_back_and_lora_skips(
        tiny, monkeypatch):
    monkeypatch.setattr(torch_model, "capability", lambda name: None)
    grid = replace(GRID, decode_b=(1, 32), decode_t=(8,), prefill_n=(8,), prefill_b=(1,),
                   matmul_sizes=(), attention_n=())
    got = torch_calibrate.calibrate_torch(tiny, repeats=1, grid=grid, smi=False,
                                          sustained_minutes=0)
    decodes = by_probe(got, "decode")
    assert decodes and all(r["path"] == "transformers" for r in decodes)
    assert got["summary"]["decode_path"] == "transformers" and "8" in got["summary"]["decode_b32"]
    lora = [s for s in got["skipped"] if s["probe"] == "lora_step"]
    assert {s["shape"]["variant"] for s in lora} == {"plain", "checkpointed"}
    assert "torch_backend.train_timing.time_training" in lora[0]["reason"]
    assert got["summary"]["training_affordable"] == "unmeasured"


def test_the_gate_judges_training_by_the_60_step_projection(tiny, monkeypatch):
    def time_training(model, *, steps, items, prompt_tokens, rank, targets, checkpointing,
                      project):
        return {"seconds_per_step": 0.5, "tokens_per_s": 10.0, "steps": steps,
                "peak_memory_bytes": 2**30, "gradient_checkpointing": checkpointing,
                "projected_seconds": {k: 0.5 * k for k in project}}

    real = torch_model.capability
    monkeypatch.setattr(torch_model, "capability",
                        lambda name: time_training if name == "train" else real(name))
    grid = replace(GRID, decode_t=(), prefill_n=(), matmul_sizes=(), attention_n=())
    got = torch_calibrate.calibrate_torch(tiny, repeats=1, grid=grid, smi=False,
                                          sustained_minutes=0)
    s = got["summary"]
    assert s["training"]["projected_seconds"] == 30.0
    assert s["training_affordable"] == "yes" and s["training_fits"] == "yes"


def test_the_machine_s_first_backend_is_the_default(monkeypatch):
    from mechbench_runner import identity
    monkeypatch.setattr(identity, "backends", lambda: [])
    assert cal.default_backend() == "mlx"
    monkeypatch.setattr(identity, "backends", lambda: ["torch"])
    assert cal.default_backend() == "torch"


def test_the_verb_passes_the_torch_settings_through(monkeypatch):
    seen: dict = {}

    def fake(model, **kw):
        seen.update(kw, model=model)
        return {"items": [], "machine": {"chip": "NVIDIA GB10"}, "stack_components": {}}

    monkeypatch.setattr(cal, "calibrate", fake)
    ctx = Ctx(Config(api_base_url="http://127.0.0.1:1", api_key="mbk_test",
                     poll_interval_seconds=0.01, warm_model_id=None, runner_id="r"))
    got = invoke(ctx, "runner", "calibrate", {"backend": "torch", "sustained_minutes": 0.5,
                                              "device": "cuda:1", "residuals": "r.json",
                                              "against": "home.json"})
    assert seen["backend"] == "torch" and seen["sustained_minutes"] == 0.5
    assert seen["device"] == "cuda:1" and seen["residuals"] == "r.json"
    assert seen["against"] == "home.json" and seen["model"] is None
    assert "machine's probes" in got["note"]


def test_a_torch_calibration_only_keeps_the_baseline_on_a_torch_machine(monkeypatch):
    seen: dict = {}
    monkeypatch.setattr(torch_calibrate, "calibrate_torch",
                        lambda model, **kw: seen.update(kw) or {})
    monkeypatch.setattr(cal, "default_backend", lambda: "mlx")
    cal.calibrate(None, backend="torch")
    assert seen["write_baseline"] is False and seen["sustained_minutes"] == 5.0
    monkeypatch.setattr(cal, "default_backend", lambda: "torch")
    cal.calibrate(None, sustained_minutes=0)
    assert seen["write_baseline"] is True and seen["sustained_minutes"] == 0
