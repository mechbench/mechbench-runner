"""Config loading for mechbench.

Two sources, in a deliberate order: the environment, then the
credentials `mechbench login` stored. Both the MCP server and
the job-runner are long-lived, so this fails fast on missing required
values at startup rather than on the first tool call.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from . import credentials
from .watchdog import DEFAULT_STALL_SECONDS

# Prod, because almost everyone installing this is connecting a machine
# to mechbench.ai. Developing against a local API is the rarer case and
# the one whose owner can be expected to set an env var, so it is the
# opt-in: MECHBENCH_API_URL=http://localhost:3000.
#
# It was the other way round until 2026-08-23, and the first real install
# duly registered against a stale localhost server and got a 404.
DEFAULT_API_URL = "https://api.mechbench.ai"


@dataclass(frozen=True)
class Config:
    """Resolved runner config. Construct via `Config.from_env()`."""

    api_base_url: str
    api_key: str | None
    poll_interval_seconds: float
    warm_model_id: str | None
    #: How long without progress means wedged rather than slow (000294).
    #: Zero disables the watchdog, which is what a debugger wants.
    watchdog_seconds: float = DEFAULT_STALL_SECONDS
    #: Which machine this credential belongs to, when it came from a
    #: registration. None for a hand-pasted key, which is anonymous by
    #: construction — it has no runner record behind it.
    runner_id: str | None = None
    runner_name: str | None = None
    #: True when the key came from `login` rather than the environment.
    from_stored_credentials: bool = False

    @classmethod
    def from_env(cls) -> Config:
        poll = float(os.environ.get("MECHBENCH_POLL_INTERVAL_SECONDS", "2.0"))
        watchdog = float(
            os.environ.get("MECHBENCH_WATCHDOG_SECONDS", str(DEFAULT_STALL_SECONDS))
        )
        # Which model to warm at startup so the first job does not pay
        # cold-start cost. Purely operational, and deliberately without a
        # built-in value: a runner that invents a model can execute a
        # protocol that never said which weights it wanted.
        warm = os.environ.get("MECHBENCH_WARM_MODEL_ID")

        env_key = os.environ.get("MECHBENCH_API_KEY")
        if env_key:
            # The environment owns the credential entirely when it
            # supplies one — see credentials.py on why the pair is not
            # split across sources.
            return cls(
                api_base_url=os.environ.get(
                    "MECHBENCH_API_URL", DEFAULT_API_URL
                ).rstrip("/"),
                api_key=env_key,
                poll_interval_seconds=poll,
                warm_model_id=warm,
                watchdog_seconds=watchdog,
            )

        stored = credentials.load()
        if stored:
            return cls(
                api_base_url=stored.api_url.rstrip("/"),
                api_key=stored.api_key,
                poll_interval_seconds=poll,
                warm_model_id=warm,
                runner_id=stored.runner_id,
                runner_name=stored.name,
                from_stored_credentials=True,
            )

        return cls(
            api_base_url=os.environ.get(
                "MECHBENCH_API_URL", DEFAULT_API_URL
            ).rstrip("/"),
            api_key=None,
            poll_interval_seconds=poll,
            warm_model_id=warm,
            watchdog_seconds=watchdog,
        )

    def require_api_key(self) -> str:
        if not self.api_key:
            raise RuntimeError(
                "this machine is not signed in.\n"
                "  Run `mechbench login` and follow the printed link,\n"
                "  or set MECHBENCH_API_KEY for a container or CI job."
            )
        return self.api_key
