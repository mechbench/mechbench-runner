from __future__ import annotations

import base64
import contextlib
import json
import os
import time
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path
from typing import Any

import numpy as np

from .torch_probes import Clock, Rows

FORMAT = "mechbench.residuals/1"
DETERMINISTIC_WORKSPACE = ":4096:8"
FP32_HEADROOM = 1.25

Say = Callable[[str], None]


def compare(a: Any, b: Any) -> dict[str, Any]:
    x = np.asarray(a, dtype=np.float64)
    y = np.asarray(b, dtype=np.float64)
    diff = np.abs(x - y)
    scale = np.maximum(np.abs(y), 1e-6)
    out: dict[str, Any] = {
        "identical": bool(np.array_equal(x, y)),
        "max_abs": float(diff.max()) if diff.size else 0.0,
        "max_rel": float((diff / scale).max()) if diff.size else 0.0,
    }
    if x.ndim >= 1 and x.shape[-1] > 1:
        out["argmax_agreement"] = float(np.mean(x.argmax(-1) == y.argmax(-1)))
    return out


def layer_compare(a: dict[str, Any], b: dict[str, Any], points: Sequence[str]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for p in points:
        if p not in a or p not in b:
            continue
        x = np.asarray(a[p], dtype=np.float64).ravel()
        y = np.asarray(b[p], dtype=np.float64).ravel()
        ny = float(np.linalg.norm(y))
        nx = float(np.linalg.norm(x))
        out.append({
            "point": p,
            "max_abs": float(np.abs(x - y).max()) if x.size else 0.0,
            "rel_norm": float(np.linalg.norm(x - y) / ny) if ny else None,
            "cosine": float(x @ y / (nx * ny)) if nx and ny else None,
        })
    return out


def to_numpy(t: Any) -> np.ndarray:
    return t.detach().float().cpu().numpy()


@contextlib.contextmanager
def deterministic() -> Iterator[None]:
    import torch

    held = {
        "env": os.environ.get("CUBLAS_WORKSPACE_CONFIG"),
        "algorithms": torch.are_deterministic_algorithms_enabled(),
        "cudnn_deterministic": torch.backends.cudnn.deterministic,
        "cudnn_benchmark": torch.backends.cudnn.benchmark,
    }
    if held["env"] is None:
        os.environ["CUBLAS_WORKSPACE_CONFIG"] = DETERMINISTIC_WORKSPACE
    torch.use_deterministic_algorithms(True)
    torch.backends.cudnn.deterministic = True
    torch.backends.cudnn.benchmark = False
    try:
        yield
    finally:
        torch.use_deterministic_algorithms(held["algorithms"])
        torch.backends.cudnn.deterministic = held["cudnn_deterministic"]
        torch.backends.cudnn.benchmark = held["cudnn_benchmark"]
        if held["env"] is None:
            os.environ.pop("CUBLAS_WORKSPACE_CONFIG", None)


def logits_of(model: Any, ids: Any, **kw: Any) -> np.ndarray:
    return to_numpy(model.run(ids, **kw).logits[0])


def twice(model: Any, ids: Any, clock: Clock) -> tuple[np.ndarray, np.ndarray, list[float]]:
    held: dict[str, Any] = {}

    def one(key: str) -> None:
        held[key] = model.run(ids).logits

    seconds = [clock.wall(lambda: one("a")), clock.wall(lambda: one("b"))]
    return to_numpy(held["a"][0]), to_numpy(held["b"][0]), seconds


def repeats(model: Any, ids: Any, clock: Clock, rows: Rows, ref: str, dtype: str,
            workspace_at_start: str | None) -> None:
    n = int(ids.shape[-1])
    a, b, default_seconds = twice(model, ids, clock)
    base = min(default_seconds)
    rows.add("numeric", {"n": n, "variant": "repeat-default"}, default_seconds, default_seconds[0],
             probe="numerics", dtype=dtype, model=ref, peak=clock.peak(), **compare(b, a))
    try:
        with deterministic():
            c, d, strict = twice(model, ids, clock)
    except Exception as e:  # noqa: BLE001
        rows.skip("numerics", {"n": n, "variant": "repeat-deterministic"},
                  f"{type(e).__name__}: {e}"[:300])
        return
    vs_default = compare(c, a)
    rows.add("numeric", {"n": n, "variant": "repeat-deterministic"}, strict, strict[0],
             probe="numerics", dtype=dtype, model=ref, peak=clock.peak(), **compare(d, c),
             cost_ratio=min(strict) / base if base else None,
             max_abs_vs_default=vs_default["max_abs"],
             argmax_agreement_vs_default=vs_default.get("argmax_agreement"),
             cublas_workspace_config_at_start=workspace_at_start)


def clone(act: Any, info: Any) -> Any:
    return act.clone()


def paths(model: Any, ids: Any, clock: Clock, rows: Rows, ref: str, dtype: str) -> None:
    from mechbench_compute.torch_backend.tracing import use_attention

    n = int(ids.shape[-1])
    fused = logits_of(model, ids)
    impl = str(getattr(model._model.config, "_attn_implementation", None) or "eager")  # noqa: SLF001
    t = time.perf_counter()
    with use_attention(model._model, "eager"):  # noqa: SLF001
        eager = logits_of(model, ids)
    seconds = time.perf_counter() - t
    rows.add("numeric", {"n": n, "variant": "path-eager-vs-default"}, [seconds], seconds,
             probe="numerics", dtype=dtype, model=ref, peak=clock.peak(), default_attention=impl,
             **compare(eager, fused))
    hooks = {f"blocks.{i}.attn.per_head_out": clone for i in range(model.arch.n_layers)}
    t = time.perf_counter()
    per_head = logits_of(model, ids, hooks=hooks)
    seconds = time.perf_counter() - t
    rows.add("numeric", {"n": n, "variant": "path-per-head-vs-fused"}, [seconds], seconds,
             probe="numerics", dtype=dtype, model=ref, peak=clock.peak(), **compare(per_head, fused))


def residual_points(n_layers: int) -> list[str]:
    return ["embed", *(f"blocks.{i}.resid_post" for i in range(n_layers))]


def torch_residuals(model: Any, ids: Any) -> dict[str, np.ndarray]:
    names = residual_points(model.arch.n_layers)
    out = model.run(ids, capture=names)
    got = {k: to_numpy(v[0]) for k, v in out.cache.items() if k in names}
    got["logits"] = to_numpy(out.logits[0, -1:])
    return got


def precision(model: Any, ids: Any, clock: Clock, rows: Rows, ref: str, dtype: str,
              weight_bytes: int) -> None:

    n = int(ids.shape[-1])
    shape = {"n": n, "variant": "precision-vs-float32"}
    free = clock.free()
    if free is not None and free < FP32_HEADROOM * weight_bytes:
        rows.skip("numerics", shape, f"a float32 copy needs about {weight_bytes} more bytes and "
                  f"{free} are free")
        return
    low = torch_residuals(model, ids)
    params = list(model._model.parameters())  # noqa: SLF001
    held = [p.dtype for p in params]
    t = time.perf_counter()
    try:
        for p in params:
            p.data = p.data.float()
        high = torch_residuals(model, ids)
    except Exception as e:  # noqa: BLE001
        rows.skip("numerics", shape, f"{type(e).__name__}: {e}"[:300])
        high = None
    finally:
        for p, d in zip(params, held, strict=True):
            p.data = p.data.to(d)
        clock.clear()
    seconds = time.perf_counter() - t
    if high is None:
        return
    restored = torch_residuals(model, ids)
    points = residual_points(model.arch.n_layers)
    rows.add("numeric", shape, [seconds], seconds, probe="numerics", dtype=dtype, model=ref,
             peak=clock.peak(), per_layer=layer_compare(low, high, points),
             **{f"logits_{k}": v for k, v in compare(low["logits"], high["logits"]).items()},
             restored_identical=all(np.array_equal(low[k], restored[k]) for k in low))


def encode(a: np.ndarray) -> str:
    return base64.b64encode(np.ascontiguousarray(a, dtype="<f4").tobytes()).decode()


def decode(text: str, shape: Sequence[int]) -> np.ndarray:
    return np.frombuffer(base64.b64decode(text), dtype="<f4").reshape(shape)


def export(values: dict[str, np.ndarray], tokens: list[int], about: dict[str, Any]) -> dict[str, Any]:
    return {
        "format": FORMAT,
        **about,
        "tokens": [int(t) for t in tokens],
        "encoding": "float32-le-base64",
        "points": list(values),
        "shapes": {k: list(v.shape) for k, v in values.items()},
        "values": {k: encode(v) for k, v in values.items()},
    }


def read_export(path: str | Path) -> dict[str, Any]:
    body = json.loads(Path(path).read_text())
    if not isinstance(body, dict) or body.get("format") != FORMAT:
        raise ValueError(f"{path} is not a {FORMAT} export")
    return body


def values_of(body: dict[str, Any]) -> dict[str, np.ndarray]:
    return {k: decode(v, body["shapes"][k]) for k, v in body["values"].items()}


def diff_exports(mine: dict[str, Any], theirs: dict[str, Any]) -> dict[str, Any]:
    if mine.get("tokens") != theirs.get("tokens"):
        return {"comparable": False, "reason": "the two exports ran different token ids"}
    a, b = values_of(mine), values_of(theirs)
    points = [p for p in mine["points"] if p in b and p != "logits"]
    per_layer = layer_compare(a, b, points)
    out: dict[str, Any] = {"comparable": True, "per_layer": per_layer,
                           "against": {k: theirs.get(k) for k in ("backend", "accelerator", "device",
                                                                   "model", "stack", "dtype")}}
    if "logits" in a and "logits" in b:
        out["logits"] = compare(a["logits"], b["logits"])
    first = next((r for r in per_layer if r["max_abs"] > 0), None)
    out["first_difference"] = first["point"] if first else None
    return out


def mlx_residuals(model: Any, tokens: list[int]) -> dict[str, np.ndarray]:
    import mlx.core as mx

    names = [f"blocks.{i}.resid_post" for i in range(model.arch.n_layers)]
    with contextlib.suppress(Exception):
        model.run(mx.array([tokens[:1]]), capture=["embed"])
        names = ["embed", *names]
    out = model.run(mx.array([tokens]), capture=names)
    got = {k: np.array(v[0].astype(mx.float32)) for k, v in out.cache.items() if k in names}
    got["logits"] = np.array(out.logits[0, -1:].astype(mx.float32))
    return got
