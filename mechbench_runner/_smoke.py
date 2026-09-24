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

    tools = build_tools(config)

    runs = tools["run"]("list", {"limit": 20})["items"]
    print(f"✓ run list returned {len(runs)} run(s)")

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
