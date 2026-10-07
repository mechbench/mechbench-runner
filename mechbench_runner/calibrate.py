from __future__ import annotations

import ctypes
import gc
import json
import os
import statistics
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import microbench
from .ambient import Sampler, ambient
from .identity import fingerprint, identity, stack_components

DEFAULT_MODEL = "mlx-community/gemma-4-e2b-it-bf16"
ITEM_KIND = "platform/calibration"
FALLBACK_ITEM_KIND = "records/record"
KEY = ("chip", "stack", "model", "dtype", "primitive", "shape_key")
SHAPE_KEYS = ("n", "b", "k", "t", "variant")
MOVES_BYTES = ("load", "io", "memcopy")
N = 128
B = 1
K = 4
TARGET_TOKENS = 16
LOAD_REPEATS = 3
WARMUP = 3

PROT_READ = 0x1
MAP_SHARED = 0x1
MS_INVALIDATE = 0x2

TEXT = (
    "The lighthouse stood at the end of the breakwater, and every evening the "
    "keeper climbed its hundred and twelve steps to light the lamp. Ships passed "
    "in the dark, and none of them knew his name. "
)

Say = Callable[[str], None]


def item_kind() -> str:
    try:
        from mechbench_compute.lexicon import BY_KIND
    except Exception:  # noqa: BLE001
        return FALLBACK_ITEM_KIND
    return ITEM_KIND if ITEM_KIND in BY_KIND else FALLBACK_ITEM_KIND


def _libc() -> Any:
    libc = ctypes.CDLL(None, use_errno=True)
    libc.mmap.restype = ctypes.c_void_p
    libc.mmap.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_int, ctypes.c_int,
                          ctypes.c_int, ctypes.c_long]
    libc.msync.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_int]
    libc.munmap.argtypes = [ctypes.c_void_p, ctypes.c_size_t]
    libc.mincore.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_char_p]
    return libc


def _mapped(path: Path, fn: Callable[[Any, int, int], Any]) -> Any:
    libc = _libc()
    fd = os.open(path, os.O_RDONLY)
    try:
        size = os.fstat(fd).st_size
        if size == 0:
            return None
        addr = libc.mmap(None, size, PROT_READ, MAP_SHARED, fd, 0)
        if addr is None or addr == ctypes.c_void_p(-1).value:
            return None
        try:
            return fn(libc, addr, size)
        finally:
            libc.munmap(addr, size)
    finally:
        os.close(fd)


def evict(path: Path) -> bool:
    if hasattr(os, "posix_fadvise"):
        fd = os.open(path, os.O_RDONLY)
        try:
            os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)
            return True
        except OSError:
            return False
        finally:
            os.close(fd)
    return bool(_mapped(path, lambda libc, addr, size: libc.msync(addr, size, MS_INVALIDATE) == 0))


def resident_fraction(path: Path) -> float | None:
    page = os.sysconf("SC_PAGE_SIZE")

    def count(libc: Any, addr: int, size: int) -> float | None:
        pages = (size + page - 1) // page
        vec = ctypes.create_string_buffer(pages)
        if libc.mincore(addr, size, vec) != 0:
            return None
        return sum(1 for b in vec.raw if b & 1) / pages
    return _mapped(path, count)


def weight_files(snapshot: Path) -> list[Path]:
    return sorted(p.resolve() for p in snapshot.glob("*.safetensors"))


def dtype_of(snapshot: Path) -> str:
    try:
        config = json.loads((snapshot / "config.json").read_text())
    except (OSError, ValueError):
        return "unknown"
    q = config.get("quantization") or config.get("quantization_config")
    if isinstance(q, dict) and q.get("bits"):
        return f"q{q['bits']}"
    text = config.get("text_config") if isinstance(config.get("text_config"), dict) else {}
    return str(config.get("torch_dtype") or config.get("dtype") or text.get("torch_dtype")
               or text.get("dtype") or "unknown")


def local_snapshot(model: str) -> tuple[str, str, Path] | None:
    from mechbench_compute.hub import (
        parse_model_ref,
        resolve_cached_revision,
        snapshot_path,
    )

    repo_id, revision = parse_model_ref(model)
    sha = resolve_cached_revision(repo_id, revision)
    if sha is None:
        return None
    path = snapshot_path(repo_id, sha)
    return None if path is None else (repo_id, sha, Path(path))


def _mx() -> Any:
    import mlx.core as mx
    return mx


def reset_peak() -> None:
    mx = _mx()
    (getattr(mx, "reset_peak_memory", None) or mx.metal.reset_peak_memory)()


def peak() -> int:
    mx = _mx()
    return int((getattr(mx, "get_peak_memory", None) or mx.metal.get_peak_memory)())


def clear() -> None:
    gc.collect()
    mx = _mx()
    (getattr(mx, "clear_cache", None) or mx.metal.clear_cache)()


def shape_key(shape: dict[str, Any]) -> str:
    try:
        from mechbench_compute.calibration import write_shape_key
    except ImportError:
        return ",".join(f"{k}={shape[k]}" for k in SHAPE_KEYS if shape.get(k) is not None)
    return write_shape_key(shape)


def record(primitive: str, shape: dict[str, Any], runs: list[float], warmup: float, *,
           peak_memory: int, bytes_moved: int | None = None, **extra: Any) -> dict[str, Any]:
    m = microbench.summary(runs)
    key = shape_key(shape)
    out: dict[str, Any] = {
        "id": f"{primitive}:{key}" if key else primitive,
        "primitive": primitive,
        "shape": dict(shape),
        "shape_key": key,
        "seconds": m["seconds"],
        "peak_memory_bytes": int(peak_memory),
        "repeats": m["repeats"],
        "spread": m["spread"],
        "warmup_seconds": warmup,
    }
    if primitive in MOVES_BYTES and bytes_moved and m["seconds"] > 0:
        out["bytes_per_second"] = bytes_moved / m["seconds"]
    out.update({k: v for k, v in extra.items() if v is not None})
    return out


def micro_records(repeats: int) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    rows: list[dict[str, Any]] = []
    measured: dict[str, dict[str, Any]] = {}
    for name in microbench.NAMES:
        step, facts = microbench.SETUPS[name]()
        warm = microbench.warm_up(step)
        reset_peak()
        runs = [microbench.timed(step) for _ in range(repeats)]
        m = microbench.summary(runs)
        measured[name] = {**m, "shape": facts["shape"]}
        flops = facts["flops"]
        rows.append({**record(name, facts["shape"], runs, warm[0], peak_memory=peak(),
                              bytes_moved=facts["bytes"],
                              flops_per_second=(flops / m["seconds"]) if flops else None,
                              warmup_repeats=len(warm)),
                     "model": "", "dtype": facts["dtype"]})
        del step
        clear()
    return rows, measured


def _tokens(model: Any, n: int) -> list[int]:
    ids: list[int] = []
    while len(ids) < n:
        flat = model.tokenize(TEXT * 4, chat_template=False).tolist()
        while flat and isinstance(flat[0], list):
            flat = flat[0]
        ids.extend(int(t) for t in flat)
    return ids[:n]


def _load(snapshot: Path) -> Any:
    from mechbench_compute import Model
    from mlx.utils import tree_flatten

    model = Model.load(str(snapshot))
    _mx().eval([v for _, v in tree_flatten(model._model.parameters())])  # noqa: SLF001
    return model


def _loads(snapshot: Path, files: list[Path], cache: str, repeats: int, say: Say,
           ) -> tuple[Any, list[float], float, dict[str, Any]]:
    model = None
    times: list[float] = []
    resident: list[float] = []
    evicted = True
    for i in range(repeats + 1):
        model = None
        clear()
        if cache == "cold":
            evicted = all(evict(f) for f in files) and evicted
            fr = [resident_fraction(f) for f in files]
            if fr and all(x is not None for x in fr):
                resident.append(sum(fr) / len(fr))  # type: ignore[arg-type]
        if i == 1:
            reset_peak()
        say(f"load {cache} {'warm-up' if i == 0 else i}")
        t = time.perf_counter()
        model = _load(snapshot)
        times.append(time.perf_counter() - t)
    extra: dict[str, Any] = {}
    if cache == "cold":
        extra = {"evicted": evicted, "resident_before": max(resident) if resident else None}
    return model, times, peak(), extra


def model_records(model_ref: str, repeats: int, say: Say, residuals: str | None = None,
                  ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    mx = _mx()
    found = local_snapshot(model_ref)
    if found is None:
        from mechbench_compute.hub import ensure_model
        say(f"downloading {model_ref}")
        repo_id, sha, path = ensure_model(model_ref)
        found = (repo_id, sha, Path(path))
    repo_id, sha, snapshot = found
    files = weight_files(snapshot)
    weight_bytes = sum(f.stat().st_size for f in files)
    dtype = dtype_of(snapshot)
    checkpoint = f"{repo_id}@{sha}"
    about = {"model": checkpoint, "dtype": dtype, "weight_bytes": weight_bytes}
    rows: list[dict[str, Any]] = []

    def add(r: dict[str, Any]) -> None:
        rows.append({**r, "model": checkpoint, "dtype": dtype})

    model = None
    for cache in ("cold", "warm"):
        model = None
        model, times, top, extra = _loads(snapshot, files, cache,
                                          max(1, min(repeats, LOAD_REPEATS)), say)
        add(record("load", {"variant": cache}, times[1:], times[0], peak_memory=top,
                   bytes_moved=weight_bytes, **extra))

    assert model is not None
    ids = mx.array([_tokens(model, N)])
    layers = model.arch.n_layers
    picks = sorted({round(i * (layers - 1) / (K - 1)) for i in range(K)})
    names = [f"blocks.{i}.resid_post" for i in picks]
    captured = {"bytes": 0}

    def forward() -> None:
        mx.eval(model.run(ids).logits)

    def capture() -> None:
        out = model.run(ids, capture=names)
        tensors = list(out.cache.values())
        mx.eval(out.logits, *tensors)
        captured["bytes"] = sum(int(t.nbytes) for t in tensors)

    say("forward and capture")
    warm = [microbench.timed(forward) for _ in range(WARMUP)]
    [microbench.timed(capture) for _ in range(WARMUP)]
    reset_peak()
    base: list[float] = []
    hooked: list[float] = []
    for _ in range(repeats):
        base.append(microbench.timed(forward))
        hooked.append(microbench.timed(capture))
    top = peak()
    add(record("forward", {"n": N, "b": B}, base, warm[0], peak_memory=top))
    added = [max(h - b, 0.0) for h, b in zip(hooked, base, strict=True)]
    add(record("capture", {"n": N, "k": len(names)}, added, 0.0, peak_memory=top,
               with_capture_seconds=statistics.median(hooked),
               without_capture_seconds=statistics.median(base),
               bytes_captured=captured["bytes"], layers=picks))

    if residuals:
        say("residuals")
        write_mlx_residuals(model, residuals, about)

    import mlx.nn as nn
    import mlx.optimizers as optim
    from mechbench_compute.distill import Example, soft_ce
    from mechbench_compute.lora import apply_lora

    lm = model.lm
    apply_lora(lm, rank=8, seed=0)
    loss_and_grad = nn.value_and_grad(lm, soft_ce)
    opt = optim.Adam(learning_rate=1e-4)
    flat = [int(t) for t in ids[0].tolist()]
    batch = [Example(flat[:-TARGET_TOKENS], flat[-TARGET_TOKENS:])] * B

    def lora_step() -> None:
        loss, grads = loss_and_grad(lm, batch)
        opt.update(lm, grads)
        mx.eval(lm.trainable_parameters(), opt.state, loss)

    say("lora_step")
    warm = [microbench.timed(lora_step) for _ in range(WARMUP)]
    reset_peak()
    steps = [microbench.timed(lora_step) for _ in range(repeats)]
    add(record("lora_step", {"n": N, "b": B}, steps, warm[0], peak_memory=peak(),
               rank=8, targets=["q_proj", "v_proj"], target_tokens=TARGET_TOKENS))
    model = None
    clear()
    return rows, about


def write_mlx_residuals(model: Any, path: str, about: dict[str, Any]) -> None:
    from . import numerics

    tokens = _tokens(model, numerics_tokens())
    body = numerics.export(numerics.mlx_residuals(model, tokens), tokens,
                           {"model": about["model"], "dtype": about["dtype"], "backend": "mlx",
                            "accelerator": "metal", "device": identity().get("chip")})
    Path(path).write_text(json.dumps(body) + "\n")
    about["residuals"] = path


def numerics_tokens() -> int:
    from .torch_probes import Grid
    return Grid().residual_tokens


def default_backend() -> str:
    from .identity import backends
    found = backends()
    return found[0] if found else "mlx"


def calibrate(model: str | None = None, *, repeats: int = 10, write_baseline: bool = True,
              say: Say = lambda _: None, baseline: Path | None = None,
              backend: str | None = None, device: str | None = None,
              sustained_minutes: float | None = None, residuals: str | None = None,
              against: str | None = None) -> dict[str, Any]:
    chosen = backend or default_backend()
    if chosen == "torch":
        from . import sustained, torch_calibrate
        return torch_calibrate.calibrate_torch(
            model, repeats=repeats, device=device,
            sustained_minutes=(sustained.DEFAULT_MINUTES if sustained_minutes is None
                               else sustained_minutes),
            write_baseline=write_baseline and default_backend() == "torch", baseline=baseline,
            residuals=residuals, against=against, say=say)
    if chosen != "mlx":
        raise ValueError(f"calibrate runs on the mlx or torch backend, not {chosen!r}")
    me = identity()
    from .api_client import _hardware
    _, memory_gb = _hardware()
    sampler = Sampler().start()
    taken_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    try:
        say("micro-benchmarks")
        rows, measured = micro_records(repeats)
        about: dict[str, Any] = {}
        if model:
            model_rows, about = model_records(model, repeats, say, residuals)
            rows += model_rows
    finally:
        sampler.stop()
    components = stack_components(checkpoint=about.get("model"), dtype=about.get("dtype"))
    stack = fingerprint(components)
    chip = me.get("chip") or "unknown"
    items = [{**r, "chip": chip, "stack": stack, "backend": "mlx", "accelerator": "metal",
              "device": chip} for r in rows]
    amb, quiet = ambient(sampler.result(), None, None)
    if write_baseline:
        microbench.save_baseline(measured, me, fingerprint(stack_components()), baseline)
    return {
        "kind": "collection",
        "item_kind": item_kind(),
        "key": list(KEY),
        "machine": {"chip": chip, "memory_gb": memory_gb, "gpu_cores": me.get("gpu_cores"),
                    "os": me.get("os"), "python": me.get("python")},
        "stack_components": components,
        "taken_at": taken_at,
        "quiet": quiet,
        "ambient": amb,
        "items": sorted(items, key=lambda i: tuple(str(i.get(k)) for k in KEY)),
    }


def object_path(owner: str, project: str, collection: dict[str, Any]) -> str:
    chip = str(collection["machine"].get("chip") or "unknown")
    slug = "-".join(chip.lower().replace("apple", "").split()) or "unknown"
    stack = collection["items"][0]["stack"] if collection["items"] else fingerprint(
        collection.get("stack_components") or {})
    return f"{owner}/{project}/calibration/{slug}-{str(stack).removeprefix('sha256:')[:12]}"


def default_model() -> str | None:
    try:
        return DEFAULT_MODEL if local_snapshot(DEFAULT_MODEL) is not None else None
    except Exception:  # noqa: BLE001
        return None


def stderr(msg: str) -> None:
    print(f"[calibrate] {msg}", file=sys.stderr, flush=True)
