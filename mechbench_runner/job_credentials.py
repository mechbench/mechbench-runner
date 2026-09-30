from __future__ import annotations

import atexit
import threading
from typing import Any

from .api_client import ApiError


class JobCredentials:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._job_id: str | None = None
        self._secrets: dict[str, Any] = {}

    @property
    def job_id(self) -> str | None:
        return self._job_id

    @property
    def empty(self) -> bool:
        return self._job_id is None and not self._secrets

    def fetch(self, api: Any, job: dict[str, Any]) -> dict[str, Any]:
        job_id = str(job["id"])
        self.clear()
        delivered = job.pop("integrations", None)
        if isinstance(delivered, dict):
            got = delivered
        elif job.get("providers"):
            got = _ask(api, job_id)
        else:
            got = {}
        with self._lock:
            self._job_id = job_id
            for provider, credential in got.items():
                if isinstance(credential, dict):
                    self._secrets[str(provider)] = dict(credential)
            got.clear()
        return self._secrets

    def clear(self) -> None:
        with self._lock:
            for credential in self._secrets.values():
                if isinstance(credential, dict):
                    credential.clear()
            self._secrets.clear()
            self._job_id = None


def _ask(api: Any, job_id: str) -> dict[str, Any]:
    try:
        answer = api.job_credentials(job_id)
    except ApiError as exc:
        if exc.status == 404:
            return {}
        raise
    missing = answer.get("missing") or []
    if missing:
        print(f"[runner] {job_id}: no credential held for {', '.join(missing)}; "
              f"nodes that call {'it' if len(missing) == 1 else 'them'} will fail",
              flush=True)
    credentials = answer.get("credentials")
    return dict(credentials) if isinstance(credentials, dict) else {}


HELD = JobCredentials()
atexit.register(HELD.clear)
