"""Thin typed wrapper around the mechbench-api REST surface.

Synchronous and ruthlessly minimal — the runner's consumer paths are
either a stdio MCP loop (one tool call at a time) or a 2-second
polling loop, so async machinery doesn't earn its keep. httpx's
sync `Client` is enough; it reuses a connection pool for free.
"""

from __future__ import annotations

from typing import Any

import httpx

from .config import Config


class ApiError(RuntimeError):
    def __init__(self, status: int, body: Any) -> None:
        super().__init__(f"mechbench-api {status}: {body}")
        self.status = status
        self.body = body


def register_runner(
    api_base_url: str,
    *,
    token: str,
    name: str,
    hostname: str,
    platform: str,
    runner_version: str,
    timeout: float = 30.0,
) -> dict[str, Any]:
    """`POST /runners/register` — trade a registration token for a key.

    The one call that carries no credential, because it is what produces
    one. Lives outside `ApiClient` for exactly that reason: the client
    requires a key in its constructor, and it should keep doing so.
    """
    res = httpx.post(
        f"{api_base_url.rstrip('/')}/runners/register",
        json={
            "token": token.strip(),
            "name": name,
            "hostname": hostname,
            "platform": platform,
            "runnerVersion": runner_version,
        },
        timeout=httpx.Timeout(timeout),
    )
    if res.status_code >= 400:
        try:
            body = res.json()
        except ValueError:
            body = res.text
        raise ApiError(res.status_code, body)
    return res.json()


def start_device_auth(
    api_base_url: str,
    *,
    name: str,
    hostname: str,
    platform: str,
    runner_version: str,
    timeout: float = 30.0,
) -> dict[str, Any]:
    """`POST /runners/device` — ask to be adopted, and say who is asking.

    Unauthenticated, like registration: having no credential is the
    problem being solved. The machine facts travel now so the person
    approving sees a machine rather than a blank.
    """
    res = httpx.post(
        f"{api_base_url.rstrip('/')}/runners/device",
        json={
            "name": name,
            "hostname": hostname,
            "platform": platform,
            "runnerVersion": runner_version,
        },
        timeout=httpx.Timeout(timeout),
    )
    if res.status_code >= 400:
        raise ApiError(res.status_code, _body_of(res))
    return res.json()


def poll_device_auth(
    api_base_url: str, device_code: str, timeout: float = 15.0
) -> dict[str, Any]:
    """`POST /runners/device/poll` — pending, approved, denied or expired."""
    res = httpx.post(
        f"{api_base_url.rstrip('/')}/runners/device/poll",
        json={"deviceCode": device_code},
        timeout=httpx.Timeout(timeout),
    )
    if res.status_code == 429:
        return {"status": "slow_down"}
    if res.status_code >= 400:
        raise ApiError(res.status_code, _body_of(res))
    return res.json()


def _body_of(res: httpx.Response) -> Any:
    try:
        return res.json()
    except ValueError:
        return res.text


class ApiClient:
    def __init__(self, config: Config) -> None:
        self.config = config
        api_key = config.require_api_key()
        self._client = httpx.Client(
            base_url=config.api_base_url,
            headers={"authorization": f"Bearer {api_key}"},
            timeout=httpx.Timeout(30.0),
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> ApiClient:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # --- this machine ------------------------------------------------------

    def whoami(self) -> dict[str, Any]:
        """`GET /runners/me` — which machine, which account, which scope."""
        res = self._client.get("/runners/me")
        self._raise_for_status(res)
        return res.json()

    def revoke_runner(self, runner_id: str) -> None:
        """`DELETE /runners/:id` — sign this machine out everywhere, not
        just locally. The key stops working immediately."""
        self._raise_for_status(self._client.delete(f"/runners/{runner_id}"))

    # --- job queue ---------------------------------------------------------

    def claim_next_job(self) -> dict[str, Any] | None:
        """Call `GET /jobs/next`. Returns None on 204 (no work)."""
        res = self._client.get(
            "/jobs/next", params={"capabilities": "mlx-local,pure"})
        if res.status_code == 204:
            return None
        self._raise_for_status(res)
        return res.json()

    def report_progress(self, job_id: str, num: int, den: int, *,
                        unit: str | None = None,
                        status: str | None = None,
                        node: dict | None = None) -> None:
        """PATCH `/jobs/:id/progress` (task 000252). Best-effort by
        contract: callers should tolerate failures — progress display
        degrades to the plain status chip, never blocks the job.

        `unit` says what the numbers count, so the board can render bytes
        as bytes. `status` promotes a claimed job from preparing to
        running, which is the moment weights are ready and compute starts.
        """
        body: dict[str, object] = {"num": num, "den": den}
        if unit is not None:
            body["unit"] = unit
        if status is not None:
            body["status"] = status
        if node is not None:
            # Where in the graph the run is (000316): index/count over
            # nodes, done/total within the current one.
            body["node"] = node
        res = self._client.patch(f"/jobs/{job_id}/progress", json=body)
        self._raise_for_status(res)

    def declare_preparing(self, job_id: str, steps: list[dict]) -> None:
        """PATCH `/jobs/:id/preparing` with the whole plan, so the board can
        show what is going to happen before any of it has."""
        self._raise_for_status(
            self._client.patch(f"/jobs/{job_id}/preparing", json={"steps": steps})
        )

    def report_preparing_step(self, job_id: str, step: dict) -> None:
        """PATCH one step by key. Best-effort, like progress: a failed report
        degrades the display, it never fails the job."""
        self._raise_for_status(
            self._client.patch(f"/jobs/{job_id}/preparing", json={"step": step})
        )

    def fail_job(self, job_id: str, message: str) -> None:
        """POST `/jobs/:id/fail` — mark a claimed job (and its run)
        failed with the error message. Failures are failed, not done."""
        res = self._client.post(f"/jobs/{job_id}/fail",
                                json={"message": message[:2000]})
        self._raise_for_status(res)

    def complete_job_cbor(
        self, job_id: str, cbor_bytes: bytes, content_hash: str
    ) -> None:
        """Post canonical-CBOR bytes to `POST /jobs/:id/complete` with
        content-type application/cbor and X-Content-Hash header. The
        content-addressed path (task 000186)."""
        res = self._client.post(
            f"/jobs/{job_id}/complete",
            content=cbor_bytes,
            headers={
                "content-type": "application/cbor",
                "x-content-hash": content_hash,
            },
        )
        self._raise_for_status(res)

    def complete_job_json(
        self, job_id: str, result_json: str, content_hash: str
    ) -> None:
        """Legacy JSON path. Kept for the 000181 deprecation window."""
        res = self._client.post(
            f"/jobs/{job_id}/complete",
            json={"resultJson": result_json, "contentHash": content_hash},
        )
        self._raise_for_status(res)

    def get_job(self, job_id: str) -> dict[str, Any]:
        res = self._client.get(f"/jobs/{job_id}")
        self._raise_for_status(res)
        return res.json()

    def list_jobs(self) -> list[dict[str, Any]]:
        res = self._client.get("/jobs")
        self._raise_for_status(res)
        return res.json()

    def fetch_object(self, path: str) -> bytes:
        res = self._client.get(f"/objects/{path}")
        self._raise_for_status(res)
        return res.content

    # --- plumbing ----------------------------------------------------------

    @staticmethod
    def _raise_for_status(res: httpx.Response) -> None:
        if res.status_code >= 400:
            try:
                body = res.json()
            except ValueError:
                body = res.text
            raise ApiError(res.status_code, body)
