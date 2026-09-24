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

FINISHED = ("done", "done_with_missing")

HISTORY = pathlib.Path.home() / ".mechbench" / "runs.jsonl"


def _connect(config: Config) -> None:
    bench.configure(api_url=config.api_base_url, api_key=config.require_api_key())


def _binds(pairs: list[str] | None) -> dict[str, Any]:
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
        pass


def _params(pairs: list[str] | None) -> dict[str, Any]:
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
    print(job, flush=True)
    detail = f"  run {run_id} · {protocol}" + (f" · {label}" if label else "")
    if budget is not None:
        detail += f" · cap ${budget}"
    print(detail, file=sys.stderr, flush=True)
    if wait:
        return watch(config, [job])
    return 0


def cancel(config: Config, jobs: list[str], reason: str = "") -> int:
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
    _connect(config)
    failed: list[str] = []
    for jid, j in bench.watch(jobs, interval=interval):
        if j.get("status") is None:
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
            for node, why in (j.get("missingNodes") or {}).items():
                reason = str((why or {}).get("reason", ""))[:200] if isinstance(
                    why, dict) else str(why)[:200]
                print(f"  ~~ {jid} node {node} did not run: {reason}", flush=True)
    return 1 if failed else 0


def _as_table(payload: Any) -> str | None:
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
    if protocol is not None:
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


def protocol_publish(config: Config, protocol: str, version: int | None) -> int:
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


def _findings(body: Any) -> None:
    found = body.get("findings") if isinstance(body, dict) else None
    for f in found or []:
        where = f" [{f['node']}]" if f.get("node") else ""
        print(f"  {f.get('severity', '')} {f.get('code', '')}{where}: "
              f"{f.get('message', '')}", file=sys.stderr)


def protocol_push(config: Config, file: str, into: str, org: bool) -> int:
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
