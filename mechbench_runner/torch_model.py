from __future__ import annotations

import gc
import importlib
import statistics
import threading
import time
import warnings
from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import microbench
from .torch_probes import Clock, Grid, Rows, host_memory, is_oom, measure

CAPABILITIES = {
    "generate": ("mechbench_compute.torch_backend.throughput", "measure_throughput"),
    "train": ("mechbench_compute.torch_backend.train_timing", "time_training"),
}
LOAD_REPEATS = 3
WATCH_SECONDS = 0.02
STAGED_FRACTION = 0.5
LORA_RANK = 8
LORA_TARGETS = ("q_proj", "v_proj")
LORA_STEPS = 5
PROJECTED_STEPS = 60
PROJECT = (60, 240, 720)
TEXT = (
    "The lighthouse stood at the end of the breakwater, and every evening the "
    "keeper climbed its hundred and twelve steps to light the lamp. Ships passed "
    "in the dark, and none of them knew his name. "
)

Say = Callable[[str], None]


def capability(name: str) -> Callable[..., Any] | None:
    module, attribute = CAPABILITIES[name]
    try:
        return getattr(importlib.import_module(module), attribute, None)
    except ImportError:
        return None


def absent(name: str) -> str:
    module, attribute = CAPABILITIES[name]
    from .identity import compute_version
    return f"compute {compute_version() or '(unknown)'} has no {module}.{attribute}"


class HostWatch:
    def __init__(self, interval: float = WATCH_SECONDS) -> None:
        self._interval = interval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.before = host_memory()
        self.peak_rss = self.before.get("rss")
        self.peak_used = self.before.get("used")

    def _take(self) -> None:
        now = host_memory()
        if now.get("rss") is not None:
            self.peak_rss = max(self.peak_rss or 0, int(now["rss"] or 0))
        if now.get("used") is not None:
            self.peak_used = max(self.peak_used or 0, int(now["used"] or 0))

    def _loop(self) -> None:
        while not self._stop.is_set():
            self._take()
            self._stop.wait(self._interval)

    def start(self) -> HostWatch:
        self._thread = threading.Thread(target=self._loop, name="host-watch", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> dict[str, Any]:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5.0)
        self._take()
        return {"host_rss_before_bytes": self.before.get("rss"),
                "peak_host_rss_bytes": self.peak_rss,
                "system_used_before_bytes": self.before.get("used"),
                "peak_system_used_bytes": self.peak_used,
                "memory_total_bytes": self.before.get("total")}


def staging(mem: dict[str, Any], weight_bytes: int, cuda: bool = True) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if weight_bytes <= 0:
        return out
    rss0, rss1 = mem.get("host_rss_before_bytes"), mem.get("peak_host_rss_bytes")
    used0, used1 = mem.get("system_used_before_bytes"), mem.get("peak_system_used_bytes")
    if rss0 is not None and rss1 is not None:
        out["host_rss_over_weights"] = (rss1 - rss0) / weight_bytes
    if used0 is not None and used1 is not None:
        out["system_peak_over_weights"] = (used1 - used0) / weight_bytes
    over = out.get("host_rss_over_weights")
    out["loads_to_device"] = ("host" if not cuda else "unknown" if over is None
                              else "staged" if over >= STAGED_FRACTION else "direct")
    return out


def load_once(snapshot: Path, clock: Clock, dtype: Any) -> tuple[Any, float, dict[str, Any]]:
    from mechbench_compute.torch_backend.model import TorchModel

    clock.clear()
    watch = HostWatch().start()
    clock.reset_peak()
    t = time.perf_counter()
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            model = TorchModel.load(str(snapshot), device=str(clock.device), dtype=dtype)
        clock.sync()
    finally:
        mem = watch.stop()
    seconds = time.perf_counter() - t
    mem["peak_device_allocated_bytes"] = clock.peak() if clock.cuda else None
    mem["peak_device_reserved_bytes"] = clock.peak_reserved()
    return model, seconds, mem


def loads(snapshot: Path, files: list[Path], clock: Clock, dtype: Any, repeats: int,
          weight_bytes: int, rows: Rows, model_ref: str, dtype_name: str, say: Say) -> Any:
    from .calibrate import evict, resident_fraction

    model = None
    for cache in ("cold", "warm"):
        times: list[float] = []
        mems: list[dict[str, Any]] = []
        resident: list[float] = []
        evicted = True
        for i in range(repeats + 1):
            model = None
            clock.clear()
            if cache == "cold":
                evicted = all(evict(f) for f in files) and evicted
                fr = [resident_fraction(f) for f in files]
                if fr and all(x is not None for x in fr):
                    resident.append(sum(fr) / len(fr))  # type: ignore[arg-type]
            say(f"load {cache} {'warm-up' if i == 0 else i}")
            model, seconds, mem = load_once(snapshot, clock, dtype)
            times.append(seconds)
            mems.append(mem)
        timed = mems[1:] or mems
        worst = max(timed, key=lambda m: m.get("peak_system_used_bytes") or 0)
        peak = max(int(m.get("peak_device_allocated_bytes") or m.get("peak_host_rss_bytes") or 0)
                   for m in timed)
        extra: dict[str, Any] = {**worst, **staging(worst, weight_bytes, clock.cuda),
                                 "first_load_seconds": times[0]}
        if cache == "cold":
            extra.update({"evicted": evicted,
                          "resident_before": max(resident) if resident else None})
        rows.add("load", {"variant": cache}, times[1:] or times, times[0], probe="load",
                 dtype=dtype_name, model=model_ref, peak=peak, bytes_moved=weight_bytes, **extra)
    return model


def token_ids(model: Any, n: int) -> list[int]:
    tok = model.tokenizer
    ids: list[int] = []
    while len(ids) < n:
        more = [int(t) for t in tok.encode(TEXT * 4, add_special_tokens=False)]
        if not more:
            more = [0]
        ids.extend(more)
    return ids[:n]


def batch_ids(model: Any, n: int, b: int) -> Any:
    import torch
    row = token_ids(model, n)
    return torch.tensor([row] * b, dtype=torch.long, device=model.device)


def max_positions(model: Any) -> int | None:
    cfg = model._model.config  # noqa: SLF001
    cfg = getattr(cfg, "text_config", None) or cfg
    value = getattr(cfg, "max_position_embeddings", None)
    return int(value) if value else None


def plain_forward(model: Any, ids: Any) -> Any:
    import torch
    with torch.no_grad():
        try:
            return model._model(input_ids=ids, logits_to_keep=1, use_cache=False).logits  # noqa: SLF001
        except TypeError:
            return model._model(input_ids=ids, use_cache=False).logits  # noqa: SLF001


def first_forward(model: Any, clock: Clock, grid: Grid, rows: Rows, ref: str, dtype: str) -> None:
    ids = batch_ids(model, grid.ladder_n, 1)
    first = clock.wall(lambda: model.run(ids))
    second = clock.wall(lambda: model.run(ids))
    rows.add("forward", {"n": grid.ladder_n, "b": 1, "variant": "first-call"}, [second], first,
             probe="first_call", dtype=dtype, model=ref, peak=clock.peak(), first_seconds=first,
             second_seconds=second)


def guarded(rows: Rows, clock: Clock, probe: str, shape: dict[str, Any],
            fn: Callable[[], None]) -> bool:
    try:
        fn()
        return True
    except Exception as e:  # noqa: BLE001
        reason = ("out of memory" if is_oom(e) else type(e).__name__) + f": {e}"
        rows.skip(probe, shape, reason[:300])
        clock.clear()
        return False


def prefill(model: Any, clock: Clock, grid: Grid, repeats: int, rows: Rows, ref: str,
            dtype: str, say: Say) -> None:
    limit = max_positions(model)
    for n in grid.prefill_n:
        for b in grid.prefill_b:
            shape = {"n": n, "b": b}
            if limit is not None and n > limit:
                rows.skip("prefill", shape, f"the model takes at most {limit} positions")
                continue
            if n * b > grid.max_prefill_tokens:
                rows.skip("prefill", shape, f"{n * b} tokens is over the grid's cap of "
                          f"{grid.max_prefill_tokens} per forward")
                continue
            say(f"prefill n={n} b={b}")

            def run(n: int = n, b: int = b, shape: dict[str, Any] = shape) -> None:
                ids = batch_ids(model, n, b)
                clock.reset_peak()
                runs, warm = measure(clock.timed, lambda: plain_forward(model, ids), repeats,
                                     at_least=1, warm_seconds=0.0,
                                     budget=grid.shape_budget_seconds)
                seconds = microbench.summary(runs)["seconds"]
                rows.add("forward", shape, runs, warm[0], probe="prefill", dtype=dtype, model=ref,
                         peak=clock.peak(), tokens_per_second=n * b / seconds,
                         peak_device_reserved_bytes=clock.peak_reserved())
            guarded(rows, clock, "prefill", shape, run)
            clock.clear()


def hf_generate(model: Any, ids: Any, new: int) -> Any:
    import torch
    lm = model._model  # noqa: SLF001
    pad = getattr(model.tokenizer, "pad_token_id", None)
    with torch.no_grad(), warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return lm.generate(input_ids=ids, attention_mask=torch.ones_like(ids), max_new_tokens=new,
                           min_new_tokens=new, do_sample=False, use_cache=True,
                           pad_token_id=pad if pad is not None else 0)


def decoder(model: Any) -> tuple[Callable[[Any, int, int, bool], tuple[float, float]], str]:
    found = capability("generate")
    if found is not None and hasattr(model, "generate_batch"):
        def measured(t: int, b: int, new: int, warm: bool) -> tuple[float, float]:
            got = found(model, batch_sizes=(b,), prompt_tokens=t, new_tokens=new, warmup=warm)[0]
            return float(got["prefill_seconds"]), float(got["decode_seconds"]) / new
        return measured, "compute"

    def generated(t: int, b: int, new: int, warm: bool) -> tuple[float, float]:
        ids = batch_ids(model, t, b)
        if warm:
            hf_generate(model, ids, 1)
        first = Clock(model.device).wall(lambda: hf_generate(model, ids, 1))
        full = Clock(model.device).wall(lambda: hf_generate(model, ids, new))
        return first, max(full - first, 0.0) / (new - 1)
    return generated, "transformers"


def decode(model: Any, clock: Clock, grid: Grid, repeats: int, rows: Rows, ref: str, dtype: str,
           weight_bytes: int, bandwidth: float | None, say: Say) -> None:
    run, path = decoder(model)
    limit = max_positions(model)
    new = max(2, grid.decode_new_tokens)
    for t in grid.decode_t:
        for b in grid.decode_b:
            shape = {"t": t, "b": b}
            if limit is not None and t + new > limit:
                rows.skip("decode", shape, f"the model takes at most {limit} positions")
                continue
            say(f"decode t={t} b={b} ({path})")

            def one(t: int = t, b: int = b, shape: dict[str, Any] = shape) -> None:
                clock.reset_peak()
                prefills: list[float] = []
                steps: list[float] = []
                spent = 0.0
                while len(steps) < max(1, repeats):
                    prefill_s, step_s = run(t, b, new, not steps)
                    prefills.append(prefill_s)
                    steps.append(step_s)
                    spent += prefill_s + step_s * new
                    if spent >= grid.shape_budget_seconds and len(steps) >= 2:
                        break
                step = statistics.median(steps)
                effective = weight_bytes / step if step > 0 else None
                rows.add("decode", shape, steps, steps[0], probe="decode", dtype=dtype, model=ref,
                         peak=clock.peak(), path=path, new_tokens=new,
                         tokens_per_second=b / step if step > 0 else None,
                         prefill_seconds=statistics.median(prefills),
                         effective_bytes_per_second=effective,
                         bandwidth_fraction=(effective / bandwidth
                                             if effective and bandwidth else None))
            guarded(rows, clock, "decode", shape, one)
            clock.clear()


def picks(n_layers: int, k: int) -> list[int]:
    if k >= n_layers:
        return list(range(n_layers))
    if k == 1:
        return [n_layers // 2]
    return sorted({round(i * (n_layers - 1) / (k - 1)) for i in range(k)})


def empty_trace(model: Any, ids: Any) -> None:
    import torch
    from mechbench_compute.torch_backend.tracing import wrap_model

    envoy = wrap_model(model._model)  # noqa: SLF001
    with torch.no_grad(), envoy.trace(input_ids=ids):
        pass


def ladder(model: Any, clock: Clock, grid: Grid, repeats: int, rows: Rows, ref: str, dtype: str,
           say: Say) -> float | None:
    from mechbench_compute.interventions import Ablate

    n = grid.ladder_n
    layers = model.arch.n_layers
    mid = layers // 2
    ids = batch_ids(model, n, 1)
    say(f"instrumentation ladder n={n}")
    clock.reset_peak()
    runs, warm = measure(clock.timed, lambda: model.run(ids), repeats,
                         budget=grid.shape_budget_seconds)
    plain = microbench.summary(runs)["seconds"]
    rows.add("forward", {"n": n, "b": 1, "variant": "plain"}, runs, warm[0], probe="ladder",
             dtype=dtype, model=ref, peak=clock.peak(), ratio_to_plain=1.0)

    def rung(primitive: str, shape: dict[str, Any], step: Callable[[], Any],
             added: bool, **extra: Any) -> None:
        def go() -> None:
            clock.reset_peak()
            runs, warm = measure(clock.timed, step, repeats, budget=grid.shape_budget_seconds)
            whole = microbench.summary(runs)["seconds"]
            timed = [max(r - plain, 0.0) for r in runs] if added else runs
            fields = {"ratio_to_plain": whole / plain if plain else None, **extra}
            if added:
                fields.update({"with_seconds": whole, "without_seconds": plain})
            rows.add(primitive, shape, timed, warm[0], probe="ladder", dtype=dtype, model=ref,
                     peak=clock.peak(), **fields)
        guarded(rows, clock, "ladder", shape, go)

    rung("forward", {"n": n, "b": 1, "variant": "nnsight"}, lambda: empty_trace(model, ids), False)
    for k in sorted({1, min(8, layers), layers}):
        names = [f"blocks.{i}.resid_post" for i in picks(layers, k)]
        held: dict[str, int] = {}

        def capture(names: list[str] = names, held: dict[str, int] = held) -> None:
            out = model.run(ids, capture=names)
            held["bytes"] = sum(int(t.numel() * t.element_size()) for t in out.cache.values())

        capture()
        per = held["bytes"] / len(names) / (n / 1000)
        rung("capture", {"n": n, "k": len(names), "variant": "resid_post"}, capture, True,
             bytes_captured=held["bytes"], layers=picks(layers, k),
             bytes_per_layer_per_ktok=per)
    rung("intervene", {"n": n, "variant": "ablate-mlp"},
         lambda: model.run(ids, interventions=[Ablate.mlp(mid)]), True, layers=[mid])
    for k in (1, layers):
        names = [f"blocks.{i}.attn.per_head_out" for i in picks(layers, k)]
        rung("forward", {"n": n, "b": 1, "k": len(names), "variant": "per-head"},
             lambda names=names: model.run(ids, capture=names), False)
    rung("forward", {"n": n, "b": 1, "variant": "attn-weights-eager"},
         lambda: model.run(ids, capture=[f"blocks.{mid}.attn.weights"]), False, layers=[mid])
    for row in rows.items:
        if row.get("probe") == "ladder" and row["primitive"] == "capture" and row.get("layers"):
            k_layers = len(row["layers"])
            row["seconds_per_layer_per_ktok"] = row["seconds"] / k_layers / (n / 1000)
    return plain


def attribution(model: Any, clock: Clock, grid: Grid, repeats: int, rows: Rows, ref: str,
                dtype: str, plain: float | None, say: Say) -> None:
    from mechbench_compute.ops.logits import attribute as attribute_op

    text = model.tokenizer.decode(token_ids(model, grid.ladder_n))
    n = int(model.tokenize(text, chat_template=False).shape[-1])
    records = [{"id": "calibration", "prompt": text, "template": "raw"}]
    for split in ("layer", "sublayer"):
        shape = {"n": n, "b": 1, "variant": f"attribute-{split}"}
        say(f"attribution by {split}")

        def go(split: str = split, shape: dict[str, Any] = shape) -> None:
            def step() -> None:
                attribute_op.attribute_logits(model, records, {"split": split})
            clock.reset_peak()
            runs, warm = measure(clock.wall, step, repeats, at_least=1, warm_seconds=0.0,
                                 budget=grid.shape_budget_seconds)
            seconds = microbench.summary(runs)["seconds"]
            rows.add("forward", shape, runs, warm[0], probe="attribution", dtype=dtype, model=ref,
                     peak=clock.peak(), ratio_to_plain=seconds / plain if plain else None)
        guarded(rows, clock, "attribution", shape, go)


def lora(model: Any, clock: Clock, grid: Grid, repeats: int, rows: Rows, ref: str, dtype: str,
         say: Say) -> None:
    timing = capability("train")
    for checkpointing in (False, True):
        variant = "checkpointed" if checkpointing else "plain"
        shape = {"n": grid.lora_n, "b": grid.lora_b, "variant": variant}
        if timing is None:
            rows.skip("lora_step", shape, absent("train") + ": no LoRA training on torch")
            continue
        say(f"lora_step {variant}")

        def go(checkpointing: bool = checkpointing, shape: dict[str, Any] = shape) -> None:
            clock.reset_peak()
            got = timing(model, steps=LORA_STEPS, items=grid.lora_b, prompt_tokens=grid.lora_n,
                         rank=LORA_RANK, targets=LORA_TARGETS, checkpointing=checkpointing,
                         project=PROJECT)
            per_step = float(got["seconds_per_step"])
            projected = {str(k): v for k, v in (got.get("projected_seconds") or {}).items()}
            row = rows.add("lora_step", shape, [per_step], per_step, probe="lora", dtype=dtype,
                           model=ref, peak=int(got.get("peak_memory_bytes") or clock.peak()),
                           rank=LORA_RANK, targets=list(LORA_TARGETS),
                           tokens_per_second=got.get("tokens_per_s"),
                           gradient_checkpointing=got.get("gradient_checkpointing"),
                           peak_device_reserved_bytes=clock.peak_reserved(),
                           final_loss=got.get("final_loss"), lora_params=got.get("lora_params"),
                           projected=projected, projected_steps=PROJECTED_STEPS,
                           projected_seconds=projected.get(str(PROJECTED_STEPS),
                                                           PROJECTED_STEPS * per_step))
            row["repeats"] = int(got.get("steps") or LORA_STEPS)
            row["warmup_seconds"] = 0.0
        guarded(rows, clock, "lora_step", shape, go)
        gc.collect()
