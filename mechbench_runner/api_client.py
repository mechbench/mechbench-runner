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

    # --- job queue ---------------------------------------------------------

    def claim_next_job(self) -> dict[str, Any] | None:
        """Call `GET /jobs/next`. Returns None on 204 (no work)."""
        res = self._client.get(
            "/jobs/next", params={"capabilities": "mlx-local,pure"})
        if res.status_code == 204:
            return None
        self._raise_for_status(res)
        return res.json()

    def report_progress(self, job_id: str, num: int, den: int) -> None:
        """PATCH `/jobs/:id/progress` (task 000252). Best-effort by
        contract: callers should tolerate failures — progress display
        degrades to the plain status chip, never blocks the job."""
        res = self._client.patch(
            f"/jobs/{job_id}/progress", json={"num": num, "den": den}
        )
        self._raise_for_status(res)

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
