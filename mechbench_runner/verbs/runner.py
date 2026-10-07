from __future__ import annotations

import json
import pathlib
from typing import Any

from .core import LIMIT, OFFSET, SEARCH, Arg, Ctx, Noun, Verb, VerbError, paged


def runner_calibrate(ctx: Ctx, a: dict) -> Any:
    from .. import calibrate as cal

    into = a.get("into")
    if a.get("push") and (not into or into.count("/") != 1):
        raise VerbError("push names the project it goes under: into OWNER/PROJECT")
    backend = a.get("backend") or cal.default_backend()
    model = a.get("model") or (cal.default_model() if backend == "mlx" else None)
    minutes = a.get("sustained_minutes")
    collection = cal.calibrate(
        model, repeats=int(a.get("repeats") or 10), say=cal.stderr, backend=backend,
        device=a.get("device"), sustained_minutes=None if minutes is None else float(minutes),
        residuals=a.get("residuals"), against=a.get("against"))
    out: dict[str, Any] = {"collection": collection}
    if not model:
        out["note"] = ((f"no model named and {cal.DEFAULT_MODEL} is not in the Hugging Face "
                        "cache: only the micro-benchmarks ran; pass model") if backend == "mlx"
                       else "no model named: only the machine's probes ran; pass model")
    if a.get("out"):
        pathlib.Path(a["out"]).write_text(json.dumps(collection, indent=2) + "\n")
        out["written"] = a["out"]
    if a.get("push"):
        owner, project = str(into).split("/")
        path = cal.object_path(owner, project, collection)
        out["pushed"] = {"path": path, **(ctx.bench().emit(path, collection) or {})}
    return out


def runner_list(ctx: Ctx, a: dict) -> Any:
    rows = ctx.get("/runners")
    rows = [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []
    if not a.get("signed_out"):
        rows = [r for r in rows if not r.get("signedOut")]
    want = str(a.get("search") or "").lower()
    if want:
        rows = [r for r in rows
                if any(want in str(r.get(k) or "").lower() for k in ("id", "name", "hostname"))]
    return paged(rows, a)


RUNNER = Noun(
    "runner",
    "A machine that claims jobs and runs them: list names them (a run pins one by "
    "id), calibrate measures the one the command runs on.",
    (
        Verb(
            "runner",
            "list",
            "The runners you can reach, with the id a run pins with `--runner`: "
            "whether each is connected, what it is doing, its version.",
            "GET /runners",
            (
                Arg("signed_out", "Signed-out runners too.", type="bool"),
                SEARCH,
                LIMIT,
                OFFSET,
            ),
            runner_list,
            "list",
            ("id", "name", "hostname", "connected", "phase", "runnerVersion", "lastSeenAt"),
            effect="read",
        ),
        Verb(
            "runner",
            "calibrate",
            "Measure this machine: the copy and matmul micro-benchmarks, a model's load "
            "(cold and warm page cache), forward, capture overhead and one LoRA step, "
            "repeated, as medians with the warm-up apart; answers a calibration "
            "collection and keeps the micro-benchmarks as the canary's idle baseline. On the "
            "torch backend it also reads the GPU's identity, its bandwidth, matmul throughput "
            "by dtype, launch latency, host-device copies, attention kernels and sustained "
            "load, and the model's prefill and decode at batch, the cost of instrumentation, "
            "attribution, and its numerics run to run, by path and by precision.",
            "",
            (
                Arg("model", "The model to time (default: Gemma 4 E2B when it is cached)."),
                Arg("repeats", "Timed repeats of each measurement (default 10).",
                    type="int"),
                Arg("backend", "The backend to measure (default: the one this machine "
                    "offers first).", choices=("mlx", "torch")),
                Arg("device", "torch only: the device (default cuda when there is one, "
                    "else cpu)."),
                Arg("sustained_minutes", "torch on CUDA only: how long the sustained-load "
                    "probe runs (default 5; 0 skips it).", type="float"),
                Arg("residuals", "Write the model's residual stream, layer by layer, for one "
                    "fixed input to this JSON file, to diff across backends.", local=True),
                Arg("against", "torch only: a residuals file from another backend; the same "
                    "tokens run here and the collection carries the layer-by-layer difference.",
                    local=True),
                Arg("out", "Write the collection to this JSON file too.", local=True),
                Arg("push", "Store the collection as an object under into.", type="bool"),
                Arg("into", "The project it is pushed under: OWNER/PROJECT."),
            ),
            runner_calibrate,
            "act",
            effect="draft",
            local=True,
        ),
    ),
    absent={
        "read": "`runner list` has each runner whole; the machines page shows one",
        "create": "a runner is registered from its own machine by `mechbench login`",
        "update": "renamed and paused on the machines page (PATCH /runners/:id)",
        "delete": "signed out on the machines page, or by `mechbench logout` on the machine",
        "history": "a runner's jobs are its history, on the jobs page",
    },
)
