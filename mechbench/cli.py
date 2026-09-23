"""CLI entry.

`mechbench {login,logout,whoami,doctor,models,mcp,run,runs,label,protocol,
             delete,history,status,…}`
"""

from __future__ import annotations

import argparse
import sys

from mechbench_runner.config import Config

#: The three service commands, mapped to what each does. The old
#: `*-agent` spellings died with the `mechbench` rename (task 000307):
#: a brand-new command name owes nothing to the old one's muscle memory.
SERVICE_COMMANDS = {
    "install-service": "install",
    "uninstall-service": "uninstall",
    "service-status": "status",
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="mechbench")
    # metavar, or argparse prints every subcommand name in the usage
    # line's {...} blob — including the ones whose help is SUPPRESSed,
    # which defeats hiding them at all (task 000305).
    sub = parser.add_subparsers(dest="cmd", required=True, metavar="<command>")

    login_p = sub.add_parser(
        "login",
        help="Connect this machine to a mechbench account.",
    )
    login_p.add_argument(
        "--token",
        help="A registration token (mbr_...) from the website. Omit to be "
             "shown where to get one.",
    )
    login_p.add_argument(
        "--name",
        help="What to call this machine (default: its hostname).",
    )
    sub.add_parser("logout", help="Revoke this machine's key and forget it.")
    sub.add_parser(
        "install-service",
        help="Start the runner automatically, and keep it running.",
    )
    sub.add_parser("uninstall-service", help="Stop starting it automatically.")
    sub.add_parser(
        "service-status",
        help="Is the service installed, loaded and running?",
    )
    # The old names, kept working and kept out of --help (task 000305).
    #
    # These said "agent" because launchd calls a per-user background job a
    # LaunchAgent. Systemd has no such word — the same command writes a
    # user *unit* there — and in this platform "agent" already means the
    # model-driven kind: agent keys, agent.object_write, the
    # agent-callable MCP surface. One word, two unrelated jobs, and the
    # louder job was not the one the command meant.
    #
    # Aliases rather than a clean break because 0.4.0 is published and
    # these are in muscle memory, README, and the download page. They can
    # go once those have turned over.
    sub.add_parser("whoami", help="Which machine, which account, which scope.")
    sub.add_parser(
        "update", help="Upgrade this machine now, and restart the service."
    )
    sub.add_parser(
        "doctor",
        help="Check whether this machine can actually run jobs.",
    )
    models_p = sub.add_parser(
        "models", help="What weights are cached, and what pruning would return."
    )
    models_p.add_argument(
        "--prune",
        action="store_true",
        help="Delete every revision no ref points at.",
    )
    models_p.add_argument(
        "--delete",
        metavar="COMMIT",
        nargs="+",
        help="Delete specific revisions, by full commit hash.",
    )

    budget_p = sub.add_parser(
        "budget",
        help="Bound the model cache so an untended runner cannot fill "
             "the disk.",
    )
    budget_p.add_argument(
        "--keep-free", metavar="GB", type=float, dest="keep_free",
        help="Evict least-recently-used models to keep this much disk free.",
    )
    budget_p.add_argument(
        "--max", metavar="GB", type=float, dest="max_cache",
        help="Cap the cache's total size.",
    )
    budget_p.add_argument(
        "--recent-days", metavar="DAYS", type=float, dest="recent_days",
        help="Never evict anything used this recently, even over budget "
             "(default 3).",
    )
    budget_p.add_argument(
        "--off", action="store_true",
        help="Remove the budget entirely.",
    )
    budget_p.add_argument(
        "--sweep", action="store_true",
        help="Evict now instead of waiting for the runner's next claim.",
    )

    sub.add_parser(
        "mcp",
        help="Run the MCP server over stdio (the agent-callable surface).",
    )
    sub.add_parser(
        "supervise",
        help="Run the runner as a supervised child, restarting and "
             "upgrading it as needed.",
    )
    run_p = sub.add_parser(
        "run",
        help="Run a protocol (with a PROTOCOL arg), or the job-runner "
             "polling loop (with none).",
    )
    run_p.add_argument(
        "protocol",
        nargs="?",
        help="A protocol ref or id to run. With none, this is the runner "
             "loop (what the supervisor invokes).",
    )
    run_p.add_argument(
        "--param",
        action="append",
        metavar="NAME=VALUE",
        help="Bind one of the protocol's params: the model, an n. A VALUE "
             "that parses as JSON is that value (12, true, {…}); any "
             "other is text.",
    )
    run_p.add_argument(
        "--input",
        action="append",
        metavar="NAME=PATH",
        help="Bind one of the protocol's inputs to a stored object, by "
             "its path.",
    )
    run_p.add_argument(
        "--keep",
        choices=("all", "outputs"),
        help="What the run stores: everything (default), or its declared "
             "outputs alone, intermediates held on the runner.",
    )
    run_p.add_argument(
        "--label",
        metavar="TEXT",
        help="What the run is for, one line: 'P0, reasoning on'. Found "
             "again with `mechbench runs --label`.",
    )
    run_p.add_argument(
        "--bind",
        action="append",
        metavar="NAME=VALUE",
        help=argparse.SUPPRESS,
    )
    run_p.add_argument("--budget", type=float, metavar="USD",
                       help="Spend cap for the run; required for endpoint models.")
    run_p.add_argument("--wait", action="store_true",
                       help="After queuing, watch to a terminal state and "
                            "exit non-zero on failure.")
    run_p.add_argument(
        "--no-log-file",
        action="store_true",
        help="Runner loop only: print to the terminal without the rotating log.",
    )

    watch_p = sub.add_parser(
        "watch", help="Watch jobs to a terminal state, printing on change.")
    watch_p.add_argument("jobs", nargs="+", help="Job ids to watch.")

    cancel_p = sub.add_parser(
        "cancel",
        help="Withdraw jobs nobody is running: queued, preparing, or "
             "interrupted.")
    cancel_p.add_argument("jobs", nargs="+", help="Job ids to cancel.")
    cancel_p.add_argument(
        "--reason", default="",
        help="Why, recorded on the job and in the audit log.")

    protocol_p = sub.add_parser(
        "protocol",
        help="Push a protocol file, export one, publish a version, or copy "
             "one into a project.")
    protocol_sub = protocol_p.add_subparsers(
        dest="protocol_cmd", required=True, metavar="<verb>")
    publish_p = protocol_sub.add_parser(
        "publish",
        help="Make a version readable by anyone — the head unless --version.")
    publish_p.add_argument("protocol", help="A protocol id (prt_…).")
    publish_p.add_argument("--version", type=int, help="The version to publish.")
    unpublish_p = protocol_sub.add_parser(
        "unpublish", help="Withdraw a published version; names the articles citing it.")
    unpublish_p.add_argument("protocol", help="A protocol id (prt_…).")
    unpublish_p.add_argument("--version", type=int, required=True)
    push_p = protocol_sub.add_parser(
        "push",
        help="Push a protocol file into a project: created, versioned, "
             "described, or unchanged when it matches the head.")
    push_p.add_argument("file", help="A protocol file (JSON), as `export` writes it.")
    push_p.add_argument("--into", required=True, metavar="OWNER/PROJECT")
    push_p.add_argument("--org", action="store_true", help="OWNER is an org.")
    export_p = protocol_sub.add_parser(
        "export",
        help="Write a protocol version as its canonical file; a push of it "
             "changes nothing.")
    export_p.add_argument("protocol", help="A protocol id (prt_…).")
    export_p.add_argument("--version", type=int,
                          help="The version (default: the head).")
    export_p.add_argument("-o", dest="out", metavar="FILE",
                          help="Write to FILE instead of stdout.")
    copy_p = protocol_sub.add_parser(
        "copy", help="Copy a version into a project, sub-protocols and all.")
    copy_p.add_argument("source", help="<protocol-id>@<version>")
    copy_p.add_argument("--into", required=True, metavar="OWNER/PROJECT")
    copy_p.add_argument("--name", help="The copy's name (default: the source's).")
    copy_p.add_argument("--org", action="store_true", help="OWNER is an org.")
    copy_p.add_argument("--dry-run", action="store_true", dest="dry_run",
                        help="Say what it would create, and create nothing.")

    runs_p = sub.add_parser(
        "runs",
        help="List runs newest first, by label, protocol or project: job, "
             "status, versions, spend, label.")
    runs_p.add_argument("--label", metavar="TEXT", help="Exactly this label.")
    runs_p.add_argument("--label-contains", metavar="TEXT", dest="label_contains",
                        help="A label containing TEXT.")
    runs_p.add_argument("--protocol", metavar="ID", help="One protocol's runs (prt_…).")
    runs_p.add_argument("--project", metavar="OWNER/PROJECT",
                        help="The runs of a project's protocols.")
    runs_p.add_argument("--owner", metavar="HANDLE",
                        help="An org's runs (default: your own).")
    runs_p.add_argument("--limit", type=int, help="At most this many (default 100).")
    runs_p.add_argument("--json", action="store_true", dest="as_json",
                        help="Print the rows as JSON.")

    label_p = sub.add_parser(
        "label", help="Relabel a run, or clear its label; the change is kept "
                      "in its history.")
    label_p.add_argument("run", help="A run id, or its job's id (j_…).")
    label_g = label_p.add_mutually_exclusive_group(required=True)
    label_g.add_argument("text", nargs="?", help="The new label.")
    label_g.add_argument("--clear", action="store_true", help="Remove the label.")

    delete_p = sub.add_parser(
        "delete",
        help="Say what deleting an object path, or a protocol, job, article, "
             "dataset or project id, would do; --yes does it.")
    delete_p.add_argument(
        "target", help="An object path, or a prt_/j_/art_/ds_/proj_ id.")
    delete_p.add_argument("--prefix", action="store_true",
                          help="An object path: everything under it too.")
    delete_p.add_argument(
        "--yes", action="store_true", help="Delete, not just describe.")
    delete_p.add_argument("--acknowledge-citations", action="store_true",
                          dest="acknowledge",
                          help="Delete even though articles cite it.")

    history_p = sub.add_parser(
        "history", help="A thing's audit log — readable after it is deleted.")
    history_p.add_argument(
        "kind", choices=["object", "protocol", "article", "project", "dataset", "job"])
    history_p.add_argument("id")

    result_p = sub.add_parser(
        "result", help="Read one result node: <job>/<node>, or <node> with "
                       "--protocol/--bind. Envelope stripped.")
    result_p.add_argument("spec", help="<job>/<node>, or just <node> with --protocol.")
    result_p.add_argument(
        "--protocol", metavar="REF",
        help="Find the job by what it RAN instead of a job id: pair with "
             "--bind (task 000449, kills the job-id sidecars).")
    result_p.add_argument(
        "--bind", action="append", metavar="NAME=VALUE",
        help="A binding to match when --protocol is given.")
    fmt_g = result_p.add_mutually_exclusive_group()
    fmt_g.add_argument("--json", dest="fmt", action="store_const", const="json",
                       help="Force JSON output.")
    fmt_g.add_argument("--table", dest="fmt", action="store_const", const="table",
                       help="Force table output (metric tables only).")
    result_p.add_argument("-o", dest="out", metavar="FILE",
                          help="Write the JSON payload to FILE.")
    result_p.set_defaults(fmt="auto")
    status = sub.add_parser(
        "status",
        help="Ask the running runner what it is doing.",
    )
    status.add_argument(
        "--json",
        action="store_true",
        help="Emit the raw control-socket reply.",
    )
    status.add_argument(
        "--watch",
        action="store_true",
        help="Stream events as they happen instead of printing once.",
    )
    sub.add_parser("pause", help="Stop claiming new jobs; finish the current one.")
    sub.add_parser("resume", help="Start claiming jobs again.")

    restart_p = sub.add_parser(
        "restart",
        help="Restart the runner through the service manager (picks up a "
             "code change). Refuses to interrupt a running job.")
    restart_p.add_argument(
        "--force", action="store_true",
        help="Restart even while a job is running (it will be interrupted "
             "and re-claimed).")

    smoke = sub.add_parser(
        "smoke",
        help="Run the in-process smoke test (skips model load by default).",
    )
    smoke.add_argument(
        "--full",
        action="store_true",
        help="Include the 42-forward-pass layer-ablation run (~1-2 min).",
    )

    args = parser.parse_args(argv)
    config = Config.from_env()

    if args.cmd == "supervise":
        from mechbench_runner.supervisor import main as supervise_main

        return supervise_main()

    if args.cmd == "update":
        from mechbench_runner import updater

        return updater.update_now()

    if args.cmd == "doctor":
        from mechbench_runner import doctor

        return doctor.run(config)

    if args.cmd == "models":
        from mechbench_runner import models_cmd

        return models_cmd.run(prune=args.prune, delete=args.delete)

    if args.cmd == "budget":
        from mechbench_runner import budget_cmd

        return budget_cmd.run(
            keep_free=args.keep_free, max_cache=args.max_cache,
            recent_days=args.recent_days, off=args.off, do_sweep=args.sweep,
        )

    if args.cmd in SERVICE_COMMANDS:
        from mechbench_runner import service as service_mod

        cmd = SERVICE_COMMANDS[args.cmd]
        try:
            if cmd == "install":
                st = service_mod.install()
                print(f"Installed {st.path}")
                print(f"  {st.detail}")
                hint = service_mod.linger_hint()
                if hint:
                    print(f"\n{hint}")
                return 0 if st.loaded else 1
            if cmd == "uninstall":
                st = service_mod.uninstall()
                print(st.detail)
                return 0
            st = service_mod.status()
            print(f"service  {st.detail}")
            print(f"unit     {st.path}")
            return 0 if st.running or not st.installed else 1
        except service_mod.UnsupportedPlatformError as exc:
            print(str(exc), file=sys.stderr)
            return 1

    if args.cmd == "restart":
        import os
        import signal as signal_mod
        import time

        from mechbench_runner import service as service_mod
        from mechbench_runner.control import ControlError, request

        # Who is serving, and are they busy? Both matter: the pid is the
        # identity a restart has to change, and a job in flight is work
        # that must be handed back to the server rather than dropped.
        before: int | None = None
        busy_job: str | None = None
        try:
            snap = request("status")
            pid = snap.get("pid")
            before = int(pid) if isinstance(pid, int) else None
            if snap.get("orphaned"):
                print(f"note: pid {before} is an ORPHAN — its supervisor is "
                      f"gone, so the service manager cannot reach it "
                      f"(task 000462).")
            if snap.get("job") is not None or snap.get("phase") in (
                    "executing", "loading-model", "downloading-model"):
                busy_job = (snap.get("job") or {}).get("id")
        except ControlError:
            pass  # nothing answering — nothing to interrupt

        if busy_job and not args.force:
            print(f"a job is running ({busy_job}), and SIGTERM means "
                  f"'finish it first' — so a restart now would wait, not "
                  f"restart.\n  `restart --force` interrupts it on the server "
                  f"(its claim, progress and result path survive, and it can "
                  f"resume), or wait for it to finish.", file=sys.stderr)
            return 1

        if busy_job:
            # --force abandons the job NOW, and that has to reach the
            # SERVER first: an interrupted job keeps its claim, progress
            # and resultPath and is re-claimable (epic 000320), where a
            # job whose runner merely vanished waits on the watchdog.
            #
            # This runs in a DIFFERENT process from the one holding the
            # claim's token, which is why `/interrupt` is authorized by
            # the claim's identity (task 000511). Before that it could
            # not land, and the job stayed `running` — uncancellable,
            # re-adopted on every start.
            print(f"interrupting {busy_job} so it can be resumed…")
            try:
                from mechbench_runner.api_client import ApiClient

                with ApiClient(config) as api:
                    api.interrupt_job(busy_job, "mechbench restart --force")
                print(f"  interrupted. `mechbench cancel {busy_job}` ends it "
                      f"for good; otherwise it resumes when the runner is back.")
            except Exception as exc:  # noqa: BLE001 — advisory, never fatal
                print(f"  could not interrupt it on the server: {exc}\n"
                      f"  the job stays claimed by this machine and will be "
                      f"resumed on the next start.", file=sys.stderr)

        try:
            st = service_mod.restart()
            # SIGTERM is a request the runner is entitled to finish a job
            # under, and an orphan is not the service manager's to stop at
            # all. A FORCED restart therefore escalates, by pid, once.
            if args.force and before and service_mod.serving_pid() == before:
                print(f"  pid {before} did not yield; stopping it.")
                try:
                    os.kill(before, signal_mod.SIGKILL)
                except (ProcessLookupError, PermissionError) as exc:
                    print(f"  could not stop pid {before}: {exc}",
                          file=sys.stderr)
                time.sleep(2.0)
                st = service_mod.restart()
        except service_mod.UnsupportedPlatformError as exc:
            print(str(exc), file=sys.stderr)
            return 1

        print(f"restart  {st.detail}")
        if st.running:
            # The service manager reports "running" the moment the process
            # exists, but the control socket is not answering until the
            # runner has finished importing and bound it — poll a short
            # window so the version line is there when it can be.
            for _ in range(20):
                try:
                    data = request("status")
                    compute = data.get("compute_version")
                    print(f"runner   up{f' (compute {compute})' if compute else ''}")
                    break
                except ControlError:
                    time.sleep(0.5)
        return 0 if st.running else 1

    if args.cmd in {"login", "logout", "whoami"}:
        from mechbench_runner import login as login_mod

        if args.cmd == "login":
            return login_mod.login(config, token=args.token, name=args.name)
        if args.cmd == "logout":
            return login_mod.logout(config)
        return login_mod.whoami(config)

    if args.cmd == "mcp":
        from mechbench_runner.mcp_server import run_stdio

        run_stdio(config)
        return 0

    if args.cmd == "cancel":
        from mechbench_runner import bench_cmd

        return bench_cmd.cancel(config, args.jobs, args.reason)

    if args.cmd == "protocol":
        from mechbench_runner import bench_cmd

        if args.protocol_cmd == "publish":
            return bench_cmd.protocol_publish(config, args.protocol, args.version)
        if args.protocol_cmd == "unpublish":
            return bench_cmd.protocol_unpublish(config, args.protocol, args.version)
        if args.protocol_cmd == "push":
            return bench_cmd.protocol_push(config, args.file, args.into, args.org)
        if args.protocol_cmd == "export":
            return bench_cmd.protocol_export(config, args.protocol, args.version,
                                             args.out)
        return bench_cmd.protocol_copy(config, args.source, args.into, args.name,
                                       args.org, args.dry_run)

    if args.cmd == "runs":
        from mechbench_runner import bench_cmd

        return bench_cmd.runs(config, label=args.label,
                              label_contains=args.label_contains,
                              protocol=args.protocol, project=args.project,
                              owner=args.owner, limit=args.limit, as_json=args.as_json)

    if args.cmd == "label":
        from mechbench_runner import bench_cmd

        return bench_cmd.label_run(config, args.run, None if args.clear else args.text)

    if args.cmd == "delete":
        from mechbench_runner import bench_cmd

        return bench_cmd.delete(config, args.target, args.prefix, args.yes,
                                args.acknowledge)

    if args.cmd == "history":
        from mechbench_runner import bench_cmd

        return bench_cmd.history(config, args.kind, args.id)

    if args.cmd in {"run", "watch", "result"}:
        # `run` overloads: with a PROTOCOL it launches (the researcher's
        # verb, task 000448); with none it is the runner loop (what the
        # supervisor invokes). `watch`/`result` are always the researcher.
        #
        # A launch flag with no protocol is a typo, not a request to start
        # the daemon in the foreground: `mechbench run --wait` used to do
        # exactly that, silently dropping the flag. Refuse it.
        if args.cmd == "run" and args.protocol is None and (
                args.bind or args.param or args.input or args.keep
                or args.budget is not None or args.wait or args.label):
            print("run: --param/--input/--keep/--budget/--label/--wait need a PROTOCOL "
                  "to launch; a bare `run` is the runner loop.", file=sys.stderr)
            return 2
        if args.cmd != "run" or args.protocol is not None:
            from mechbench_runner import bench_cmd

            if args.cmd == "run":
                return bench_cmd.run(config, args.protocol, args.bind,
                                     args.budget, args.wait,
                                     params=args.param, inputs=args.input,
                                     keep=args.keep, label=args.label)
            if args.cmd == "watch":
                return bench_cmd.watch(config, args.jobs)
            return bench_cmd.result(config, args.spec, args.fmt, args.out,
                                    args.protocol, args.bind)

    if args.cmd == "run":
        from mechbench_runner.exits import EXIT_CRASH
        from mechbench_runner.logs import excepthook_to_log
        from mechbench_runner.logs import install as install_logs

        # Bounded logs, because launchd has no rotation and this process
        # is meant to run for months unattended (task 000294).
        #
        # Installed *before* the update hook, so what an update did lands
        # in runner.log with everything else. It went to the boot log at
        # first — technically where boot-time output belongs, and
        # practically somewhere nobody would think to look, while being
        # the only record of why an upgrade failed.
        if not args.no_log_file:
            install_logs()
            excepthook_to_log()

        # An approved update replaces this code and can only do that
        # while the code is still unloaded — so before the runner itself
        # is imported.
        from mechbench_runner import updater

        updater.take_pending_step()

        from mechbench_runner.job_runner import JobRunner
        try:
            return JobRunner(config).run()
        except SystemExit:
            raise
        except BaseException:
            # A crash has to *be* a crash to the supervisor, and it has
            # to be legible in the file afterwards.
            import traceback

            traceback.print_exc()
            return EXIT_CRASH

    if args.cmd in {"status", "pause", "resume"}:
        from mechbench_runner.control import ControlError

        try:
            if args.cmd == "status" and getattr(args, "watch", False):
                return _watch()
            from mechbench_runner.control import request

            data = request(args.cmd)
        except ControlError as exc:
            print(f"{exc}", file=sys.stderr)
            return 1
        if args.cmd == "status" and getattr(args, "json", False):
            import json

            print(json.dumps(data, indent=2))
        else:
            print(_render(data))
        return 0

    if args.cmd == "smoke":
        from mechbench_runner._smoke import main as smoke_main

        return smoke_main(full=args.full)

    parser.error(f"unknown cmd: {args.cmd}")
    return 2


def _render(data: dict) -> str:
    """One screen of prose, not a table — this is read at a glance."""
    lines = []
    phase = data.get("phase", "unknown")
    job = data.get("job")
    if job:
        done, total = job.get("done", 0), job.get("total", 0)
        pct = f" {100 * done // total}%" if total else ""
        lines.append(
            f"{phase}: {job.get('protocol_kind')} ({job.get('id')}){pct}"
            f"  {job.get('elapsed_seconds', 0):.0f}s elapsed"
        )
        spent = job.get("spent_usd")
        if spent is not None:
            cap = job.get("cap_usd")
            against = f" of ${cap:.2f}" if cap else ""
            lines.append(f"spend    ${spent:.4f}{against}")
    else:
        lines.append(phase + (" (paused)" if data.get("paused")
                              and phase != "paused" else ""))
    lines.append(f"model    {data.get('model_id') or '(none loaded)'}")
    lines.append(f"api      {data.get('api_url')}")
    lines.append(
        f"jobs     {data.get('completed', 0)} completed, {data.get('failed', 0)} failed"
    )
    limits = data.get("limits") or {}
    for hold in limits.get("holds", []):
        # A rate-limit hold is the difference between "wedged" and
        # "waiting", and it is exactly what a person checks status for.
        lines.append(
            f"limited  {hold['provider']} held {hold['seconds']:.0f}s more"
        )
    tight = [b for b in limits.get("buckets", [])
             if b.get("capacity") and b["available"] < b["capacity"] / 4]
    for b in tight[:3]:
        lines.append(
            f"quota    {b['provider']} {b['currency']}: "
            f"{b['available']:.0f} of {b['capacity']:.0f} left"
        )
    up = data.get("uptime_seconds", 0)
    compute = data.get("compute_version")
    compute_note = f" (compute {compute})" if compute else ""
    # Whose answer this is (task 000462). An orphan answers `status` as
    # readily as the live runner, with its own stale version, and the
    # service manager cannot reach it — so say so on the line a reader
    # takes the version from.
    if data.get("orphaned"):
        whose = " ORPHAN — supervisor gone; `restart --force` to replace it"
    elif data.get("supervised"):
        whose = ""
    else:
        whose = " (unsupervised)"
    lines.append(
        f"runner   v{data.get('runner_version')}{compute_note} "
        f"pid {data.get('pid')}, up {up / 60:.0f}m{whose}"
    )
    return "\n".join(lines)


def _watch() -> int:
    """Follow the event stream. The runner pushes; this never polls."""
    import json
    import socket as _socket

    from mechbench_runner.control import PROTOCOL_VERSION, ControlError, socket_path

    path = socket_path()
    if not path.exists():
        print(f"no runner is listening at {path}", file=sys.stderr)
        return 1
    s = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
    try:
        s.connect(str(path))
    except OSError as exc:
        print(f"could not attach: {exc}", file=sys.stderr)
        return 1
    s.sendall((json.dumps({"v": PROTOCOL_VERSION, "op": "subscribe"}) + "\n").encode())
    print("attached; ^C to detach")
    buf = b""
    try:
        while True:
            chunk = s.recv(65536)
            if not chunk:
                print("runner closed the connection")
                return 1
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                if not line.strip():
                    continue
                msg = json.loads(line)
                if "event" in msg:
                    print(f"{msg['event']}  {json.dumps(msg.get('data') or {})}")
                elif msg.get("ok"):
                    print(_render(msg.get("data") or {}))
    except KeyboardInterrupt:
        return 0
    except ControlError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    finally:
        s.close()


if __name__ == "__main__":
    sys.exit(main())
