"""The run noun: a protocol launched, and the job that executes it."""

from __future__ import annotations

import time
from typing import Any

from .core import (
    ACK,
    FULL,
    LIMIT,
    OFFSET,
    OWNER,
    SEARCH,
    YES,
    Arg,
    Ctx,
    Noun,
    Verb,
    VerbError,
    delete,
    history,
    listing,
    page,
    view,
)

# --- run ----------------------------------------------------------------------


def job_of(ctx: Ctx, run: str) -> str:
    """A run's job id: its own when given one (`j_…`), else its newest."""
    if run.startswith("j_"):
        return run
    job = ctx.get(f"/runs/{run}", view="summary").get("jobId")
    if not job:
        raise VerbError(f"run {run} has no job")
    return str(job)


def run_launch(ctx: Ctx, a: dict) -> Any:
    return ctx.bench().launch(
        a["protocol"],
        params=a.get("params"),
        inputs=a.get("inputs"),
        keep=a.get("keep"),
        budget=a.get("budget"),
        label=a.get("label"),
    )


def run_update(ctx: Ctx, a: dict) -> Any:
    label = None if a.get("clear") else a.get("label")
    if label is None and not a.get("clear"):
        raise VerbError("update needs a label, or clear")
    return ctx.bench().label_run(a["id"], label)


TERMINAL = ("done", "done_with_missing", "failed", "cancelled", "interrupted")


def run_watch(ctx: Ctx, a: dict) -> Any:
    """Wait for a run to reach a terminal state, up to `timeout` seconds
    (default 120), and answer its summary as it then stands."""
    deadline = time.monotonic() + float(a.get("timeout") or 120)
    interval = float(a.get("interval") or 4)
    while True:
        row = ctx.get(f"/runs/{a['id']}", view="summary")
        if row.get("jobStatus") in TERMINAL or time.monotonic() >= deadline:
            return row
        time.sleep(interval)


def finished(ctx: Ctx, run: str) -> dict:
    """A run's summary, when it has finished and so has results to read;
    otherwise it says what state it is in, rather than a missing object."""
    row = ctx.get(f"/runs/{run}", view="summary")
    if row.get("jobStatus") not in ("done", "done_with_missing"):
        raise VerbError(f"run {run} has no result: it is {row.get('jobStatus')}")
    return row


def run_result(ctx: Ctx, a: dict) -> Any:
    return ctx.bench().result(finished(ctx, a["id"]), a["node"])


def run_cancel(ctx: Ctx, a: dict) -> Any:
    return ctx.bench().cancel(job_of(ctx, a["id"]), reason=a.get("reason") or "")


RUN_ID = Arg("id", "The run's id, or its job's (j_…).", required=True, positional=True)

RUN = Noun(
    "run",
    "A run of a protocol, and the job that executes it.",
    (
        Verb(
            "run",
            "list",
            "Runs newest first, by label, status, protocol or project.",
            "GET /runs",
            (
                Arg("label", "Exactly this label."),
                Arg("label_contains", "A label containing this."),
                Arg("status", "Its job's status: queued, running, done, failed, …"),
                Arg("protocol", "One protocol's runs (prt_…)."),
                Arg("project", "owner/project."),
                OWNER,
                SEARCH,
                LIMIT,
                OFFSET,
                FULL,
            ),
            lambda ctx, a: listing(
                ctx,
                "/runs",
                {
                    "label": a.get("label"),
                    "labelContains": a.get("label_contains"),
                    "status": a.get("status"),
                    "protocol": a.get("protocol"),
                    "project": a.get("project"),
                    "owner": a.get("owner"),
                    **page(a),
                    "view": view(a),
                },
            ),
            "list",
            (
                "jobId",
                "jobStatus",
                "protocolName",
                "protocolVersion",
                "spentUsd",
                "label",
            ),
        ),
        Verb(
            "run",
            "read",
            "Its status, progress, error and missing nodes; full has the job.",
            "GET /runs/:id",
            (RUN_ID, FULL),
            lambda ctx, a: ctx.get(f"/runs/{a['id']}", view=view(a)),
            "read",
        ),
        Verb(
            "run",
            "launch",
            "Run a protocol: bind its params and inputs by name.",
            "POST /protocols/:id/runs",
            (
                Arg("protocol", "The protocol's id.", required=True, positional=True),
                Arg(
                    "params",
                    "A param, NAME=VALUE (JSON values parse).",
                    type="pairs",
                    flag="--param",
                ),
                Arg("inputs", "An input, NAME=PATH.", type="pairs", flag="--input"),
                Arg("keep", "all, or outputs alone.", choices=("all", "outputs")),
                Arg("budget", "Spend cap, USD.", type="float"),
                Arg("label", "What the run is for, one line."),
            ),
            run_launch,
        ),
        Verb(
            "run",
            "update",
            "Relabel it, or clear its label; kept in its history.",
            "PATCH /runs/:id",
            (
                RUN_ID,
                Arg("label", "The new label."),
                Arg("clear", "Remove the label.", type="bool"),
            ),
            run_update,
        ),
        Verb(
            "run",
            "watch",
            "Wait until it finishes (or timeout seconds), then its summary.",
            "GET /runs/:id",
            (RUN_ID, Arg("timeout", "Seconds to wait (default 120).", type="float")),
            run_watch,
            "read",
        ),
        Verb(
            "run",
            "result",
            "One node's output, the envelope stripped.",
            "GET /objects/:path",
            (RUN_ID, Arg("node", "The node's id.", required=True, positional=True)),
            run_result,
            "read",
        ),
        Verb(
            "run",
            "cancel",
            "Withdraw it while nobody is running it.",
            "POST /jobs/:id/cancel",
            (RUN_ID, Arg("reason", "Why, for the audit log.")),
            run_cancel,
        ),
        Verb(
            "run",
            "rerun",
            "Run the same graph and bindings again, as a new run.",
            "POST /jobs/:id/rerun",
            (RUN_ID,),
            lambda ctx, a: ctx.api("POST", f"/jobs/{job_of(ctx, a['id'])}/rerun")[0],
        ),
        Verb(
            "run",
            "delete",
            "Delete it and its job (its results stay): a dry run unless yes.",
            "DELETE /jobs/:id",
            (RUN_ID, YES, ACK),
            lambda ctx, a: delete(ctx, job_of(ctx, a["id"]), a),
        ),
        Verb(
            "run",
            "history",
            "Its job's audit log: created, relabelled, cancelled, deleted.",
            "GET /history/:kind/:id",
            (RUN_ID,),
            lambda ctx, a: history(ctx, "job", job_of(ctx, a["id"])),
            "read",
        ),
    ),
    absent={"create": "launch is its create: a run is a protocol launched"},
)
