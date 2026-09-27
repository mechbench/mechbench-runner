from __future__ import annotations

import sys

from .api_client import ApiClient
from .config import Config
from .verbs import Ctx, invoke


def main() -> int:
    config = Config.from_env()
    if not config.api_key:
        print(
            "error: MECHBENCH_API_KEY is required for smoke test.",
            file=sys.stderr,
        )
        return 2

    ctx = Ctx(config)

    runs = invoke(ctx, "run", "list", {"limit": 20})["items"]
    print(f"✓ run list returned {len(runs)} run(s)")

    done = [r for r in runs if r.get("jobStatus") in ("done", "done_with_missing")]
    if done:
        row = invoke(ctx, "run", "read", {"id": done[0]["jobId"]})
        print(f"✓ run read {done[0]['jobId']} → {row.get('jobStatus')} "
              f"{row.get('resultPath')}")
    else:
        with ApiClient(config) as api:
            api.call("GET", "/auth/me")
        print("✓ run read skipped (no finished runs); api /auth/me reachable")

    print("\nall smoke checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
