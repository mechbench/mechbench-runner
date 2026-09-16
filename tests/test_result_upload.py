"""The runner's half of the presigned result upload (task 000492).

Large results go grant → PUT to object storage → finalize; small ones
take the direct path they always did; a store that cannot grant (501)
sends the runner back to the direct path. The grant and the finalize
carry the claim token like every other job-scoped write.
"""

from __future__ import annotations

import hashlib
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
    api.claim_tokens["j_1"] = "tok"
    return api


BYTES = b"\xa1\x64kind\x64note" * 1000
DIGEST = hashlib.sha256(BYTES).hexdigest()
GRANT = {"upload": {"url": "https://bucket.s3.test/key?sig=1", "method": "PUT",
                    "headers": {"content-type": "application/cbor",
                                "content-length": str(len(BYTES)),
                                "x-amz-checksum-sha256": "abc="},
                    "expiresAt": "2026-09-13T21:30:00Z"},
         "contentHash": f"sha256:{DIGEST}", "sizeBytes": len(BYTES)}


class TestTheClient:
    def test_a_grant_is_requested_with_the_claim_token(self):
        seen = {}

        def handler(req: httpx.Request) -> httpx.Response:
            seen["path"] = req.url.path
            seen["tok"] = req.headers.get("x-claim-token")
            seen["body"] = json.loads(req.content)
            return httpx.Response(200, json=GRANT)
        grant = _client(handler).request_result_upload("j_1", f"sha256:{DIGEST}", len(BYTES),
                                                       kind="document_collection")
        assert seen["path"] == "/jobs/j_1/result-upload"
        assert seen["tok"] == "tok"
        assert seen["body"] == {"contentHash": f"sha256:{DIGEST}", "sizeBytes": len(BYTES),
                                "kind": "document_collection"}
        assert grant["upload"]["url"].startswith("https://bucket")

    def test_a_store_that_cannot_grant_returns_none(self):
        def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(501, json={"code": "UPLOAD_GRANT_UNSUPPORTED"})
        assert _client(handler).request_result_upload("j_1", f"sha256:{DIGEST}", 10) is None

    def test_any_other_refusal_raises(self):
        def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(413, json={"code": "BODY_TOO_LARGE"})
        with pytest.raises(Exception):
            _client(handler).request_result_upload("j_1", f"sha256:{DIGEST}", 10)

    def test_the_upload_sends_exactly_the_grants_headers_and_no_bearer(self, monkeypatch):
        sent = {}

        def fake_put(url, *, content, headers, timeout):
            sent.update(url=url, content=content, headers=headers)
            return httpx.Response(200)
        monkeypatch.setattr(httpx, "put", fake_put)
        ApiClient.upload_to_grant(GRANT, BYTES)
        assert sent["url"] == GRANT["upload"]["url"]
        assert sent["content"] == BYTES
        assert sent["headers"] == GRANT["upload"]["headers"]
        assert "authorization" not in {k.lower() for k in sent["headers"]}

    def test_a_refused_upload_raises(self, monkeypatch):
        monkeypatch.setattr(httpx, "put", lambda *a, **k: httpx.Response(
            400, text="<Error>BadDigest</Error>"))
        with pytest.raises(RuntimeError, match="refused: 400"):
            ApiClient.upload_to_grant(GRANT, BYTES)

    def test_finalize_carries_the_token_and_no_body_bytes(self):
        seen = {}

        def handler(req: httpx.Request) -> httpx.Response:
            seen["tok"] = req.headers.get("x-claim-token")
            seen["body"] = json.loads(req.content)
            return httpx.Response(200, json={"ok": True})
        _client(handler).complete_job_uploaded("j_1", f"sha256:{DIGEST}")
        assert seen["tok"] == "tok"
        assert seen["body"] == {"uploaded": True, "contentHash": f"sha256:{DIGEST}"}


class RecordingApi:
    """The parts of ApiClient that _deliver touches."""

    def __init__(self, grant: dict | None):
        self.grant = grant
        self.grant_requests: list[int] = []
        self.direct: list[int] = []
        self.finalized: list[str] = []
        self.uploaded: list[int] = []
        self.declared_missing: list[dict | None] = []
        self.claim_tokens: dict[str, str] = {}

    def request_result_upload(self, job_id, content_hash, size_bytes, kind=None):
        self.grant_requests.append(size_bytes)
        return self.grant

    def upload_to_grant(self, grant, cbor_bytes, timeout=600.0):
        self.uploaded.append(len(cbor_bytes))

    def complete_job_uploaded(self, job_id, content_hash, kind=None, missing=None):
        self.finalized.append(content_hash)
        self.declared_missing.append(missing)

    def complete_job_cbor(self, job_id, cbor_bytes, content_hash):
        self.direct.append(len(cbor_bytes))


def _runner(monkeypatch):
    class StubControl:
        def __init__(self, _state, path=None):
            self.path = path or "/tmp/stub.sock"

        def start(self):
            return None

        def stop(self):
            return None

    monkeypatch.setattr(jr, "ControlServer", StubControl)
    return jr.JobRunner(Config(
        api_base_url="http://127.0.0.1:1", api_key="k",
        poll_interval_seconds=0.01, warm_model_id=None, runner_id="r_mine"))


class TestDeliverChoosesThePath:
    def test_a_small_result_goes_direct_without_asking(self, monkeypatch):
        r = _runner(monkeypatch)
        api = RecordingApi(grant=GRANT)
        r._deliver(api, "j_1", b"\xa0" * 100, "00")
        assert api.direct == [100] and api.grant_requests == []

    def test_a_large_result_goes_grant_upload_finalize(self, monkeypatch):
        r = _runner(monkeypatch)
        monkeypatch.setattr(jr, "PRESIGN_THRESHOLD_BYTES", 50)
        api = RecordingApi(grant=GRANT)
        big = b"\xa0" * 200
        r._deliver(api, "j_1", big, "00")
        assert api.grant_requests == [200]
        assert api.uploaded == [200]
        assert api.finalized == ["sha256:00"]
        assert api.direct == []

    def test_a_store_that_cannot_grant_falls_back_to_direct(self, monkeypatch):
        r = _runner(monkeypatch)
        monkeypatch.setattr(jr, "PRESIGN_THRESHOLD_BYTES", 50)
        api = RecordingApi(grant=None)
        r._deliver(api, "j_1", b"\xa0" * 200, "00")
        assert api.grant_requests == [200]
        assert api.direct == [200] and api.finalized == []

    def test_a_delivery_failure_keeps_the_spool(self, monkeypatch):
        """Unchanged contract from 000320: the result exists; the server's
        state has to catch up. A failed upload is not a failed job."""
        r = _runner(monkeypatch)
        monkeypatch.setattr(jr, "PRESIGN_THRESHOLD_BYTES", 50)

        class Refusing(RecordingApi):
            def upload_to_grant(self, grant, cbor_bytes, timeout=600.0):
                raise RuntimeError("upload to object storage refused: 403")
        api = Refusing(grant=GRANT)
        jr._spool_result("j_1", b"\xa0" * 200, "00")
        r._deliver(api, "j_1", b"\xa0" * 200, "00")  # must not raise
        assert api.finalized == []
        assert (jr.spool_dir() / "j_1" / "result.cbor").is_file()


class TestDeclaringWhatDidNotRun:
    """Task 000515. The API reads `nodes_missing` off the result bytes
    when it has them; on the grant path it never does, so the runner
    declares it — and a run that finished having lost a branch lands as
    `done_with_missing` rather than a plain `done`."""

    MISSING = {"sonnet": {"reason": "RuntimeError: that provider said no",
                          "source": ["sonnet"]}}

    def test_the_grant_path_declares_it(self, monkeypatch):
        r = _runner(monkeypatch)
        monkeypatch.setattr(jr, "PRESIGN_THRESHOLD_BYTES", 50)
        api = RecordingApi(grant=GRANT)
        r._deliver(api, "j_1", b"\xa0" * 200, "00", self.MISSING)
        assert api.declared_missing == [self.MISSING]

    def test_the_direct_path_declares_nothing(self, monkeypatch):
        """The bytes go to the API, which reads the manifest itself. A
        declaration here would be a second, weaker copy of one fact."""
        r = _runner(monkeypatch)
        api = RecordingApi(grant=GRANT)
        r._deliver(api, "j_1", b"\xa0" * 100, "00", self.MISSING)
        assert api.direct == [100] and api.declared_missing == []

    def test_a_late_delivery_declares_it_from_the_spool(self, monkeypatch):
        """The process that produced the result is gone; the spool is
        what remembers. Decoding a multi-gigabyte CBOR to recover one
        key is not the alternative."""
        jr._spool_result("j_2", b"\xa0" * 200, "00", None, self.MISSING)
        assert jr._spooled_missing("j_2") == self.MISSING
        assert jr._spooled_missing("j_never") is None

    def test_a_result_that_lost_nothing_spools_no_sidecar(self):
        jr._spool_result("j_3", b"\xa0" * 10, "00")
        assert not (jr.spool_dir() / "j_3" / "result.missing.json").exists()
        assert jr._spooled_missing("j_3") is None

    def test_missing_is_read_off_a_payload_either_way_it_is_wrapped(self):
        wrapped = {"payload": {"kind": "run/result", "nodes_missing": self.MISSING}}
        bare = {"kind": "run/result", "nodes_missing": self.MISSING}
        assert jr._missing_of(wrapped) == self.MISSING
        assert jr._missing_of(bare) == self.MISSING
        # The common case: nothing missing, and nothing to say about it.
        assert jr._missing_of({"payload": {"kind": "run/result"}}) is None
        assert jr._missing_of({"payload": {"nodes_missing": {}}}) is None
        assert jr._missing_of(None) is None

    def test_a_finished_job_clears_its_spool_whichever_finish_it_was(self):
        """A spooled result for a job the server has already finished has
        nowhere to go. `done_with_missing` is finished — left out of that
        set, the flush would offer it forever."""
        assert "done_with_missing" in jr.TERMINAL_SERVER_STATUS
        assert "done" in jr.TERMINAL_SERVER_STATUS

    def test_a_declaration_is_bounded_so_a_finalize_is_never_refused(self):
        """The API caps a reason at 2000 and refuses a longer one — and a
        refused finalize sends the result back to the spool, to be
        retried forever. The manifest keeps the whole reason; what
        travels is a summary."""
        huge = {"n": {"reason": "x" * 9000, "source": ["n"]}}
        got = jr._missing_of({"payload": {"nodes_missing": huge}})
        assert got is not None
        assert len(got["n"]["reason"]) == jr.MAX_MISSING_REASON
        assert got["n"]["source"] == ["n"]

    def test_a_declaration_carries_at_most_so_many_nodes(self):
        many = {f"n{i}": {"reason": "died", "source": [f"n{i}"]} for i in range(200)}
        got = jr._missing_of({"payload": {"nodes_missing": many}})
        assert got is not None and len(got) == jr.MAX_MISSING_NODES
