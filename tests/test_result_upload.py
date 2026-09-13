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
        self.claim_tokens: dict[str, str] = {}

    def request_result_upload(self, job_id, content_hash, size_bytes, kind=None):
        self.grant_requests.append(size_bytes)
        return self.grant

    def upload_to_grant(self, grant, cbor_bytes, timeout=600.0):
        self.uploaded.append(len(cbor_bytes))

    def complete_job_uploaded(self, job_id, content_hash, kind=None):
        self.finalized.append(content_hash)

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
