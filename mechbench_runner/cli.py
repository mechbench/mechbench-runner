"""CLI entry.

`mechbench-runner {login,logout,whoami,doctor,models,mcp,run,status,…}`
"""

from __future__ import annotations

import argparse
import sys

from .config import Config


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="mechbench-runner")
    sub = parser.add_subparsers(dest="cmd", required=True)

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
        "install-agent",
        help="Start the runner automatically, and keep it running.",
    )
    sub.add_parser("uninstall-agent", help="Stop starting it automatically.")
    sub.add_parser(
        "agent-status",
        help="Is the service installed, loaded and running?",
    )
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

    sub.add_parser(
        "mcp",
        help="Run the MCP server over stdio (the agent-callable surface).",
    )
    run_p = sub.add_parser(
        "run",
        help="Run the job-runner polling loop against mechbench-api.",
    )
    run_p.add_argument(
        "--no-log-file",
        action="store_true",
        help="Print to the terminal only, without the rotating log.",
    )
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

    if args.cmd == "update":
        from . import updater

        return updater.update_now()

    if args.cmd == "doctor":
        from . import doctor

        return doctor.run(config)

    if args.cmd == "models":
        from . import models_cmd

        return models_cmd.run(prune=args.prune, delete=args.delete)

    if args.cmd in {"install-agent", "uninstall-agent", "agent-status"}:
        from . import agent as agent_mod

        try:
            if args.cmd == "install-agent":
                st = agent_mod.install()
                print(f"Installed {st.path}")
                print(f"  {st.detail}")
                hint = agent_mod.linger_hint()
                if hint:
                    print(f"\n{hint}")
                return 0 if st.loaded else 1
            if args.cmd == "uninstall-agent":
                st = agent_mod.uninstall()
                print(st.detail)
                return 0
            st = agent_mod.status()
            print(f"service  {st.detail}")
            print(f"unit     {st.path}")
            return 0 if st.running or not st.installed else 1
        except agent_mod.UnsupportedPlatformError as exc:
            print(str(exc), file=sys.stderr)
            return 1

    if args.cmd in {"login", "logout", "whoami"}:
        from . import login as login_mod

        if args.cmd == "login":
            return login_mod.login(config, token=args.token, name=args.name)
        if args.cmd == "logout":
            return login_mod.logout(config)
        return login_mod.whoami(config)

    if args.cmd == "mcp":
        from .mcp_server import run_stdio

        run_stdio(config)
        return 0

    if args.cmd == "run":
        from .exits import EXIT_CRASH
        from .logs import excepthook_to_log
        from .logs import install as install_logs

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
        from . import updater

        updater.take_pending_step()

        from .job_runner import JobRunner
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
        from .control import ControlError

        try:
            if args.cmd == "status" and getattr(args, "watch", False):
                return _watch()
            from .control import request

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
        from ._smoke import main as smoke_main

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
    else:
        lines.append(phase + (" (paused)" if data.get("paused") and phase != "paused" else ""))
    lines.append(f"model    {data.get('model_id') or '(none loaded)'}")
    lines.append(f"api      {data.get('api_url')}")
    lines.append(
        f"jobs     {data.get('completed', 0)} completed, {data.get('failed', 0)} failed"
    )
    up = data.get("uptime_seconds", 0)
    lines.append(
        f"runner   v{data.get('runner_version')} pid {data.get('pid')}, up {up / 60:.0f}m"
    )
    return "\n".join(lines)


def _watch() -> int:
    """Follow the event stream. The runner pushes; this never polls."""
    import json
    import socket as _socket

    from .control import PROTOCOL_VERSION, ControlError, socket_path

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
