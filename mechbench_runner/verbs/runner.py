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
    model = a.get("model") or cal.default_model()
    collection = cal.calibrate(model, repeats=int(a.get("repeats") or 10), say=cal.stderr)
    out: dict[str, Any] = {"collection": collection}
    if not model:
        out["note"] = (f"no model named and {cal.DEFAULT_MODEL} is not in the Hugging Face "
                       "cache: only the micro-benchmarks ran; pass model")
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
            "collection and keeps the micro-benchmarks as the canary's idle baseline.",
            "",
            (
                Arg("model", "The model to time (default: Gemma 4 E2B when it is cached)."),
                Arg("repeats", "Timed repeats of each measurement (default 10).",
                    type="int"),
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
