"""CLI entry: `mechbench-runner {mcp,run,smoke}`."""

from __future__ import annotations

import argparse
import sys

from .config import Config


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="mechbench-runner")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser(
        "mcp",
        help="Run the MCP server over stdio (the agent-callable surface).",
    )
    sub.add_parser(
        "run",
        help="Run the job-runner polling loop against mechbench-api.",
    )
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

    if args.cmd == "mcp":
        from .mcp_server import run_stdio

        run_stdio(config)
        return 0

    if args.cmd == "run":
        from .job_runner import JobRunner

        JobRunner(config).run()
        return 0

    if args.cmd == "smoke":
        from ._smoke import main as smoke_main

        return smoke_main(full=args.full)

    parser.error(f"unknown cmd: {args.cmd}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
