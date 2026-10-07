from __future__ import annotations

import json
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import gpu, microbench, numerics, sustained, torch_model, torch_probes
from .ambient import Sampler, ambient
from .identity import fingerprint
from .torch_identity import pick_device, torch_components, torch_identity
from .torch_probes import Clock, Grid, Rows

DECODE_BATCH = 32
DECODE_AFFORDABLE_TOKENS_PER_SECOND = 30.0
TRAINING_AFFORDABLE_SECONDS = 45 * 60
TRAINING_MEMORY_BYTES = 110 * 10**9

Say = Callable[[str], None]


def snapshot_of(model: str) -> tuple[str, Path]:
    if Path(model).is_dir():
        path = Path(model).resolve()
        return f"local:{path.name}", path
    from .calibrate import local_snapshot

    found = local_snapshot(model)
    if found is None:
        from mechbench_compute.hub import ensure_model
        repo_id, sha, path = ensure_model(model)
        found = (repo_id, sha, Path(path))
    repo_id, sha, snapshot = found
    return f"{repo_id}@{sha}", snapshot


def micro(clock: Clock, repeats: int, rows: Rows) -> dict[str, dict[str, Any]]:
    measured: dict[str, dict[str, Any]] = {}
    for name in microbench.NAMES:
        step, facts = microbench.TORCH_SETUPS[name]()
        warm = microbench.warm_up(step)
        clock.reset_peak()
        runs = [microbench.timed(step) for _ in range(repeats)]
        m = microbench.summary(runs)
        measured[name] = {**m, "shape": facts["shape"]}
        flops = facts["flops"]
        rows.add(name, facts["shape"], runs, warm[0], probe="canary", dtype=facts["dtype"],
                 peak=clock.peak(), bytes_moved=facts["bytes"],
                 flops_per_second=(flops / m["seconds"]) if flops else None,
                 warmup_repeats=len(warm))
        step = None
        clock.clear()
    return measured


def block_ambient(measured: dict[str, dict[str, Any]], smi: gpu.Smi | None) -> dict[str, Any]:
    base = {"backend": "torch", **{n: {"seconds": m["seconds"]} for n, m in measured.items()}}
    ratios = microbench.canary(base)
    out: dict[str, Any] = {"canary": ratios,
                           "canary_worst": min(ratios.values()) if ratios else None,
                           "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    if smi is not None:
        out["gpu"] = smi.state()
    return out


def run_sustained(clock: Clock, grid: Grid, minutes: float, rows: Rows, smi: gpu.Smi | None,
                  say: Say) -> dict[str, Any] | None:
    shape = {"variant": f"sustained-{grid.sustained_size}"}
    if minutes <= 0:
        rows.skip("sustained", shape, "sustained_minutes is 0")
        return None
    if not clock.cuda:
        rows.skip("sustained", shape, "the sustained-load probe reads the GPU's clocks and "
                  "throttle state, and runs on CUDA only")
        return None
    import torch
    size = grid.sustained_size
    t = {"a": torch.randn(size, size, device=clock.device, dtype=torch.bfloat16),
         "b": torch.randn(size, size, device=clock.device, dtype=torch.bfloat16)}
    say(f"sustained load for {minutes:g} min")
    flops = 2 * size**3
    got = sustained.sustained(lambda: torch.matmul(t["a"], t["b"]), clock.timed, flops=flops,
                              minutes=minutes, sample=smi.state if smi else None)
    t.clear()
    clock.clear()
    common = {"probe": "sustained", "dtype": "bfloat16", "peak": clock.peak()}
    rows.add("matmul", {"variant": f"sustained-burst-{size}"}, got["burst"], got["burst"][0],
             flops_per_second=got["burst_flops_per_second"], **common)
    rows.add("matmul", {"variant": f"sustained-steady-{size}"}, got["steady"], got["steady"][0],
             flops_per_second=got["steady_flops_per_second"],
             **{k: got[k] for k in ("steady_over_burst", "slowed_at_seconds",
                                    "throttle_began_seconds", "slowdown_began_seconds",
                                    "throttled", "duration_seconds", "gpu")},
             **common)
    return {k: v for k, v in got.items() if k not in ("burst", "steady")}


def model_blocks(model_ref: str, clock: Clock, grid: Grid, repeats: int, rows: Rows,
                 measured: dict[str, dict[str, Any]], smi: gpu.Smi | None, bandwidth: float | None,
                 residuals: str | None, against: str | None, workspace: str | None,
                 say: Say) -> dict[str, Any]:
    import torch

    from .calibrate import weight_files

    checkpoint, snapshot = snapshot_of(model_ref)
    files = weight_files(snapshot)
    weight_bytes = sum(f.stat().st_size for f in files)
    dtype = "bfloat16"
    about: dict[str, Any] = {"model": checkpoint, "dtype": dtype, "weight_bytes": weight_bytes}

    def block(name: str) -> None:
        say(name)
        rows.ambient = block_ambient(measured, smi)

    block("load")
    model = torch_model.loads(snapshot, files, clock, torch.bfloat16,
                              max(1, min(repeats, torch_model.LOAD_REPEATS)), weight_bytes, rows,
                              checkpoint, dtype, say)
    common = (clock, grid, repeats, rows, checkpoint, dtype)
    block("first forward")
    torch_model.first_forward(model, clock, grid, rows, checkpoint, dtype)
    block("prefill")
    torch_model.prefill(model, *common, say)
    block("decode")
    torch_model.decode(model, *common, weight_bytes, bandwidth, say)
    block("instrumentation")
    plain = torch_model.ladder(model, *common, say)
    torch_model.attribution(model, *common, plain, say)
    block("lora")
    torch_model.lora(model, *common, say)
    block("numerics")
    theirs = numerics.read_export(against) if against else None
    tokens = (theirs["tokens"] if theirs else
              torch_model.token_ids(model, grid.residual_tokens))
    ids = torch.tensor([tokens], dtype=torch.long, device=model.device)
    numerics.repeats(model, ids, clock, rows, checkpoint, dtype, workspace)
    numerics.paths(model, ids, clock, rows, checkpoint, dtype)
    if residuals or theirs:
        mine = numerics.export(numerics.torch_residuals(model, ids), tokens,
                               {"model": checkpoint, "dtype": dtype, "backend": "torch",
                                "accelerator": rows.stamp.get("accelerator"),
                                "device": rows.stamp.get("device")})
        if residuals:
            Path(residuals).write_text(json.dumps(mine) + "\n")
            about["residuals"] = residuals
        if theirs:
            about["cross_backend"] = numerics.diff_exports(mine, theirs)
    numerics.precision(model, ids, clock, rows, checkpoint, dtype, weight_bytes)
    rows.ambient = None
    model = None
    clock.clear()
    return about


def find(rows: list[dict[str, Any]], **want: Any) -> dict[str, Any] | None:
    for r in rows:
        if all((r.get(k) if k != "variant" else r["shape"].get("variant")) == v
               for k, v in want.items()):
            return r
    return None


def summary(items: list[dict[str, Any]], me: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    copy = find(items, probe="bandwidth")
    if copy:
        out["bandwidth_bytes_per_second"] = copy.get("bytes_per_second")
        out["bandwidth_fraction_of_stated_peak"] = copy.get("fraction_of_stated_peak")
    bf16 = [r for r in items if r.get("probe") == "matmul" and r["dtype"] == "bfloat16"]
    if bf16:
        top = max(bf16, key=lambda r: r.get("flops_per_second") or 0)
        out["matmul_bf16_flops_per_second"] = top.get("flops_per_second")
    steady = next((r for r in items if r.get("probe") == "sustained" and "steady_over_burst" in r),
                  None)
    if steady:
        burst = next(r for r in items if r.get("probe") == "sustained" and r is not steady)
        out.update({"burst_flops_per_second": burst.get("flops_per_second"),
                    "steady_flops_per_second": steady.get("flops_per_second"),
                    "steady_over_burst": steady.get("steady_over_burst"),
                    "throttled": steady.get("throttled"),
                    "throttle_began_seconds": steady.get("throttle_began_seconds")})
    loads = [r for r in items if r["primitive"] == "load"]
    if loads:
        worst = max(loads, key=lambda r: r.get("peak_system_used_bytes") or r["peak_memory_bytes"])
        total = (me.get("gpu") or {}).get("total_memory_bytes") or worst.get("memory_total_bytes")
        out["load"] = {k: worst.get(k) for k in (
            "peak_device_allocated_bytes", "peak_device_reserved_bytes", "peak_host_rss_bytes",
            "peak_system_used_bytes", "system_peak_over_weights", "loads_to_device")}
        out["load"]["memory_total_bytes"] = total
    decodes = [r for r in items if r["primitive"] == "decode" and r["shape"].get("b") == DECODE_BATCH]
    if decodes:
        out["decode_b32"] = {str(r["shape"]["t"]): r.get("tokens_per_second") for r in decodes}
        out["decode_path"] = decodes[0].get("path")
        best = max((r.get("tokens_per_second") or 0) for r in decodes)
        out["decode_affordable"] = "yes" if best > DECODE_AFFORDABLE_TOKENS_PER_SECOND else "no"
    else:
        out["decode_affordable"] = "unmeasured"
    steps = [r for r in items if r["primitive"] == "lora_step"]
    if steps:
        fast = min(steps, key=lambda r: r.get("projected_seconds") or float("inf"))
        out["training"] = {"projected_seconds": fast.get("projected_seconds"),
                           "variant": fast["shape"].get("variant"),
                           "peak_memory_bytes": max(r["peak_memory_bytes"] for r in steps)}
        out["training_affordable"] = (
            "yes" if (fast.get("projected_seconds") or float("inf")) < TRAINING_AFFORDABLE_SECONDS
            else "no")
        out["training_fits"] = ("yes" if out["training"]["peak_memory_bytes"]
                                < TRAINING_MEMORY_BYTES else "no")
    else:
        out["training_affordable"] = "unmeasured"
    return out


def gb(n: Any) -> str:
    return f"{n / 1e9:.1f} GB" if isinstance(n, (int, float)) else "n/a"


def describe(s: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    bw = s.get("bandwidth_bytes_per_second")
    if bw:
        frac = s.get("bandwidth_fraction_of_stated_peak")
        lines.append(f"bandwidth {bw / 1e9:.0f} GB/s"
                     + (f" ({frac:.0%} of the stated peak)" if frac else ""))
    if s.get("matmul_bf16_flops_per_second"):
        lines.append(f"bf16 matmul {s['matmul_bf16_flops_per_second'] / 1e12:.1f} TFLOP/s")
    if s.get("steady_flops_per_second"):
        lines.append(f"sustained {s['burst_flops_per_second'] / 1e12:.1f} → "
                     f"{s['steady_flops_per_second'] / 1e12:.1f} TFLOP/s "
                     f"(steady/burst {s['steady_over_burst']:.2f}, throttled {s['throttled']}"
                     + (f" from {s['throttle_began_seconds']:.0f} s"
                        if s.get("throttle_began_seconds") is not None else "") + ")")
    if s.get("load"):
        ld = s["load"]
        lines.append(f"load peak: device {gb(ld['peak_device_allocated_bytes'])}, system "
                     f"{gb(ld['peak_system_used_bytes'])} of {gb(ld['memory_total_bytes'])}; "
                     f"{ld['loads_to_device']}")
    if s.get("decode_b32"):
        per = ", ".join(f"t={t}: {v:.1f} tok/s" for t, v in s["decode_b32"].items() if v)
        lines.append(f"decode at batch {DECODE_BATCH} ({s.get('decode_path')}): {per}; "
                     f"affordable {s['decode_affordable']}")
    else:
        lines.append(f"decode at batch {DECODE_BATCH}: unmeasured")
    if s.get("training"):
        tr = s["training"]
        lines.append(f"training: {tr['projected_seconds'] / 60:.1f} min for 60 steps "
                     f"({tr['variant']}), peak {gb(tr['peak_memory_bytes'])}; affordable "
                     f"{s['training_affordable']}, fits {s['training_fits']}")
    else:
        lines.append("training: unmeasured (no LoRA training on torch)")
    return lines


def calibrate_torch(model: str | None, *, repeats: int = 10, device: str | None = None,
                    sustained_minutes: float = sustained.DEFAULT_MINUTES,
                    write_baseline: bool = False, baseline: Path | None = None,
                    residuals: str | None = None, against: str | None = None,
                    grid: Grid | None = None, smi: gpu.Smi | None | bool = None,
                    say: Say = lambda _: None) -> dict[str, Any]:
    import os

    workspace = os.environ.get("CUBLAS_WORKSPACE_CONFIG")
    dev = pick_device(device)
    clock = Clock(dev)
    grid = grid or torch_probes.grid_for(dev)
    rows = Rows()
    say("first calls")
    torch_probes.first_calls(clock, rows)
    if smi is None:
        smi = gpu.Smi(index=dev.index or 0) if clock.cuda and gpu.present() else None
    smi = smi or None
    me = torch_identity(dev, smi=smi, smi_present=smi is not None)
    rows.stamp = {"backend": "torch", "accelerator": me["accelerator"], "device": me["device"]}
    chip = (me.get("gpu") or {}).get("name") or (me.get("cpu") or {}).get("model") or "unknown"
    sampler = Sampler(smi=smi or False).start()
    taken_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    about: dict[str, Any] = {}
    held_sustained = None
    try:
        say("micro-benchmarks")
        measured = micro(clock, repeats, rows)
        stated = (me.get("gpu") or {}).get("stated_bandwidth_bytes_per_second")
        bandwidth = torch_probes.bandwidth(clock, grid, repeats, rows, stated)
        done = {(microbench.MATMUL_N, "bfloat16")}
        torch_probes.matmul(clock, grid, repeats, rows, capability_of(me), done)
        torch_probes.launch(clock, repeats, rows)
        torch_probes.transfer(clock, grid, repeats, rows)
        torch_probes.attention(clock, grid, repeats, rows)
        held_sustained = run_sustained(clock, grid, sustained_minutes, rows, smi, say)
        if model:
            about = model_blocks(model, clock, grid, repeats, rows, measured, smi, bandwidth,
                                 residuals, against, workspace, say)
    finally:
        sampler.stop()
    components = torch_components(me, checkpoint=about.get("model"), dtype=about.get("dtype"))
    stack = fingerprint(components)
    items = [{**r, **rows.stamp, "chip": chip, "stack": stack} for r in rows.items]
    amb, quiet = ambient(sampler.result(), None, None)
    if write_baseline:
        microbench.save_baseline(measured, {"chip": chip}, stack, baseline, backend="torch")
    from .calibrate import KEY, item_kind

    gate = summary(items, me)
    for line in describe(gate):
        say(line)
    return {
        "kind": "collection",
        "item_kind": item_kind(),
        "key": list(KEY),
        "backend": "torch",
        "machine": {"chip": chip, "memory_gb": round((me.get("host_memory_bytes") or 0) / 2**30),
                    "gpu_cores": (me.get("gpu") or {}).get("multiprocessors"),
                    "os": me.get("os"), "python": me.get("python")},
        "identity": me,
        "stack_components": components,
        "taken_at": taken_at,
        "quiet": quiet,
        "ambient": amb,
        "grid": {"reduced": grid.reduced},
        "model": about,
        "sustained": held_sustained,
        "summary": gate,
        "skipped": rows.skipped,
        "items": sorted(items, key=lambda i: tuple(str(i.get(k)) for k in (*KEY, "id"))),
    }


def capability_of(me: dict[str, Any]) -> tuple[int, int] | None:
    cc = (me.get("gpu") or {}).get("compute_capability")
    if not cc:
        return None
    major, _, minor = str(cc).partition(".")
    return int(major), int(minor or 0)
