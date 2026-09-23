"""In-process smoke test for the MCP tools (task 000185 acceptance).

Exercises the server's tool functions directly — no stdio
subprocess, no MCP client — so CI / local dev can verify the
surface without spawning a Claude session. Assumes mechbench-api is
running on MECHBENCH_API_URL with a seeded `benji` user; a fresh
API key must be available via MECHBENCH_API_KEY.

The layer-ablation run is gated behind --full because loading Gemma
4 and running 42 forward passes takes 1-2 minutes; the default run
asserts only `run list` and `run result` (the noun tools, task
000661), which are fast and enough to verify the wiring.
"""

from __future__ import annotations

import sys

from .api_client import ApiClient
from .config import Config
from .mcp_server import build_tools


def main(full: bool = False) -> int:
    config = Config.from_env()
    if not config.api_key:
        print(
            "error: MECHBENCH_API_KEY is required for smoke test.",
            file=sys.stderr,
        )
        return 2

    # The tools are plain functions (000298): no server, no MCP
    # client, no private access — the smoke calls what the server
    # registers.
    tools = build_tools(config)

    # --- run list: sanity check that the runner can reach the API.
    runs = tools["run"]("list", {"limit": 20})["items"]
    print(f"✓ run list returned {len(runs)} run(s)")

    # --- run read: the newest finished run's summary, if there is one.
    done = [r for r in runs if r.get("jobStatus") in ("done", "done_with_missing")]
    if done:
        row = tools["run"]("read", {"id": done[0]["jobId"]})
        print(f"✓ run read {done[0]['jobId']} → {row.get('jobStatus')} "
              f"{row.get('resultPath')}")
    else:
        with ApiClient(config) as api:
            api.call("GET", "/auth/me")
        print("✓ run read skipped (no finished runs); api /auth/me reachable")

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
