"""The runner carries the per-claim token (task 000491).

The claim response returns it once; every job-scoped write sends it as
X-Claim-Token; the spool persists it beside the result so a late delivery
after a restart — by a process that never saw the claim — still carries
the claim that produced it.
"""

from __future__ import annotations

import json

import httpx
import pytest

pytest.importorskip("mechbench_compute")

from mechbench_runner import job_runner as jr  # noqa: E402
from mechbench_runner.api_client import ApiClient  # noqa: E402
from mechbench_runner.config import Config  # noqa: E402


def _client(handler) -> ApiClient:
    api = ApiClient(Config(api_base_url="http://127.0.0.1:1", api_key="mbk_test",
                           poll_interval_seconds=0.01, warm_model_id=None,
                           runner_id="r_mine"))
    api._client = httpx.Client(base_url="http://127.0.0.1:1",
                               headers={"authorization": "Bearer mbk_test"},
                               transport=httpx.MockTransport(handler))
    return api


class Recorder:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str | None]] = []

    def __call__(self, req: httpx.Request) -> httpx.Response:
        self.calls.append((req.method, req.url.path, req.headers.get("x-claim-token")))
        if req.url.path == "/jobs/next":
            return httpx.Response(200, json={"id": "j_1", "protocolKind": "pipeline",
                                             "claimToken": "tok-first"})
        return httpx.Response(200, json={"ok": True})


class TestTheClientCarriesTheToken:
    def test_the_claim_response_token_is_kept_by_job(self):
        rec = Recorder()
        api = _client(rec)
        job = api.claim_next_job()
        assert job["id"] == "j_1"
        assert api.claim_tokens == {"j_1": "tok-first"}

    def test_the_claim_asks_for_a_token(self):
        """The negotiation that keeps a mixed-version fleet working: the
        server issues a token only to a runner that says it can carry one."""
        seen: dict[str, str | None] = {}

        def handler(req: httpx.Request) -> httpx.Response:
            seen["supported"] = req.headers.get("x-claim-token-supported")
            return httpx.Response(204)
        _client(handler).claim_next_job()
        assert seen["supported"] == "1"

    def test_every_job_scoped_write_sends_it(self):
        rec = Recorder()
        api = _client(rec)
        api.claim_next_job()
        api.report_progress("j_1", 1, 2, status="running")
        api.declare_preparing("j_1", [{"key": "w", "label": "w", "status": "active"}])
        api.report_preparing_step("j_1", {"key": "w", "label": "w", "status": "done"})
        api.interrupt_job("j_1", "restart", timeout=5.0)
        api.fail_job("j_1", "boom")
        api.complete_job_cbor("j_1", b"\xa0", "sha256:00")
        api.complete_job_json("j_1", "{}", "sha256:00")
        writes = [(m, p, t) for m, p, t in rec.calls if p != "/jobs/next"]
        assert len(writes) == 7
        assert all(t == "tok-first" for _, _, t in writes), writes

    def test_a_job_it_never_claimed_sends_no_header(self):
        rec = Recorder()
        api = _client(rec)
        api.report_progress("j_other", 1, 2)
        assert rec.calls[-1][2] is None

    def test_the_claim_response_without_a_token_is_tolerated(self):
        """An API from before 000491 returns no claimToken; nothing breaks
        and no header is sent — the server's null-hash rows pass."""
        def handler(req: httpx.Request) -> httpx.Response:
            if req.url.path == "/jobs/next":
                return httpx.Response(200, json={"id": "j_old"})
            return httpx.Response(200, json={"ok": True})
        api = _client(handler)
        api.claim_next_job()
        assert api.claim_tokens == {}


class TestTheSpoolPersistsIt:
    def test_the_token_is_written_beside_the_result_and_read_back(self):
        jr._spool_result("j_1", b"\xa0", "00", "tok-first")
        assert jr._spooled_result("j_1") == (b"\xa0", "00")
        assert jr._spooled_claim_token("j_1") == "tok-first"
        path = jr.spool_dir() / "j_1" / "claim.token"
        assert oct(path.stat().st_mode & 0o777) == "0o600"

    def test_no_token_means_no_file(self):
        jr._spool_result("j_2", b"\xa0", "00")
        assert jr._spooled_claim_token("j_2") is None
        assert jr._spooled_result("j_2") == (b"\xa0", "00")

    def test_clearing_the_spool_takes_the_token_with_it(self):
        jr._spool_result("j_3", b"\xa0", "00", "tok")
        jr._clear_spool("j_3")
        assert jr._spooled_claim_token("j_3") is None
