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
from .endings import ended_notes

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


def _params(pairs: list[str] | None) -> dict[str, Any]:
    """`--param n=12`: a value that parses as JSON is that value, any
    other is text — so `n=12` is a number and `label=draws` a string."""
    out: dict[str, Any] = {}
    for p in pairs or []:
        name, _, value = p.partition("=")
        if not name:
            raise SystemExit(f"--param wants name=value, got {p!r}")
        try:
            out[name] = json.loads(value)
        except json.JSONDecodeError:
            out[name] = value
    return out


def _inputs(pairs: list[str] | None) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for p in pairs or []:
        name, _, value = p.partition("=")
        if not name or not value:
            raise SystemExit(f"--input wants name=path, got {p!r}")
        out[name] = value
    return out


def run(config: Config, protocol: str, binds: list[str] | None,
        budget: float | None, wait: bool, *,
        params: list[str] | None = None, inputs: list[str] | None = None,
        keep: str | None = None, label: str | None = None) -> int:
    """Bind a protocol, queue its job, print the job id, record it. With
    `--wait`, then watch to a terminal state and exit on the result.

    `--param` and `--input` bind by the names the protocol declares, and
    `--label` says what the run is for, so `mechbench runs --label` finds
    it again. `--bind`, the legacy binding, is refused: the server no
    longer reads it."""
    if binds:
        print("run: --bind is the legacy binding, which is no longer read; "
              "bind a param with --param NAME=VALUE and an input with "
              "--input NAME=PATH", file=sys.stderr)
        return 2
    _connect(config)
    declared_params = _params(params)
    declared_inputs = _inputs(inputs)
    try:
        out = bench.launch(
            protocol, budget=budget,
            params=declared_params or None, inputs=declared_inputs or None,
            keep=keep, label=label)
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
               "params": declared_params,
               "inputs": declared_inputs, "budget_usd": budget,
               **({"keep": keep} if keep else {}),
               **({"label": label} if label else {}),
               "run": run_id, "job": job})
    print(job, flush=True)  # first line is the job id, for JOB=$(mechbench run …)
    detail = f"  run {run_id} · {protocol}" + (f" · {label}" if label else "")
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

    code = _print_result(payload, fmt, out_path)
    # Last, on stderr, where the eye lands after the output scrolls by.
    for note in ended_notes(payload, node):
        print(f"!! {note}", file=sys.stderr)
    return code


def _print_result(payload: Any, fmt: str, out_path: str | None) -> int:
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


# --- publishing, copying, deleting, histories (epic 000535, task 000542) ------


def protocol_publish(config: Config, protocol: str, version: int | None) -> int:
    """Publish a version — the head unless one is named — and print where
    the public reads it. An article embeds exactly this version."""
    _connect(config)
    try:
        n = (version if version is not None
             else int(bench.get_protocol(protocol)["version"]))
        out = bench.publish_protocol_version(protocol, n)
    except bench.BenchError as e:
        print(f"publish failed: {e}", file=sys.stderr)
        return 1
    print(f"{protocol} v{n} published")
    if out.get("publicPath"):
        # The site is the API's host without `api.` (mechbench.ai); any
        # other API (a local one) gets the path alone.
        site = config.api_base_url.replace("://api.", "://", 1)
        print(f"  {site if site != config.api_base_url else ''}{out['publicPath']}")
    for inc in out.get("unpublishedIncludes") or []:
        print(f"  note: it includes {inc.get('name')}, which is not published — "
              f"readers see only its name", file=sys.stderr)
    return 0


def protocol_unpublish(config: Config, protocol: str, version: int) -> int:
    _connect(config)
    try:
        out = bench.unpublish_protocol_version(protocol, version)
    except bench.BenchError as e:
        print(f"unpublish failed: {e}", file=sys.stderr)
        return 1
    print(f"{protocol} v{version} unpublished")
    _print_citations(out, "now show a placeholder where it was")
    return 0


def protocol_copy(config: Config, source: str, into: str, name: str | None,
                  org: bool, dry_run: bool) -> int:
    """`<protocol>@<version> --into owner/project`: a new protocol there,
    its sub-protocols copied (or reused) with it."""
    pid, sep, ver = source.partition("@")
    owner, slash, project = into.partition("/")
    if not sep or not ver.isdigit() or not slash or not owner or not project:
        print("copy wants <protocol-id>@<version> --into <owner>/<project>",
              file=sys.stderr)
        return 2
    _connect(config)
    try:
        out = bench.copy_protocol_version(
            pid, int(ver), owner, project, name=name,
            owner_kind="org" if org else "user", dry_run=dry_run)
    except bench.BenchError as e:
        print(f"copy failed: {e}", file=sys.stderr)
        return 1
    made = out.get("name") if dry_run else (out.get("protocol") or {}).get("name")
    pid_new = None if dry_run else (out.get("protocol") or {}).get("id")
    print(f"{'would create' if dry_run else 'created'} {owner}/{project}/{made}"
          + (f" ({pid_new})" if pid_new else ""))
    for step in out.get("copied") or []:
        print(f"  {'would copy' if dry_run else 'copied'} {step['from']['name']} "
              f"v{step['from']['version']} as {step['to']['name']}")
    for step in out.get("reused") or []:
        print(f"  {'would reuse' if dry_run else 'reused'} {step['to']['name']} "
              f"(already a copy of {step['from']['name']} v{step['from']['version']})")
    return 0


def _print_citations(out: dict[str, Any], consequence: str) -> None:
    cited = out.get("citedBy") or []
    unreadable = int(out.get("unreadable") or 0)
    if not cited and not unreadable:
        return
    print(f"  {len(cited) + unreadable} article(s) cite it and {consequence}:")
    for a in cited:
        where = f"{a.get('ownerHandle')}/articles/{a.get('slug')}"
        print(f"    {a.get('title')} — {where} ({a.get('status')})")
    if unreadable:
        print(f"    …and {unreadable} you cannot see")


def _counts(counts: dict[str, Any]) -> str:
    return ", ".join(f"{n} {k}" for k, n in counts.items() if n)


def delete(config: Config, target: str, prefix: bool, yes: bool,
           acknowledge: bool) -> int:
    """Say what deleting `target` would do; with `--yes`, do it. A path is an
    object (everything under it with `--prefix`); a `prt_`, `j_`, `art_`,
    `ds_` or `proj_` id is that thing. Exit 0 when it is (or was) deleted,
    or could be now; 1 when something refuses it."""
    _connect(config)
    try:
        plan = bench.delete(target, prefix=prefix, dry_run=True)
    except (bench.BenchError, ValueError) as e:
        print(f"delete failed: {e}", file=sys.stderr)
        return 1
    refusal = plan.get("refusal")
    if refusal:
        print(f"refused ({refusal.get('code')}): {refusal.get('error')}")
        for p in (refusal.get("includedBy") or refusal.get("includes") or []):
            print(f"  {p.get('ownerHandle')}/{p.get('name')} ({p.get('id')})")
        for j in refusal.get("jobs") or []:
            print(f"  {j}")
        return 1
    what = _counts(plan.get("deletes") or {}) or "nothing"
    print(f"{'deleting' if yes else 'would delete'}: {what}")
    if _counts(plan.get("keeps") or {}):
        print(f"  keeps: {_counts(plan['keeps'])}")
    _print_citations(plan, "will show a placeholder where it was")
    cited = bool(plan.get("citedBy")) or bool(plan.get("unreadable"))
    if not yes:
        print("  (nothing deleted — repeat with --yes"
              + (" --acknowledge-citations" if cited else "") + ")", file=sys.stderr)
        return 0
    if cited and not acknowledge:
        print("  not deleted: articles cite it — repeat with "
              "--acknowledge-citations", file=sys.stderr)
        return 1
    try:
        bench.delete(target, prefix=prefix, acknowledge_citations=acknowledge)
    except bench.BenchError as e:
        print(f"delete failed: {e}", file=sys.stderr)
        return 1
    print(f"deleted {target} — its history: mechbench history <kind> <id>")
    return 0


def history(config: Config, kind: str, entity_id: str) -> int:
    """A lifetime's audit log, readable after the thing is gone."""
    _connect(config)
    try:
        out = bench.history(kind, entity_id)
    except bench.BenchError as e:
        print(f"history failed: {e}", file=sys.stderr)
        return 1
    life = out.get("lifetime") or {}
    ended = (f", deleted {life.get('deletedAt')} by @{life.get('deletedBy')}"
             if life.get("deletedAt") else "")
    print(f"{life.get('kind')} {life.get('id')} · {life.get('label')}")
    print(f"  created {life.get('createdAt') or '(before this was recorded)'}{ended}")
    for e in out.get("events") or []:
        meta = json.dumps(e.get("meta") or {}, separators=(",", ":"))
        actor = f"@{e['actorHandle']}" if e.get("actorHandle") else "(deleted user)"
        print(f"  {e.get('at')}  {actor}  {e.get('action')}  {meta[:160]}")
    for o in out.get("others") or []:
        state = "live now" if o.get("live") else f"deleted {o.get('deletedAt')}"
        print(f"  also at this address: {o.get('id')} ({state})")
    return 0


# --- protocols as files, runs by label (epic 000654) -------------------------


def _findings(body: Any) -> None:
    found = body.get("findings") if isinstance(body, dict) else None
    for f in found or []:
        where = f" [{f['node']}]" if f.get("node") else ""
        print(f"  {f.get('severity', '')} {f.get('code', '')}{where}: "
              f"{f.get('message', '')}", file=sys.stderr)


def protocol_push(config: Config, file: str, into: str, org: bool) -> int:
    """Push a protocol file into `owner/project`: created, a new version,
    a new description, or unchanged, as the server finds it by name. A
    legacy-form or miswired file is refused with its findings, and exits
    1 with nothing stored."""
    _connect(config)
    try:
        out = bench.push_protocol(file, into, owner_kind="org" if org else "user")
    except ValueError as e:
        print(f"push failed: {e}", file=sys.stderr)
        return 2
    except bench.BenchError as e:
        body = e.body if isinstance(e.body, dict) else {}
        print(f"push refused ({body.get('code') or e.status}): "
              f"{body.get('error') or e}", file=sys.stderr)
        _findings(body)
        return 1
    p = out.get("protocol") or {}
    print(f"{out.get('action')} {into}/{p.get('name')} {p.get('id')} "
          f"v{p.get('version')}")
    _findings(out)
    return 0


def protocol_export(config: Config, protocol: str, version: int | None,
                    out_path: str | None) -> int:
    """Write a protocol version (the head by default) as its canonical
    file: to `-o FILE`, or to stdout."""
    _connect(config)
    try:
        out = bench.export_protocol(protocol, version=version, path=out_path)
    except bench.BenchError as e:
        print(f"export failed: {e}", file=sys.stderr)
        return 1
    if out_path:
        print(f"wrote {out_path} ({out.get('name')} v{out.get('version')})",
              file=sys.stderr)
    else:
        sys.stdout.write(out["text"])
    return 0


def _money(v: Any) -> str:
    return "" if v is None else f"${v:.4g}"


def runs(config: Config, *, label: str | None, label_contains: str | None,
         protocol: str | None, project: str | None, owner: str | None,
         limit: int | None, as_json: bool) -> int:
    """Runs newest first, one line each: job, status, protocol and
    version, compute version, spend, label. `--json` prints the rows."""
    _connect(config)
    try:
        rows = bench.runs(label=label, label_contains=label_contains,
                          protocol=protocol, project=project, owner=owner,
                          limit=limit)
    except bench.BenchError as e:
        print(f"runs failed: {e}", file=sys.stderr)
        return 1
    if as_json:
        print(json.dumps(rows, indent=1))
        return 0
    if not rows:
        print("(no runs)", file=sys.stderr)
        return 0
    table = [
        (r.get("jobId") or r.get("id") or "",
         r.get("jobStatus") or r.get("status") or "",
         f"{r.get('protocolName') or r.get('protocolId')} v{r.get('protocolVersion')}",
         r.get("computeVersion") or "", _money(r.get("spentUsd")),
         (r.get("createdAt") or "")[:16], r.get("label") or "")
        for r in rows]
    widths = [max(len(str(row[i])) for row in table) for i in range(6)]
    for row in table:
        print("  ".join(str(c).ljust(widths[i]) for i, c in enumerate(row[:6]))
              + "  " + row[6])
    return 0


def label_run(config: Config, run: str, text: str | None) -> int:
    """Relabel a run (its id or its job's), or clear it: the change is
    kept in the job's history."""
    _connect(config)
    try:
        out = bench.label_run(run, text)
    except bench.BenchError as e:
        print(f"label failed: {e}", file=sys.stderr)
        return 1
    now = out.get("label")
    if not out.get("changed"):
        print(f"{run} already {'labelled ' + repr(now) if now else 'unlabelled'}")
    else:
        print(f"{run} {'labelled ' + repr(now) if now else 'unlabelled'}")
    return 0
