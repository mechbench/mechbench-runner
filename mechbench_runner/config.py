from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from urllib.parse import urlsplit

from . import credentials
from .watchdog import DEFAULT_STALL_SECONDS

DEFAULT_API_URL = "https://api.mechbench.ai"
LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


class ApiUrlError(SystemExit):
    pass


def check_api_url(url: str) -> str:
    try:
        parsed = urlsplit(url)
        host = parsed.hostname
    except ValueError:
        parsed, host = None, None
    plain = parsed is not None and bool(host) and not (parsed.username or parsed.password)
    if plain and (parsed.scheme == "https"
                  or (parsed.scheme == "http" and host in LOCAL_HOSTS)):
        return url
    raise ApiUrlError(
        f"refused the API address {url!r}: it must be https:// (http:// only "
        f"for localhost). Check MECHBENCH_API_URL, or `mechbench login` again.")


@dataclass(frozen=True)
class Config:
    api_base_url: str
    api_key: str | None
    poll_interval_seconds: float
    warm_model_id: str | None
    watchdog_seconds: float = DEFAULT_STALL_SECONDS
    runner_id: str | None = None
    runner_name: str | None = None
    from_stored_credentials: bool = False

    @classmethod
    def from_env(cls) -> Config:
        poll = float(os.environ.get("MECHBENCH_POLL_INTERVAL_SECONDS", "2.0"))
        watchdog = float(
            os.environ.get("MECHBENCH_WATCHDOG_SECONDS", str(DEFAULT_STALL_SECONDS))
        )
        warm = os.environ.get("MECHBENCH_WARM_MODEL_ID")

        env_key = os.environ.get("MECHBENCH_API_KEY")
        if env_key:
            return cls(
                api_base_url=check_api_url(os.environ.get(
                    "MECHBENCH_API_URL", DEFAULT_API_URL
                ).rstrip("/")),
                api_key=env_key,
                poll_interval_seconds=poll,
                warm_model_id=warm,
                watchdog_seconds=watchdog,
            )

        stored = credentials.load()
        if stored:
            try:
                check_api_url(stored.api_url.rstrip("/"))
            except ApiUrlError as exc:
                print(f"ignoring the stored credential: {exc.code}", file=sys.stderr)
                stored = None
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
            api_base_url=check_api_url(os.environ.get(
                "MECHBENCH_API_URL", DEFAULT_API_URL
            ).rstrip("/")),
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
