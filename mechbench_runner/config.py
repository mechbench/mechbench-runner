"""Config loading for mechbench-runner.

All runtime config comes from environment variables. Fails fast on
missing required values so both the MCP server (long-running) and
the job-runner (long-running) catch misconfiguration at startup
rather than on the first tool call.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Config:
    """Resolved runner config. Construct via `Config.from_env()`."""

    api_base_url: str
    api_key: str | None
    poll_interval_seconds: float
    warm_model_id: str | None

    @classmethod
    def from_env(cls) -> Config:
        return cls(
            api_base_url=os.environ.get(
                "MECHBENCH_API_URL", "http://localhost:3000"
            ).rstrip("/"),
            api_key=os.environ.get("MECHBENCH_API_KEY"),
            poll_interval_seconds=float(
                os.environ.get("MECHBENCH_POLL_INTERVAL_SECONDS", "2.0")
            ),
            # Which model to warm at startup so the first job does not pay
            # cold-start cost. Purely operational, and deliberately without a
            # built-in value: a runner that invents a model can execute a
            # protocol that never said which weights it wanted.
            warm_model_id=os.environ.get("MECHBENCH_WARM_MODEL_ID")
            or os.environ.get("MECHBENCH_DEFAULT_MODEL_ID"),
        )

    def require_api_key(self) -> str:
        if not self.api_key:
            raise RuntimeError(
                "MECHBENCH_API_KEY env var is required (mint one via "
                "mechbench-ui at /settings/api-keys or POST /auth/api-keys)."
            )
        return self.api_key
