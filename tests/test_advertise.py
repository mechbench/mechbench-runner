from __future__ import annotations

import json

import httpx
import pytest

pytest.importorskip("mechbench_compute")

from mechbench_runner import api_client  # noqa: E402
from mechbench_runner.api_client import ApiClient, advertise  # noqa: E402
from mechbench_runner.config import Config  # noqa: E402
from mechbench_runner.policy import Decision, PolicyHolder, check_claim  # noqa: E402

PIN_A = "sha256:" + "a" * 64
PIN_B = "sha256:" + "b" * 64


def _client(handler) -> ApiClient:
    api = ApiClient(Config(api_base_url="http://127.0.0.1:1", api_key="mbk_test",
                           poll_interval_seconds=0.01, warm_model_id=None,
                           runner_id="r_mine"))
    api._client.close()
    api._client = httpx.Client(base_url="http://127.0.0.1:1",
                               transport=httpx.MockTransport(handler))
    return api


class TestAdvertise:
    def test_it_says_what_the_matcher_reads(self, tmp_path):
        from mechbench_compute import __version__
        caps = advertise(tmp_path / "none.json")
        assert {"classes", "compute", "installs", "installed", "accelerator",
                "memory_gb"} <= set(caps)
        assert set(caps) - {"classes", "compute", "installs", "installed",
                            "accelerator", "memory_gb"} <= {
            "chip", "gpu_cores", "os", "python", "stack", "backends",
            "accelerators", "architectures", "architecture_levels",
            "architecture_levels_by_backend"}
        assert caps["accelerator"] in caps["accelerators"] or not caps["accelerators"].get(
            caps["accelerator"])
        assert set(caps["backends"]) == {b for names in caps["accelerators"].values()
                                         for b in names}
        assert caps["classes"] == ["local", "pure", "remote"]
        assert caps["compute"] == __version__
        assert caps["installs"] is True
        assert caps["installed"] == []
        assert caps["accelerator"] in {"metal", "cuda", "rocm", "tpu", "cpu"}
        assert isinstance(caps["memory_gb"], int) and caps["memory_gb"] >= 0

    @pytest.mark.parametrize("raw", [
        [PIN_A, PIN_B, PIN_A, "not-a-pin"],
        [{"hash": PIN_A, "address": "a/b/extensions/c"}, {"hash": PIN_B}],
        {"installed": [PIN_A, {"hash": PIN_B}]},
        {PIN_A: {"address": "a/b/extensions/c"}, PIN_B: {}},
    ])
    def test_it_reads_the_installed_hashes_in_any_shape(self, tmp_path, raw):
        path = tmp_path / "installed.json"
        path.write_text(json.dumps(raw))
        assert advertise(path)["installed"] == [PIN_A, PIN_B]

    def test_a_broken_file_is_nothing_installed(self, tmp_path):
        path = tmp_path / "installed.json"
        path.write_text("{not json")
        assert advertise(path)["installed"] == []

    def test_a_machine_with_no_backend_claims_no_local_job(self, monkeypatch):
        from mechbench_compute import backends

        monkeypatch.setattr(backends, "available", lambda *a, **k: [])
        assert api_client.read_classes() == ["pure", "remote"]
        assert advertise()["classes"] == ["pure", "remote"]
        seen: dict[str, str | None] = {}

        def handler(req: httpx.Request) -> httpx.Response:
            seen["query"] = req.url.params.get("capabilities")
            return httpx.Response(204)
        _client(handler).claim_next_job()
        assert seen["query"] == "pure,remote"
        _client(handler).claim_next_job("pure")
        assert seen["query"] == "pure"

    def test_it_advertises_only_the_backends_compute_s_executor_runs(self, monkeypatch):
        from mechbench_compute import backends

        from mechbench_runner import identity

        monkeypatch.setattr(backends, "advertise",
                            lambda *a, **k: {"accelerator": "cuda", "backends": []})
        assert identity.backends() == []
        assert api_client.read_classes() == ["pure", "remote"]
        torch = {"accelerator": "cuda", "backends": ["torch"]}
        monkeypatch.setattr(backends, "advertise", lambda *a, **k: torch)
        assert identity.backends() == ["torch"]
        assert api_client.read_classes() == ["local", "pure", "remote"]

    def test_the_set_is_compute_s_and_an_older_compute_s_one_accelerator_is_read_as_one(
            self, monkeypatch):
        from mechbench_compute import backends

        from mechbench_runner import identity

        mac = {"accelerator": "metal", "backends": ["mlx", "torch"],
               "accelerators": {"metal": ["mlx"], "cpu": ["torch"]}}
        monkeypatch.setattr(backends, "advertise", lambda *a, **k: mac)
        assert identity.backends() == ["mlx", "torch"]
        assert identity.accelerators() == {"metal": ["mlx"], "cpu": ["torch"]}
        monkeypatch.setattr(backends, "advertise",
                            lambda *a, **k: {"accelerator": "cuda", "backends": ["torch"]})
        assert identity.accelerators() == {"cuda": ["torch"]}
        monkeypatch.setattr(backends, "advertise",
                            lambda *a, **k: {"accelerator": "cpu", "backends": []})
        assert identity.accelerators() == {}

        def gone(*a, **k):
            raise ImportError("no compute")
        monkeypatch.setattr(backends, "advertise", gone)
        assert identity.accelerators() is None
        assert identity.backends() == []

    def test_the_architectures_are_keyed_by_backend_where_compute_keys_them(
            self, monkeypatch):
        from mechbench_compute import support

        from mechbench_runner import identity

        keyed = {"mlx": {"gemma3": "full"}, "torch": {"qwen3": "core", "gemma3": "core"}}
        monkeypatch.setattr(support, "architecture_levels_by_backend",
                            lambda *a, **k: keyed, raising=False)
        assert identity.architecture_levels_by_backend() == {
            "mlx": {"gemma3": "full"}, "torch": {"gemma3": "core", "qwen3": "core"}}
        monkeypatch.delattr(support, "architecture_levels_by_backend")
        assert identity.architecture_levels_by_backend() is None

    def test_the_architectures_are_compute_s_one_map_of_its_backends(
            self, monkeypatch):
        from mechbench_compute import support

        from mechbench_runner import identity

        levels = {"gemma3": "core", "llama": "core"}
        monkeypatch.setattr(support, "architecture_levels", lambda *a, **k: levels,
                            raising=False)
        assert identity.architecture_levels() == levels
        monkeypatch.delattr(support, "architecture_levels")
        monkeypatch.setattr(support, "local_architectures",
                            lambda *a, **k: [{"modelType": "gemma4", "level": "full"}])
        assert identity.architecture_levels() == {"gemma4": "full"}

    def test_the_accelerator_is_the_one_compute_detects(self, monkeypatch):
        from mechbench_compute import backends
        api_client._hardware.cache_clear()
        monkeypatch.setattr(backends, "detect_accelerator", lambda: "rocm",
                            raising=False)
        assert api_client._hardware()[0] == "rocm"
        api_client._hardware.cache_clear()

    def test_without_compute_s_detection_it_follows_the_hardware(self, monkeypatch):
        import mechbench_compute.seeds as seeds
        from mechbench_compute import backends
        monkeypatch.delattr(backends, "detect_accelerator", raising=False)
        api_client._hardware.cache_clear()
        m3 = {"mlx": "0.29.0", "chip": "Apple M3 Max", "memory_gb": 48.0}
        monkeypatch.setattr(seeds, "hardware_class", lambda: m3)
        assert api_client._hardware()[0] == "metal"
        api_client._hardware.cache_clear()
        monkeypatch.setattr(seeds, "hardware_class", lambda: {"mlx": None})
        monkeypatch.setattr(api_client, "_has_cuda", lambda: True)
        assert api_client._hardware()[0] == "cuda"
        api_client._hardware.cache_clear()
        monkeypatch.setattr(api_client, "_has_cuda", lambda: False)
        assert api_client._hardware()[0] == "cpu"
        api_client._hardware.cache_clear()


class TestItIsSent:
    def test_on_every_claim(self):
        seen: dict[str, str | None] = {}

        def handler(req: httpx.Request) -> httpx.Response:
            seen["caps"] = req.headers.get("x-runner-capabilities")
            seen["query"] = req.url.params.get("capabilities")
            return httpx.Response(204)
        _client(handler).claim_next_job()
        assert json.loads(seen["caps"] or "null") == advertise()
        assert seen["query"] == "local,pure,remote"

    def test_at_registration(self, monkeypatch):
        sent: dict = {}

        def post(url, json=None, timeout=None):
            sent.update(json)
            body = {"runner": {"id": "rnr_1"}, "apiKey": "mbk_x"}
            return httpx.Response(201, json=body, request=httpx.Request("POST", url))
        monkeypatch.setattr(api_client.httpx, "post", post)
        api_client.register_runner("http://127.0.0.1:1", token="mbr_t", name="n",
                                   hostname="h", platform="p", runner_version="0.46.0")
        assert sent["capabilities"] == advertise()

    def test_in_hello(self):
        from mechbench_runner.channel import LiveChannel
        channel = LiveChannel.__new__(LiveChannel)
        assert channel._hello()["capabilities"] == advertise()


class Api:
    def __init__(self) -> None:
        self.released: list[tuple[str, str, str]] = []

    def fetch_policy(self) -> dict:
        return {"policyId": "pol_personal", "version": 1, "name": "personal",
                "body": {"jobs": {"serve": ["own"], "allow": []},
                         "extensions": {"admit": ["own"], "allow": [], "network": "none"},
                         "upgrades": {"compute": "auto"}, "gc": {"unused_days": 30}}}

    def whoami(self) -> dict:
        return {"runner": {"id": "rnr_1", "userId": "u_alice", "scope": "user"},
                "account": {"userId": "u_alice", "handle": "alice",
                            "displayName": None},
                "scopeLabel": "alice"}

    def release_job(self, job_id: str, code: str, message: str) -> None:
        self.released.append((job_id, code, message))


def test_check_claim_reads_the_claim_as_the_api_shapes_it(tmp_path):
    api = Api()
    holder = PolicyHolder(tmp_path / "policy.json")
    holder.start(api)
    item = {
        "address": "alice/tools/extensions/interp-extras", "version": 2, "hash": PIN_A,
        "name": "alice/tools/extensions/interp-extras",
        "package": {"name": "mechbench-ext-interp-extras", "python": ">=3.12",
                    "sdist": "~hash/sha256:" + "c" * 64, "wheel": None, "lock": None},
        "needs": [], "state": "draft", "party": "third",
        "projectOwner": {"kind": "user", "id": "u_alice"}, "approvedBy": [],
    }
    claim = {"id": "j_1", "creatorId": "u_alice", "projectId": "prj_1",
             "projectOwner": {"kind": "user", "id": "u_alice"}, "creatorOrgIds": [],
             "policy": {"id": "pol_personal", "version": 1}, "install": [item]}
    assert check_claim(api, holder, claim) == [Decision(True), Decision(True)]
    other = {**claim, "creatorId": "u_bob",
             "projectOwner": {"kind": "user", "id": "u_bob"}}
    assert [a.ok for a in check_claim(api, holder, other)] == [False, True]
    assert len(api.released) == 1
