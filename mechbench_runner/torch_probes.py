from __future__ import annotations

import contextlib
import gc
import os
import re
import time
import warnings
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import microbench
from .torch_identity import is_cuda

Step = Callable[[], Any]

MIN_REPEATS = 2
SDPA_BACKENDS = (("flash", "FLASH_ATTENTION"), ("efficient", "EFFICIENT_ATTENTION"),
                 ("math", "MATH"), ("cudnn", "CUDNN_ATTENTION"))
LAUNCH_CHAIN = 8
LAUNCH_ENQUEUE = 256


@dataclass(frozen=True)
class Grid:
    copy_bytes: int = 2**30
    matmul_sizes: tuple[int, ...] = (2048, 4096, 8192)
    matmul_dtypes: tuple[str, ...] = ("bfloat16", "float16", "tf32", "float32", "float8_e4m3fn")
    attention_n: tuple[int, ...] = (512, 2048, 8192)
    attention_heads: tuple[int, int, int] = (32, 16, 128)
    transfer_bytes: int = 256 * 2**20
    sustained_size: int = 8192
    prefill_n: tuple[int, ...] = (128, 512, 2048, 8192)
    prefill_b: tuple[int, ...] = (1, 8, 32)
    max_prefill_tokens: int = 64 * 1024
    decode_b: tuple[int, ...] = (1, 8, 32, 64)
    decode_t: tuple[int, ...] = (512, 2048)
    decode_new_tokens: int = 32
    ladder_n: int = 512
    lora_n: int = 256
    lora_b: int = 4
    residual_tokens: int = 16
    shape_budget_seconds: float = 20.0
    reduced: bool = False


CPU_GRID = Grid(copy_bytes=64 * 2**20, matmul_sizes=(512, 1024), attention_n=(128, 512),
                attention_heads=(8, 4, 64), prefill_n=(32, 128), prefill_b=(1, 4),
                max_prefill_tokens=4096, decode_b=(1, 4), decode_t=(32,), decode_new_tokens=8,
                ladder_n=64, lora_n=32, lora_b=2, shape_budget_seconds=5.0, reduced=True)


def grid_for(device: Any) -> Grid:
    return Grid() if is_cuda(device) else CPU_GRID


def host_memory() -> dict[str, int | None]:
    try:
        import psutil
    except ImportError:
        psutil = None
    if psutil is not None:
        vm = psutil.virtual_memory()
        return {"rss": int(psutil.Process().memory_info().rss), "used": int(vm.total - vm.available),
                "total": int(vm.total)}
    out: dict[str, int | None] = {"rss": None, "used": None, "total": None}
    try:
        pages = int(Path("/proc/self/statm").read_text().split()[1])
        out["rss"] = pages * os.sysconf("SC_PAGE_SIZE")
        text = Path("/proc/meminfo").read_text()
        total = re.search(r"^MemTotal:\s*(\d+)", text, re.M)
        avail = re.search(r"^MemAvailable:\s*(\d+)", text, re.M)
        if total and avail:
            out["total"] = int(total.group(1)) * 1024
            out["used"] = (int(total.group(1)) - int(avail.group(1))) * 1024
    except (OSError, ValueError, IndexError):
        pass
    return out


class Clock:
    def __init__(self, device: Any) -> None:
        self.device = device
        self.cuda = is_cuda(device)

    def sync(self) -> None:
        if self.cuda:
            import torch
            torch.cuda.synchronize(self.device)

    def timed(self, step: Step) -> float:
        if not self.cuda:
            t = time.perf_counter()
            step()
            return time.perf_counter() - t
        import torch
        self.sync()
        start = torch.cuda.Event(enable_timing=True)
        end = torch.cuda.Event(enable_timing=True)
        start.record()
        step()
        end.record()
        end.synchronize()
        return start.elapsed_time(end) / 1000.0

    def wall(self, step: Step) -> float:
        self.sync()
        t = time.perf_counter()
        step()
        self.sync()
        return time.perf_counter() - t

    def reset_peak(self) -> None:
        if self.cuda:
            import torch
            torch.cuda.reset_peak_memory_stats(self.device)

    def peak(self) -> int:
        if self.cuda:
            import torch
            return int(torch.cuda.max_memory_allocated(self.device))
        return int(host_memory().get("rss") or 0)

    def peak_reserved(self) -> int | None:
        if self.cuda:
            import torch
            return int(torch.cuda.max_memory_reserved(self.device))
        return None

    def free(self) -> int | None:
        if self.cuda:
            import torch
            return int(torch.cuda.mem_get_info(self.device)[0])
        mem = host_memory()
        if mem["total"] is None or mem["used"] is None:
            return None
        return mem["total"] - mem["used"]

    def clear(self) -> None:
        gc.collect()
        if self.cuda:
            import torch
            torch.cuda.empty_cache()


def is_oom(e: BaseException) -> bool:
    import torch
    return isinstance(e, torch.cuda.OutOfMemoryError) or "out of memory" in str(e).lower()


def measure(timer: Callable[[Step], float], step: Step, repeats: int, *, at_least: int = 3,
            warm_seconds: float = 0.5, budget: float | None = None) -> tuple[list[float], list[float]]:
    warm: list[float] = []
    end = time.perf_counter() + warm_seconds
    while len(warm) < at_least or time.perf_counter() < end:
        warm.append(timer(step))
    runs: list[float] = []
    spent = 0.0
    for _ in range(max(1, repeats)):
        runs.append(timer(step))
        spent += runs[-1]
        if budget is not None and spent >= budget and len(runs) >= MIN_REPEATS:
            break
    return runs, warm


@dataclass
class Rows:
    stamp: dict[str, Any] = field(default_factory=dict)
    items: list[dict[str, Any]] = field(default_factory=list)
    skipped: list[dict[str, Any]] = field(default_factory=list)
    ambient: dict[str, Any] | None = None

    def add(self, primitive: str, shape: dict[str, Any], runs: list[float], warm: float, *,
            probe: str, dtype: str, peak: int, model: str = "", bytes_moved: int | None = None,
            **extra: Any) -> dict[str, Any]:
        from .calibrate import record

        row = record(primitive, shape, runs, warm, peak_memory=peak, bytes_moved=bytes_moved,
                     **extra)
        if not model:
            row["id"] = f"{row['id']}:{dtype}"
        row.update({"model": model, "dtype": dtype, "probe": probe, **self.stamp})
        if self.ambient is not None:
            row["ambient"] = self.ambient
        self.items.append(row)
        return row

    def skip(self, probe: str, shape: dict[str, Any], reason: str, **extra: Any) -> None:
        self.skipped.append({"probe": probe, "shape": dict(shape), "reason": reason, **extra})


def gib(n: int) -> str:
    return f"{n // 2**20}MiB"


def torch_dtype(name: str) -> Any:
    import torch
    return {"bfloat16": torch.bfloat16, "float16": torch.float16, "float32": torch.float32,
            "tf32": torch.float32, "float8_e4m3fn": getattr(torch, "float8_e4m3fn", None)}[name]


@contextlib.contextmanager
def tf32(enabled: bool) -> Iterator[None]:
    import torch
    held = torch.backends.cuda.matmul.allow_tf32
    torch.backends.cuda.matmul.allow_tf32 = enabled
    try:
        yield
    finally:
        torch.backends.cuda.matmul.allow_tf32 = held


def bandwidth(clock: Clock, grid: Grid, repeats: int, rows: Rows, stated: float | None) -> float:
    import torch
    n = grid.copy_bytes
    t = {"src": torch.empty(n, dtype=torch.uint8, device=clock.device)}
    t["dst"] = torch.empty_like(t["src"])
    clock.reset_peak()
    runs, warm = measure(clock.timed, lambda: t["dst"].copy_(t["src"]), repeats)
    row = rows.add("memcopy", {"variant": f"{gib(n)}-copy"}, runs, warm[0], probe="bandwidth",
                   dtype="uint8", peak=clock.peak(), bytes_moved=2 * n,
                   stated_peak_bytes_per_second=stated)
    if stated and row.get("bytes_per_second"):
        row["fraction_of_stated_peak"] = row["bytes_per_second"] / stated
    t.clear()
    clock.clear()
    return float(row.get("bytes_per_second") or 0.0)


def matmul_step(clock: Clock, size: int, dtype: str) -> tuple[Step, Callable[[], Any]]:
    import torch
    if dtype == "float8_e4m3fn":
        fp8 = torch_dtype(dtype)
        a = torch.randn(size, size, device=clock.device).to(fp8)
        b = torch.randn(size, size, device=clock.device).to(fp8).t()
        one = torch.ones((), device=clock.device)

        def step() -> Any:
            return torch._scaled_mm(a, b, scale_a=one, scale_b=one, out_dtype=torch.bfloat16)
        return step, contextlib.nullcontext
    a = torch.randn(size, size, device=clock.device, dtype=torch_dtype(dtype))
    b = torch.randn(size, size, device=clock.device, dtype=torch_dtype(dtype))
    mode = (lambda: tf32(dtype == "tf32")) if clock.cuda and dtype in ("tf32", "float32") \
        else contextlib.nullcontext
    return (lambda: a @ b), mode


def matmul_refusal(clock: Clock, dtype: str, capability: tuple[int, int] | None) -> str | None:
    import torch
    if dtype == "tf32" and not clock.cuda:
        return "TF32 is a CUDA tensor-core mode"
    if dtype == "float8_e4m3fn":
        if not clock.cuda:
            return "fp8 matmul runs on CUDA only"
        if getattr(torch, "float8_e4m3fn", None) is None or not hasattr(torch, "_scaled_mm"):
            return f"torch {torch.__version__} has no float8_e4m3fn scaled matmul"
        if capability is not None and capability < (8, 9):
            return f"fp8 tensor cores need compute capability 8.9 or later, and this is {capability}"
    return None


def matmul(clock: Clock, grid: Grid, repeats: int, rows: Rows,
           capability: tuple[int, int] | None, done: set[tuple[int, str]]) -> None:
    for size in grid.matmul_sizes:
        for dtype in grid.matmul_dtypes:
            shape = {"variant": f"{size}x{size}x{size}"}
            if (size, dtype) in done:
                continue
            refused = matmul_refusal(clock, dtype, capability)
            if refused:
                rows.skip("matmul", {**shape, "dtype": dtype}, refused)
                continue
            try:
                step, mode = matmul_step(clock, size, dtype)
                with mode():
                    step()
                    clock.reset_peak()
                    runs, warm = measure(clock.timed, step, repeats)
            except Exception as e:  # noqa: BLE001
                rows.skip("matmul", {**shape, "dtype": dtype}, f"{type(e).__name__}: {e}"[:300])
                clock.clear()
                continue
            flops = 2 * size**3
            rows.add("matmul", shape, runs, warm[0], probe="matmul", dtype=dtype,
                     peak=clock.peak(), flops_per_second=flops / microbench.summary(runs)["seconds"])
            step = None
            clock.clear()


def launch(clock: Clock, repeats: int, rows: Rows) -> None:
    import torch
    x = torch.zeros(1, device=clock.device)

    def chain() -> None:
        for _ in range(LAUNCH_CHAIN):
            x.add_(1.0)

    def enqueue() -> None:
        for _ in range(LAUNCH_ENQUEUE):
            x.add_(1.0)

    count = max(repeats, 20)
    for variant, step, ops in (("launch-roundtrip", lambda: x.add_(1.0), 1),
                               (f"launch-chain{LAUNCH_CHAIN}", chain, LAUNCH_CHAIN),
                               (f"launch-enqueue{LAUNCH_ENQUEUE}", enqueue, LAUNCH_ENQUEUE)):
        runs, warm = measure(clock.wall, step, count)
        rows.add("numeric", {"variant": variant}, runs, warm[0], probe="launch", dtype="float32",
                 peak=clock.peak(), ops=ops,
                 per_op_seconds=microbench.summary(runs)["seconds"] / ops)


def transfer(clock: Clock, grid: Grid, repeats: int, rows: Rows) -> None:
    import torch
    n = grid.transfer_bytes
    if not clock.cuda:
        rows.skip("transfer", {"variant": gib(n)}, "host and device are one memory on CPU")
        return
    t = {"pageable": torch.empty(n, dtype=torch.uint8),
         "pinned": torch.empty(n, dtype=torch.uint8).pin_memory(),
         "dev": torch.empty(n, dtype=torch.uint8, device=clock.device)}
    steps = (("h2d-pageable", lambda: t["dev"].copy_(t["pageable"])),
             ("h2d-pinned", lambda: t["dev"].copy_(t["pinned"], non_blocking=True)),
             ("d2h-pageable", lambda: t["pageable"].copy_(t["dev"])),
             ("d2h-pinned", lambda: t["pinned"].copy_(t["dev"], non_blocking=True)))
    for variant, step in steps:
        runs, warm = measure(clock.wall, step, repeats)
        rows.add("memcopy", {"variant": f"{variant}-{gib(n)}"}, runs, warm[0], probe="transfer",
                 dtype="uint8", peak=clock.peak(), bytes_moved=n)
    t.clear()
    clock.clear()


def eager_attention(q: Any, k: Any, v: Any) -> Any:
    import torch
    groups = q.shape[1] // k.shape[1]
    k = k.repeat_interleave(groups, dim=1)
    v = v.repeat_interleave(groups, dim=1)
    n = q.shape[-2]
    scores = (q @ k.transpose(-1, -2)) * (q.shape[-1] ** -0.5)
    mask = torch.ones(n, n, dtype=torch.bool, device=q.device).triu(1)
    scores = scores.masked_fill(mask, float("-inf"))
    return torch.softmax(scores.float(), dim=-1).to(q.dtype) @ v


def sdpa(backend: Any, q: Any, k: Any, v: Any) -> Any:
    from torch.nn import functional
    from torch.nn.attention import sdpa_kernel

    with warnings.catch_warnings(), sdpa_kernel([backend]):
        warnings.simplefilter("ignore")
        return functional.scaled_dot_product_attention(q, k, v, is_causal=True, enable_gqa=True)


def attention(clock: Clock, grid: Grid, repeats: int, rows: Rows) -> None:
    import torch
    from torch.nn.attention import SDPBackend

    heads, kv_heads, dim = grid.attention_heads
    for n in grid.attention_n:
        shape = {"n": n, "b": 1}
        try:
            draw = torch.Generator(device=clock.device).manual_seed(0)
            q = torch.randn(1, heads, n, dim, device=clock.device, dtype=torch.bfloat16,
                            generator=draw)
            k = torch.randn(1, kv_heads, n, dim, device=clock.device, dtype=torch.bfloat16,
                            generator=draw)
            v = torch.randn(1, kv_heads, n, dim, device=clock.device, dtype=torch.bfloat16,
                            generator=draw)
            clock.reset_peak()
            reference = eager_attention(q, k, v).float()
            runs, warm = measure(clock.timed, lambda q=q, k=k, v=v: eager_attention(q, k, v),
                                 repeats,
                                 budget=grid.shape_budget_seconds)
            eager = rows.add("forward", {**shape, "variant": "attention-eager"}, runs, warm[0],
                             probe="attention", dtype="bfloat16", peak=clock.peak(), heads=heads,
                             kv_heads=kv_heads, head_dim=dim)
        except Exception as e:  # noqa: BLE001
            rows.skip("attention", {**shape, "variant": "attention-eager"},
                      f"{type(e).__name__}: {e}"[:300])
            clock.clear()
            continue
        for name, member in SDPA_BACKENDS:
            backend = getattr(SDPBackend, member, None)
            variant = f"attention-sdpa-{name}"
            if backend is None:
                rows.skip("attention", {**shape, "variant": variant},
                          f"torch {torch.__version__} has no {member} backend")
                continue
            try:
                clock.reset_peak()
                out = sdpa(backend, q, k, v).float()
                runs, warm = measure(clock.timed,
                                     lambda b=backend, q=q, k=k, v=v: sdpa(b, q, k, v), repeats,
                                     budget=grid.shape_budget_seconds)
            except Exception as e:  # noqa: BLE001
                rows.skip("attention", {**shape, "variant": variant},
                          f"{type(e).__name__}: {e}"[:300])
                clock.clear()
                continue
            seconds = microbench.summary(runs)["seconds"]
            rows.add("forward", {**shape, "variant": variant}, runs, warm[0], probe="attention",
                     dtype="bfloat16", peak=clock.peak(), heads=heads, kv_heads=kv_heads,
                     head_dim=dim, max_abs_diff_vs_eager=float((out - reference).abs().max()),
                     ratio_to_eager=seconds / eager["seconds"] if eager["seconds"] else None)
            out = None
        q = k = v = reference = None
        clock.clear()


def first_calls(clock: Clock, rows: Rows) -> None:
    import torch

    t = time.perf_counter()
    torch.zeros(1, device=clock.device)
    clock.sync()
    context = time.perf_counter() - t
    second = clock.wall(lambda: torch.zeros(1, device=clock.device))
    rows.add("numeric", {"variant": "first-call-context"}, [second], context, probe="first_call",
             dtype="float32", peak=clock.peak(), first_seconds=context, second_seconds=second)
    a = torch.randn(1024, 1024, device=clock.device, dtype=torch.bfloat16)
    first = clock.wall(lambda: torch.matmul(a, a))
    second = clock.wall(lambda: torch.matmul(a, a))
    rows.add("matmul", {"variant": "first-call-1024"}, [second], first, probe="first_call",
             dtype="bfloat16", peak=clock.peak(), first_seconds=first, second_seconds=second)
    if clock.cuda and torch.backends.cudnn.is_available():
        image = torch.randn(1, 8, 32, 32, device=clock.device)
        weight = torch.randn(8, 8, 3, 3, device=clock.device)
        first = clock.wall(lambda: torch.nn.functional.conv2d(image, weight))
        second = clock.wall(lambda: torch.nn.functional.conv2d(image, weight))
        rows.add("numeric", {"variant": "first-call-cudnn"}, [second], first, probe="first_call",
                 dtype="float32", peak=clock.peak(), first_seconds=first, second_seconds=second)
    else:
        rows.skip("first_call", {"variant": "first-call-cudnn"}, "cuDNN is CUDA only")
