from __future__ import annotations

from collections.abc import Mapping
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


def _compute_version() -> str:
    try:
        from mechbench_compute import __version__
    except ImportError:
        return ""
    return str(__version__)


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
        self.claim_tokens: dict[str, str] = {}

    def close(self) -> None:
        self._client.close()

    def _job_headers(self, job_id: str) -> dict[str, str]:
        tok = self.claim_tokens.get(job_id)
        return {"x-claim-token": tok} if tok else {}

    def __enter__(self) -> ApiClient:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def whoami(self) -> dict[str, Any]:
        res = self._client.get("/runners/me")
        self._raise_for_status(res)
        return res.json()

    def revoke_runner(self, runner_id: str) -> None:
        self._raise_for_status(self._client.delete(f"/runners/{runner_id}"))

    CAPABILITIES = "mlx-local,pure,remote"

    def claim_next_job(self) -> dict[str, Any] | None:
        headers = {"x-claim-token-supported": "1"}
        version = _compute_version()
        if version:
            headers["x-compute-version"] = version
        res = self._client.get(
            "/jobs/next", params={"capabilities": self.CAPABILITIES},
            headers=headers)
        if res.status_code == 204:
            return None
        self._raise_for_status(res)
        job = res.json()
        tok = job.get("claimToken") if isinstance(job, dict) else None
        if tok and job.get("id"):
            self.claim_tokens[str(job["id"])] = str(tok)
        return job

    def report_progress(self, job_id: str, num: int, den: int, *,
                        unit: str | None = None,
                        status: str | None = None,
                        node: dict | None = None,
                        resumed_from: dict | None = None,
                        spent_usd: float | None = None) -> None:
        body: dict[str, object] = {"num": num, "den": den}
        if unit is not None:
            body["unit"] = unit
        if status is not None:
            body["status"] = status
        if node is not None:
            body["node"] = node
        if resumed_from is not None:
            body["resumedFrom"] = resumed_from
        if spent_usd is not None:
            body["spentUsd"] = round(float(spent_usd), 6)
        res = self._client.patch(f"/jobs/{job_id}/progress", json=body,
                                 headers=self._job_headers(job_id))
        self._raise_for_status(res)

    def declare_preparing(self, job_id: str, steps: list[dict]) -> None:
        self._raise_for_status(
            self._client.patch(f"/jobs/{job_id}/preparing", json={"steps": steps},
                               headers=self._job_headers(job_id))
        )

    def report_preparing_step(self, job_id: str, step: dict) -> None:
        self._raise_for_status(
            self._client.patch(f"/jobs/{job_id}/preparing", json={"step": step},
                               headers=self._job_headers(job_id))
        )

    def fail_job(self, job_id: str, message: str,
                 timeout: float | None = None) -> None:
        kwargs: dict = {"json": {"message": message[:2000]}}
        if timeout is not None:
            kwargs["timeout"] = timeout
        res = self._client.post(f"/jobs/{job_id}/fail",
                                headers=self._job_headers(job_id), **kwargs)
        self._raise_for_status(res)

    def interrupt_job(self, job_id: str, message: str,
                      timeout: float | None = None) -> None:
        kwargs: dict = {"json": {"message": message[:2000]}}
        if timeout is not None:
            kwargs["timeout"] = timeout
        res = self._client.post(f"/jobs/{job_id}/interrupt",
                                headers=self._job_headers(job_id), **kwargs)
        self._raise_for_status(res)
        try:
            tok = res.json().get("claimToken")
        except ValueError:
            tok = None
        if tok:
            self.claim_tokens[job_id] = str(tok)

    def complete_job_cbor(
        self, job_id: str, cbor_bytes: bytes, content_hash: str
    ) -> None:
        res = self._client.post(
            f"/jobs/{job_id}/complete",
            content=cbor_bytes,
            headers={
                **self._job_headers(job_id),
                "content-type": "application/cbor",
                "x-content-hash": content_hash,
            },
        )
        self._raise_for_status(res)

    def request_result_upload(self, job_id: str, content_hash: str,
                              size_bytes: int,
                              kind: str | None = None) -> dict[str, Any] | None:
        body: dict[str, Any] = {"contentHash": content_hash, "sizeBytes": size_bytes}
        if kind:
            body["kind"] = kind
        res = self._client.post(f"/jobs/{job_id}/result-upload", json=body,
                                headers=self._job_headers(job_id))
        if res.status_code == 501:
            return None
        self._raise_for_status(res)
        return res.json()

    @staticmethod
    def upload_to_grant(grant: dict[str, Any], cbor_bytes: bytes,
                        timeout: float = 600.0) -> None:
        up = grant["upload"]
        res = httpx.put(up["url"], content=cbor_bytes, headers=dict(up["headers"]),
                        timeout=httpx.Timeout(timeout))
        if res.status_code >= 400:
            raise RuntimeError(
                f"upload to object storage refused: {res.status_code} "
                f"{res.text[:300]}")

    def complete_job_uploaded(self, job_id: str, content_hash: str,
                              kind: str | None = None,
                              missing: Mapping[str, Any] | None = None) -> None:
        body: dict[str, Any] = {"uploaded": True, "contentHash": content_hash}
        if kind:
            body["kind"] = kind
        if missing:
            body["missing"] = dict(missing)
        res = self._client.post(f"/jobs/{job_id}/complete", json=body,
                                headers=self._job_headers(job_id))
        self._raise_for_status(res)

    def complete_job_json(
        self, job_id: str, result_json: str, content_hash: str
    ) -> None:
        res = self._client.post(
            f"/jobs/{job_id}/complete",
            json={"resultJson": result_json, "contentHash": content_hash},
            headers=self._job_headers(job_id),
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

    def call(self, method: str, route: str, *,
             query: Mapping[str, Any] | None = None,
             body: Any = None) -> tuple[Any, Mapping[str, str]]:
        params = {k: ([str(x) for x in v] if isinstance(v, (list, tuple))
                      else "1" if v is True else str(v))
                  for k, v in (query or {}).items()
                  if v is not None and v is not False}
        res = self._client.request(method, route, params=params,
                                   json=body if body is not None else None)
        self._raise_for_status(res)
        if res.headers.get("content-type", "").startswith("application/json"):
            return res.json(), res.headers
        return res.content, res.headers

    @staticmethod
    def _raise_for_status(res: httpx.Response) -> None:
        if res.status_code >= 400:
            try:
                body = res.json()
            except ValueError:
                body = res.text
            raise ApiError(res.status_code, body)
