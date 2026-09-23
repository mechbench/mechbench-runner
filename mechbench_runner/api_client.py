"""Thin typed wrapper around the mechbench-api REST surface.

Synchronous and ruthlessly minimal — the runner's consumer paths are
either a stdio MCP loop (one tool call at a time) or a 2-second
polling loop, so async machinery doesn't earn its keep. httpx's
sync `Client` is enough; it reuses a connection pool for free.
"""

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


def _compute_version() -> str:
    """The loaded mechbench-compute's version, or "" when it is absent."""
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
        #: Per-claim tokens by job id (000491): the secret the claim
        #: returned, sent as X-Claim-Token on every job-scoped write so
        #: the server can tell THIS claim from a stale process holding the
        #: same key. Kept in memory; the spool persists it beside the
        #: result for a late delivery after a restart.
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

    #: What this machine can run. `remote` (epic 000334) says only that
    #: it has a network and can hold the owner's provider credentials —
    #: every runner does — so a job whose only non-pure work is calling
    #: someone else's API is claimable here.
    CAPABILITIES = "mlx-local,pure,remote"

    def claim_next_job(self) -> dict[str, Any] | None:
        """Call `GET /jobs/next`. Returns None on 204 (no work)."""
        # Ask for a per-claim token (000491). An API from before this
        # ignores the header and returns none; an API with it issues one
        # only to runners that ask, so neither side breaks the other.
        # The compute version this process will run the job with, so a
        # runs listing can say which version produced a result: the one
        # loaded here, not whatever is installed on disk by now.
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
        # The claim's secret rides in the claim response, once (000491).
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
        if resumed_from is not None:
            # Where a resumed job picked up (epic 000320); the server
            # keeps it on the job row, never in the result.
            body["resumedFrom"] = resumed_from
        if spent_usd is not None:
            # What this job has spent with external providers so far
            # (000338). The running TOTAL, never a delta, so a dropped
            # report costs nothing.
            body["spentUsd"] = round(float(spent_usd), 6)
        res = self._client.patch(f"/jobs/{job_id}/progress", json=body,
                                 headers=self._job_headers(job_id))
        self._raise_for_status(res)

    def declare_preparing(self, job_id: str, steps: list[dict]) -> None:
        """PATCH `/jobs/:id/preparing` with the whole plan, so the board can
        show what is going to happen before any of it has."""
        self._raise_for_status(
            self._client.patch(f"/jobs/{job_id}/preparing", json={"steps": steps},
                               headers=self._job_headers(job_id))
        )

    def report_preparing_step(self, job_id: str, step: dict) -> None:
        """PATCH one step by key. Best-effort, like progress: a failed report
        degrades the display, it never fails the job."""
        self._raise_for_status(
            self._client.patch(f"/jobs/{job_id}/preparing", json={"step": step},
                               headers=self._job_headers(job_id))
        )

    def fail_job(self, job_id: str, message: str,
                 timeout: float | None = None) -> None:
        """POST `/jobs/:id/fail` — mark a claimed job (and its run)
        failed with the error message. Failures are failed, not done."""
        kwargs: dict = {"json": {"message": message[:2000]}}
        if timeout is not None:
            # The watchdog's dying breath: a bounded wait, because the
            # process is presumed stuck and MUST still exit.
            kwargs["timeout"] = timeout
        res = self._client.post(f"/jobs/{job_id}/fail",
                                headers=self._job_headers(job_id), **kwargs)
        self._raise_for_status(res)

    def interrupt_job(self, job_id: str, message: str,
                      timeout: float | None = None) -> None:
        """POST `/jobs/:id/interrupt` (epic 000320) — the job had no
        error of its own; the runner went quiet (watchdog death,
        orphaned by a restart). Claim, progress and resultPath survive
        on the server; this machine re-claims and resumes, or
        completes late from its spool.

        The one job-scoped write the server authorizes by the claim's
        IDENTITY rather than its token (task 000511): a process that
        does not hold the token — `mechbench restart --force`, or a
        reconcile after a crash — may still report that this machine is
        not executing the job. The server rotates the token when it
        accepts one of those and hands the new one back here, which is
        what lets a spooled result be delivered afterwards."""
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
            # Overwrite, unlike a spooled token: this one is newer than
            # anything this process holds, by construction.
            self.claim_tokens[job_id] = str(tok)

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
                **self._job_headers(job_id),
                "content-type": "application/cbor",
                "x-content-hash": content_hash,
            },
        )
        self._raise_for_status(res)

    # --- the presigned result upload (000492) -------------------------------
    #
    # A result larger than the API instance can hold never passes through
    # it: ask for a GRANT (a presigned PUT bound to the job's own result
    # key, the exact length and the sha256), PUT the bytes straight to
    # object storage, then FINALIZE with the hash. The API confirms the
    # store's account of what landed before the job is done.

    def request_result_upload(self, job_id: str, content_hash: str,
                              size_bytes: int,
                              kind: str | None = None) -> dict[str, Any] | None:
        """`POST /jobs/:id/result-upload`. Returns the grant, or None when
        this deployment's store cannot grant one (501) — the caller then
        takes the direct path. Any other refusal raises."""
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
        """PUT the bytes to the grant's URL with exactly the headers the
        grant names — they are part of the signature. No bearer token:
        this is object storage, not the API, and the URL is the
        capability."""
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
        """Finalize a presigned upload: `POST /jobs/:id/complete` with
        `{uploaded: true, contentHash}` and no body.

        `missing` is the result manifest's `nodes_missing` (000515). It
        is declared here because on this path the API never holds the
        bytes to read it from — the same reason `kind` is declared —
        and without it a run that finished having lost a branch would
        land as a plain `done`."""
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
        """Legacy JSON path. Kept for the 000181 deprecation window."""
        res = self._client.post(
            f"/jobs/{job_id}/complete",
            json={"resultJson": result_json, "contentHash": content_hash},
            headers=self._job_headers(job_id),
        )
        self._raise_for_status(res)

    # Binding a protocol and finding a run by binding moved to the bench
    # client library (mechbench_compute.bench.launch / results_for, task
    # 000450) — the `run`/`result` verbs are thin wrappers over it, so the
    # runner keeps no second copy of that transport. `get_job` and
    # `fetch_object` stay: the job loop and the MCP server use them.

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
