"""The three bench verbs — `run`, `watch`, `result` (task 000448).

Every `mechbench` verb until now served the machine (`login`, `status`,
`pause`, the runner loop). None served the person using the bench, so
experiments 023 and 024 each rewrote the same three by hand — a
`launch.py` that records the job id, a `watch.py` that prints only on
change, and a reader that unwraps the API envelope.

The launch/watch/find/read plumbing itself now lives ONCE, in
`mechbench_compute.bench` (task 000450) — the same library an experiment
script imports. These verbs are thin wrappers over it: they parse the
command line, render (the job-id line, the change-only progress, the
metric table), and keep the local run history. The transport, the
envelope-stripping and the find-run-by-binding are the library's, not a
second copy here.
"""
from __future__ import annotations

import json
import pathlib
import sys
import time
from typing import Any

from mechbench_compute import bench

from .config import Config

TERMINAL = ("done", "done_with_missing", "failed", "cancelled", "interrupted")

#: The terminal statuses that mean the work landed. `done_with_missing`
#: (000515) is a result worth reading — part of the graph did not run,
#: which `watch` says out loud rather than treating as a failure.
FINISHED = ("done", "done_with_missing")

#: Every run's job id, appended the moment the API answers. A job id
#: held only in a terminal scrollback is a job id lost — and `run`
#: writes here before it does anything else, `--wait` included.
HISTORY = pathlib.Path.home() / ".mechbench" / "runs.jsonl"


def _connect(config: Config) -> None:
    """Point the bench library at this machine's credentials — the host
    embedding hook (mechbench_compute.bench.configure). The runner
    resolved its key from `~/.mechbench/config.toml` or the environment;
    the library would find the same file on its own, but handing it the
    already-resolved pair keeps one source of truth."""
    bench.configure(api_url=config.api_base_url, api_key=config.require_api_key())


def _binds(pairs: list[str] | None) -> dict[str, Any]:
    """`name=value` pairs. A value that starts with `{` or `[` is JSON —
    a model-ref binding is an object, not a string, which 024's launcher
    learned the hard way."""
    out: dict[str, Any] = {}
    for p in pairs or []:
        if "=" not in p:
            raise SystemExit(f"--bind wants name=value, got {p!r}")
        name, _, value = p.partition("=")
        if not name:
            raise SystemExit(f"--bind wants name=value, got {p!r}")
        if value[:1] in ("{", "["):
            try:
                out[name] = json.loads(value)
            except json.JSONDecodeError as e:
                raise SystemExit(f"--bind {name}: not valid JSON: {e}") from e
        else:
            out[name] = value
    return out


def _remember(entry: dict[str, Any]) -> None:
    try:
        HISTORY.parent.mkdir(parents=True, exist_ok=True)
        with HISTORY.open("a") as f:
            f.write(json.dumps(entry) + "\n")
    except OSError:
        pass  # a logging failure must never lose the run — the id still prints


def run(config: Config, protocol: str, binds: list[str] | None,
        budget: float | None, wait: bool) -> int:
    """Bind a protocol, queue its job, print the job id, record it. With
    `--wait`, then watch to a terminal state and exit on the result."""
    _connect(config)
    bindings = _binds(binds)
    try:
        out = bench.launch(protocol, bindings, budget=budget)
    except bench.BenchError as e:
        print(f"run failed: {e}", file=sys.stderr)
        return 1
    # One shape (task 000451): the bare run, with `jobId` on it.
    run_id = out.get("id")
    job = out.get("jobId")
    if not job:
        print(f"no job id in response: {json.dumps(out)[:300]}", file=sys.stderr)
        return 1
    _remember({"at": time.strftime("%Y-%m-%dT%H:%M:%SZ"), "protocol": protocol,
               "bindings": bindings, "budget_usd": budget,
               "run": run_id, "job": job})
    print(job, flush=True)  # first line is the job id, for JOB=$(mechbench run …)
    detail = f"  run {run_id} · {protocol}"
    if budget is not None:
        detail += f" · cap ${budget}"
    print(detail, file=sys.stderr, flush=True)
    if wait:
        return watch(config, [job])
    return 0


def cancel(config: Config, jobs: list[str], reason: str = "") -> int:
    """Withdraw work nobody is running (tasks 000463, 000511). Takes
    several ids, because draining a queue is the reason this exists;
    reports each one and exits non-zero if any could not be cancelled.

    A job a runner is executing is refused: interrupt it first
    (`mechbench restart --force` on that machine), then cancel it."""
    _connect(config)
    failed = 0
    for job in jobs:
        try:
            out = bench.cancel(job, reason=reason)
        except bench.BenchError as e:
            print(f"{job}: {e}", file=sys.stderr)
            failed += 1
            continue
        if out.get("alreadyCancelled"):
            print(f"{job} was already cancelled")
        else:
            print(f"{job} cancelled (was {out.get('from')})")
    return 1 if failed else 0


def _line(j: dict[str, Any]) -> str:
    num, den = j.get("progressNum"), j.get("progressDen")
    node = (j.get("progressNode") or {}).get("id")
    spent = j.get("spentUsd")
    unit = j.get("progressUnit") or ""
    prog = f" {num}/{den} {unit}".rstrip() if num is not None else ""
    return (f"{j.get('status')}{prog}"
            + (f" [{node}]" if node else "")
            + (f" ${spent}" if spent else ""))


def watch(config: Config, jobs: list[str], interval: float = 4.0) -> int:
    """Poll jobs to a terminal state, printing each change the library
    hands up — it yields only when something CHANGES, so a long local run
    does not bury the interesting moment under identical lines — and
    printing failures loudly: a watcher that reports only success is
    indistinguishable from one that has stopped watching. Non-zero exit if
    any job did not finish — `done_with_missing` counts as finished
    (000515), and each absent node is named."""
    _connect(config)
    failed: list[str] = []
    for jid, j in bench.watch(jobs, interval=interval):
        if j.get("status") is None:  # a transient fetch error, retried next round
            print(f"{time.strftime('%H:%M:%S')} {jid[:14]} "
                  f"(fetch error: {j.get('error')})", flush=True)
            continue
        print(f"{time.strftime('%H:%M:%S')} {jid[:14]} {_line(j)}", flush=True)
        status = j.get("status")
        if status in TERMINAL and status not in FINISHED:
            failed.append(jid)
            err = str(j.get("errorMessage") or j.get("error") or "")[:500]
            print(f"  !! {jid} {str(status).upper()}: {err}", flush=True)
        elif status == "done_with_missing":
            # Not a failure, and not silent either: the result is real
            # and something in it is absent (000515).
            for node, why in (j.get("missingNodes") or {}).items():
                reason = str((why or {}).get("reason", ""))[:200] if isinstance(
                    why, dict) else str(why)[:200]
                print(f"  ~~ {jid} node {node} did not run: {reason}", flush=True)
    return 1 if failed else 0


def _as_table(payload: Any) -> str | None:
    """A metric table renders as a table; everything else does not."""
    if not (isinstance(payload, dict)
            and payload.get("kind") in ("records/table", "metric_table")):
        return None
    rows = payload.get("rows") or []
    if not rows:
        return "(empty table)"
    cols: list[str] = []
    for r in rows:
        for k in r:
            if k not in cols:
                cols.append(k)
    widths = {c: max(len(c), *(len(_cell(r.get(c))) for r in rows)) for c in cols}
    head = "  ".join(c.ljust(widths[c]) for c in cols)
    body = "\n".join("  ".join(_cell(r.get(c)).ljust(widths[c]) for c in cols)
                     for r in rows)
    return f"{head}\n{body}"


def _cell(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, float):
        return f"{v:.4g}"
    if isinstance(v, (dict, list)):
        return json.dumps(v, default=str)
    return str(v)


def _resolve(spec: str, protocol: str | None,
             binds: list[str] | None) -> tuple[str | dict, str] | int:
    """`(source, node)` for a `result` request, where `source` is a job id
    or a run row carrying its result path. From a `<job>/<node>` spec, or
    from `--protocol <ref> --bind k=v <node>`, which finds the run by what
    it RAN (task 000449) so no job-id sidecar is needed."""
    if protocol is not None:
        # bench.results_for filters on the server, newest first; take the
        # newest that actually has a finished job with a result.
        runs = bench.results_for(protocol, **_binds(binds))
        done = [r for r in runs if r.get("resultPath")]
        if not done:
            print(f"no run of {protocol} with those bindings has a result yet",
                  file=sys.stderr)
            return 1
        return done[0], spec
    if "/" not in spec:
        print("result wants <job>/<node>, or <node> with --protocol", file=sys.stderr)
        return 2
    job_id, _, node = spec.partition("/")
    return job_id, node


def result(config: Config, spec: str, fmt: str, out_path: str | None,
           protocol: str | None = None, binds: list[str] | None = None) -> int:
    """Read one node's output — `<job>/<node>`, or `--protocol <ref>
    --bind k=v <node>` to find it by what it ran. Prints a table for a
    metric table and JSON for anything else (the library returns the
    payload, no envelope), or writes the JSON to `-o file`."""
    _connect(config)
    resolved = _resolve(spec, protocol, binds)
    if isinstance(resolved, int):
        return resolved
    source, node = resolved
    try:
        payload = bench.result(source, node)
    except bench.BenchError as e:
        print(f"result failed: {e}", file=sys.stderr)
        return 1

    if out_path:
        pathlib.Path(out_path).write_text(json.dumps(payload, indent=1, default=str))
        print(f"wrote {out_path}", file=sys.stderr)
        return 0
    if fmt == "json":
        print(json.dumps(payload, indent=1, default=str))
        return 0
    table = _as_table(payload)
    if fmt == "table" and table is None:
        print("(not a metric table)", file=sys.stderr)
        return 1
    print(table if table is not None else json.dumps(payload, indent=1, default=str))
    return 0
