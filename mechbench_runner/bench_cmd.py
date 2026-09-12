"""The three bench verbs — `run`, `watch`, `result` (task 000448).

Every `mechbench` verb until now served the machine (`login`, `status`,
`pause`, the runner loop). None served the person using the bench, so
experiments 023 and 024 each rewrote the same three by hand — a
`launch.py` that records the job id, a `watch.py` that prints only on
change, and a reader that unwraps the API envelope. ~150 generic lines,
written twice. This is those three, once.

The standing test (epic 000447): the next experiment should be its
graphs, its rubric and its readings — nothing else. If it contains
launch/watch/result code, this is not done.
"""
from __future__ import annotations

import json
import pathlib
import sys
import time
from typing import Any

from .api_client import ApiClient, ApiError
from .config import Config

TERMINAL = ("done", "failed", "cancelled", "interrupted")

#: Every run's job id, appended the moment the API answers. A job id
#: held only in a terminal scrollback is a job id lost — and `run`
#: writes here before it does anything else, `--wait` included.
HISTORY = pathlib.Path.home() / ".mechbench" / "runs.jsonl"


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
    body: dict[str, Any] = {"bindings": _binds(binds)}
    if budget is not None:
        body["budgetUsd"] = budget
    try:
        with ApiClient(config) as api:
            out = api.create_run(protocol, body)
    except ApiError as e:
        print(f"run failed: {e}", file=sys.stderr)
        return 1
    run_obj = out.get("run") or {}
    run_id = run_obj.get("id") or out.get("runId")
    # The response shape is inconsistent (task 000451): jobId at the top,
    # or inside run, or a job object. Take the first that answers.
    job = (out.get("jobId") or run_obj.get("jobId")
           or (out.get("job") or {}).get("id"))
    if not job:
        print(f"no job id in response: {json.dumps(out)[:300]}", file=sys.stderr)
        return 1
    _remember({"at": time.strftime("%Y-%m-%dT%H:%M:%SZ"), "protocol": protocol,
               "bindings": body["bindings"], "budget_usd": budget,
               "run": run_id, "job": job})
    print(job, flush=True)  # first line is the job id, for JOB=$(mechbench run …)
    detail = f"  run {run_id} · {protocol}"
    if budget is not None:
        detail += f" · cap ${budget}"
    print(detail, file=sys.stderr, flush=True)
    if wait:
        return watch(config, [job])
    return 0


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
    """Poll jobs to a terminal state, printing only when something
    CHANGES — a long local run must not bury the interesting moment
    under a hundred identical lines — and printing failures loudly: a
    watcher that reports only success is indistinguishable from one that
    has stopped watching. Non-zero exit if any job did not finish
    `done`."""
    last: dict[str, str] = {}
    pending = list(jobs)
    failed: list[str] = []
    with ApiClient(config) as api:
        while pending:
            for job in list(pending):
                try:
                    j = api.get_job(job)
                except ApiError as e:
                    print(f"{time.strftime('%H:%M:%S')} {job[:14]} (fetch error: {e})",
                          flush=True)
                    continue
                j = j.get("job", j)
                line = _line(j)
                if line != last.get(job):
                    print(f"{time.strftime('%H:%M:%S')} {job[:14]} {line}", flush=True)
                    last[job] = line
                if j.get("status") in TERMINAL:
                    pending.remove(job)
                    if j.get("status") != "done":
                        failed.append(job)
                        err = str(j.get("errorMessage") or j.get("error") or "")[:500]
                        print(f"  !! {job} {str(j.get('status')).upper()}: {err}",
                              flush=True)
            if pending:
                time.sleep(interval)
    return 1 if failed else 0


def _decode(raw: bytes) -> Any:
    import mechbench_schema as ms

    return ms.load_raw(bytes(raw))


def _unwrap(obj: Any) -> Any:
    """Strip the Emitted envelope every reader stripped by hand — the
    `(lambda o: o.get('payload', o))(...)` idiom that appeared five
    times across two experiments is the API leaking its envelope."""
    if isinstance(obj, dict) and "payload" in obj and "provenance" in obj:
        return obj["payload"]
    return obj


def _as_table(payload: Any) -> str | None:
    """A metric table renders as a table; everything else does not."""
    if not (isinstance(payload, dict) and payload.get("kind") == "metric_table"):
        return None
    rows = payload.get("rows") or payload.get("records") or []
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


def _resolve_result_path(api: ApiClient, spec: str, protocol: str | None,
                         binds: list[str] | None) -> tuple[str, str] | int:
    """`(result_base, node)` for a `result` request — from a `<job>/<node>`
    spec, or from `--protocol <ref> --bind k=v <node>`, which finds the
    job by what it RAN (task 000449) so no job-id sidecar is needed."""
    if protocol is not None:
        node = spec
        wanted = {k: (v if isinstance(v, str) else json.dumps(v, sort_keys=True))
                  for k, v in _binds(binds).items()}
        runs = api.find_runs(protocol, {k: v for k, v in wanted.items()
                                        if isinstance(v, str)})
        # find_runs already filtered on the server; take the newest that
        # actually has a finished job with a result.
        done = [r for r in runs if r.get("resultPath")]
        if not done:
            print(f"no run of {protocol} with those bindings has a result yet",
                  file=sys.stderr)
            return 1
        return f"{done[0]['resultPath']}", node
    if "/" not in spec:
        print("result wants <job>/<node>, or <node> with --protocol", file=sys.stderr)
        return 2
    job_id, _, node = spec.partition("/")
    job = api.get_job(job_id)
    job = job.get("job", job)
    base = job.get("resultPath")
    if not base:
        print(f"job {job_id} has no result yet (status {job.get('status')})",
              file=sys.stderr)
        return 1
    return base, node


def result(config: Config, spec: str, fmt: str, out_path: str | None,
           protocol: str | None = None, binds: list[str] | None = None) -> int:
    """Read one node's output — `<job>/<node>`, or `--protocol <ref>
    --bind k=v <node>` to find it by what it ran. Prints a table for a
    metric table and JSON for anything else (no envelope), or writes the
    JSON to `-o file`."""
    try:
        with ApiClient(config) as api:
            resolved = _resolve_result_path(api, spec, protocol, binds)
            if isinstance(resolved, int):
                return resolved
            base, node = resolved
            raw = api.fetch_object(f"{base}/{node}")
    except ApiError as e:
        print(f"result failed: {e}", file=sys.stderr)
        return 1
    payload = _unwrap(_decode(raw))

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
