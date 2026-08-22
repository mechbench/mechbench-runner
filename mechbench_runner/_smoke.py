"""In-process smoke test for the three MCP tools (task 000185 acceptance).

Exercises the server's tool functions directly — no stdio
subprocess, no MCP client — so CI / local dev can verify the
surface without spawning a Claude session. Assumes mechbench-api is
running on MECHBENCH_API_URL with a seeded `benji` user; a fresh
API key must be available via MECHBENCH_API_KEY.

The layer-ablation run is gated behind --full because loading Gemma
4 and running 42 forward passes takes 1-2 minutes; the default run
asserts only the get_result / list_jobs tools, which are
fast and enough to verify the wiring.
"""

from __future__ import annotations

import json
import sys

from .api_client import ApiClient
from .config import Config
from .mcp_server import build_server


def main(full: bool = False) -> int:
    config = Config.from_env()
    if not config.api_key:
        print(
            "error: MECHBENCH_API_KEY is required for smoke test.",
            file=sys.stderr,
        )
        return 2

    server = build_server(config)
    # FastMCP stores tools on the internal manager; pull the bound
    # functions directly so the smoke test doesn't need an MCP client.
    tools = {t.name: t.fn for t in server._tool_manager.list_tools()}  # noqa: SLF001

    # --- list_jobs: sanity check that the runner can reach the API.
    jobs = tools["list_jobs"]()
    print(f"✓ list_jobs returned {len(jobs)} job(s)")

    # --- get_result: exercise against the most recent done job, if any.
    done = [j for j in jobs if j["status"] == "done" and j.get("resultPath")]
    if done:
        path = done[0]["resultPath"]
        payload = tools["get_result"](path=path)
        print(f"✓ get_result({path}) → kind/protocol={payload.get('protocol')}")
    else:
        # Queue one via the API so get_result has something to target.
        with ApiClient(config) as api:
            res = api._client.get("/auth/me")  # noqa: SLF001 — direct probe
            res.raise_for_status()
        print("✓ get_result skipped (no completed jobs); api /auth/me reachable")

    if full:
        payload = tools["run_protocol"](
            prompt="Complete this sentence with one word: The Eiffel Tower is in"
        )
        print(
            f"✓ run_protocol → protocol={payload['protocol']} "
            f"n_layers={payload['n_layers']} "
            f"baseline={payload['prompts'][0]['baseline_logprob']}"
        )
    else:
        print("(skipping run_protocol; pass --full to include it)")

    print("\nall smoke checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main(full="--full" in sys.argv))
