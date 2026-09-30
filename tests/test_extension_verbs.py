from __future__ import annotations

import json
import pathlib
import shutil
import subprocess
import tarfile
from typing import Any

import pytest

from mechbench import cli
from mechbench_runner.config import Config
from mechbench_runner.verbs import Ctx, VerbError, invoke, noun

CFG = Config(
    api_base_url="http://api.test",
    api_key="mbk_test",
    poll_interval_seconds=0.01,
    warm_model_id=None,
    runner_id=None,
)
MODELS = pathlib.Path(__file__).resolve().parents[2] / "mechbench-models"
HASH = "sha256:" + "ab" * 32
OPS = {
    "ops": [
        {"address": "direction/apply", "family": "direction", "leaf": "apply",
         "summary": "Add a direction.", "requires": "mlx-local", "needs": [],
         "inputs": [{"name": "direction", "kinds": ["direction/vector"], "required": True, "many": False},
                    {"name": "records", "kinds": ["records/record"], "required": True, "many": True}],
         "output": {"kind": "records/record", "collection": True}, "source": {"kind": "core"}},
        {"address": "alice/lab/ops/geometry/align", "family": "geometry", "leaf": "align",
         "summary": "Align two sets.", "requires": "pure", "needs": [],
         "inputs": [{"name": "a", "kinds": ["collection"], "required": True, "many": False}],
         "output": None,
         "source": {"kind": "extension", "address": "alice/lab/extensions/interp-extras",
                    "version": 2, "hash": HASH, "state": "verified", "owner": "alice",
                    "party": "third", "visibility": "public", "flags": []}},
    ]
}
RUNNERS = [
    {"id": "rnr_1", "name": "studio", "connected": True, "policy": {"id": "pol_1", "name": "p", "version": 3}},
    {"id": "rnr_2", "name": "laptop", "connected": False, "policy": {"id": "pol_personal", "name": "x", "version": 1}},
]


class FakeApi:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict, Any]] = []
        self.stored: dict[str, bytes] = {}
        self.existing = False

    def api(self, _ctx, method, route, *, query=None, body=None):
        q = {k: v for k, v in (query or {}).items() if v is not None}
        self.calls.append((method, route, q, body))
        if route == "/ops":
            return OPS, {}
        if route.startswith("/ops/"):
            return {**OPS["ops"][0], "op": {}, "declaration": None, "extension": None}, {}
        if route == "/runners":
            return RUNNERS, {}
        if route == "/policies" and method == "GET":
            return [{"id": "pol_1", "name": "studio"}, {"id": "pol_personal", "name": "personal"}], {}
        if route == "/objects/~meta":
            return {"kind": "direction/vector"}, {}
        if method == "PUT" and route.startswith("/extensions/") and route.count("/") == 4:
            return {"address": "alice/lab/extensions/count-things", "version": body["version"],
                    "hash": HASH, "state": "draft", "visibility": "private"}, {}
        if route.startswith("/orgs/"):
            if route == "/orgs/acme":
                return {"org": {"id": "org_acme", "handle": "acme"}, "role": "admin"}, {}
            from mechbench_runner.api_client import ApiError

            raise ApiError(404, {"code": "NOT_FOUND", "error": "not found"})
        if route == "/policies/pol_1" and method == "GET":
            return {"id": "pol_1", "name": "studio", "orgId": None, "version": 3,
                    "body": {"jobs": {"serve": ["own"], "allow": []},
                             "extensions": {"admit": ["own"], "allow": [], "network": "none"},
                             "upgrades": {"compute": "auto"}, "gc": {"unused_days": 30}}}, {}
        if route.endswith("/check"):
            return {"address": "alice/lab/extensions/count-things", "version": 1, "hash": HASH,
                    "state": "pending", "visibility": "private", "job": "j_9",
                    "waitingFor": ["no runner of yours is connected"]}, {}
        if method == "GET" and "/extensions/" in route:
            if not self.existing:
                from mechbench_runner.api_client import ApiError

                raise ApiError(404, {"code": "NOT_FOUND", "error": "not found"})
            return {"address": "alice/lab/extensions/count-things", "version": 1, "hash": HASH,
                    "manifest": {"visibility": "org", "approvals": [
                        {"org": "org_acme", "by": "u_jo", "at": "2026-10-02T00:00:00Z"}]},
                    "versions": [{"version": 1, "state": "verified", "hash": HASH, "createdAt": "t"}],
                    "usage": {"protocols": 0, "runs": 0, "replications": 0, "citations": 0}}, {}
        return {"ok": True}, {}

    def put_bytes(self, _ctx, path, data, kind):
        import hashlib

        self.stored[path] = data
        self.calls.append(("PUT", f"/objects/{path}", {"kind": kind}, None))
        return {"path": path, "hash": f"sha256:{hashlib.sha256(data).hexdigest()}"}


@pytest.fixture
def fake(monkeypatch):
    f = FakeApi()
    monkeypatch.setattr(Ctx, "api", lambda self, m, r, **k: f.api(self, m, r, **k))
    monkeypatch.setattr(Ctx, "put_bytes", lambda self, p, d, kind: f.put_bytes(self, p, d, kind))
    monkeypatch.setattr(Config, "from_env", classmethod(lambda cls: CFG))
    return f


def call(noun_name, verb, args):
    return invoke(Ctx(CFG), noun_name, verb, args)


@pytest.fixture(scope="module")
def package(tmp_path_factory) -> pathlib.Path:
    root = tmp_path_factory.mktemp("ext") / "count-things"
    call("extension", "new", {"scope": "alice/lab", "name": "count-things",
                              "op": "records/count", "dir": str(root)})
    return root


class TestOp:
    def test_list_passes_its_filters_and_pages_here(self, fake):
        out = call("op", "list", {"reads": "direction/vector", "search": "dir", "limit": 1})
        assert fake.calls[-1][:3] == ("GET", "/ops", {"reads": "direction/vector", "q": "dir"})
        assert [o["address"] for o in out["items"]] == ["direction/apply"]
        assert out["items"][0]["source"] == "core" and out["next"] == 1

    def test_read_takes_core_and_extension_addresses(self, fake):
        call("op", "read", {"address": "geometry/align"})
        assert fake.calls[-1][1] == "/ops/geometry/align"
        call("op", "read", {"address": "alice/lab/ops/geometry/align@2"})
        assert fake.calls[-1][1] == "/ops/alice/lab/ops/geometry/align@2"

    def test_next_is_list_by_reads_with_the_ports_that_take_it(self, fake):
        out = call("op", "next", {"kind": "direction/vector"})
        assert fake.calls[-1][2] == {"reads": "direction/vector"}
        assert out["kind"] == "direction/vector"
        assert [(o["address"], o["ports"]) for o in out["items"]] == [
            ("direction/apply", ["direction", "records"]),
            ("alice/lab/ops/geometry/align", ["a"]),
        ]
        assert out["items"][1]["source"] == "alice/lab/extensions/interp-extras@2"

    def test_next_of_an_object_is_next_of_its_kind(self, fake):
        out = call("op", "next", {"kind": "alice/lab/results/j_1/dir"})
        assert fake.calls[0][1:3] == ("/objects/~meta", {"path": "alice/lab/results/j_1/dir"})
        assert out["kind"] == "direction/vector"


class TestScaffold:
    def test_new_writes_the_package_shape(self, package):
        files = sorted(str(p.relative_to(package)) for p in package.rglob("*") if p.is_file())
        assert files == [
            "README.md",
            "count_things/__init__.py",
            "count_things/kinds/__init__.py",
            "count_things/ops/__init__.py",
            "count_things/ops/records/__init__.py",
            "count_things/ops/records/count.py",
            "pyproject.toml",
        ]
        py = (package / "pyproject.toml").read_text()
        assert '[project.entry-points."mechbench.extensions"]' in py
        assert 'count-things = "count_things:MANIFEST"' in py
        op = (package / "count_things/ops/records/count.py").read_text()
        assert "OP = Op(" in op and "def run(ctx, inputs, params):" in op

    def test_new_refuses_what_conformance_would(self, tmp_path):
        with pytest.raises(VerbError, match="verb"):
            call("extension", "new", {"scope": "a/b", "name": "x", "op": "records/tally",
                                      "dir": str(tmp_path / "x")})
        with pytest.raises(VerbError, match="owner"):
            call("extension", "new", {"scope": "ab", "name": "x", "op": "records/count"})

    def test_the_skeleton_passes_the_real_conformance_run(self, package):
        out = call("extension", "test", {"dir": str(package)})
        assert out["passed"] is True, out
        assert out["declarations"] == "passed" and out["double_run"] == "identical"
        assert [e["op"] for e in out["examples"]] == ["alice/lab/ops/records/count"]

    def test_a_failing_package_exits_non_zero(self, package, tmp_path, capsys):
        broken = tmp_path / "broken"
        shutil.copytree(package, broken)
        op = broken / "count_things/ops/records/count.py"
        op.write_text(op.read_text().replace('return collection("records/record"',
                                             'return collection("records/nothing"'))
        assert cli.main(["extension", "test", str(broken)]) == 1
        assert '"passed": false' in capsys.readouterr().out
        assert cli.main(["extension", "test", str(package)]) == 0


def validate_with_models(body: dict[str, Any]) -> None:
    node = shutil.which("node")
    dist = MODELS / "dist" / "index.js"
    if node is None or not dist.exists():
        assert body["kind"] == "extension" and body["package"]["sdist"].startswith("~hash/sha256:")
        assert {"owner", "project", "name", "version", "tier", "provides", "min_compute"} <= set(body)
        return
    script = (
        f"import {{ ExtensionPushSchema }} from {json.dumps(str(dist))};"
        "let s='';process.stdin.on('data',d=>s+=d).on('end',()=>{"
        "const r=ExtensionPushSchema.safeParse(JSON.parse(s));"
        "if(!r.success){console.error(JSON.stringify(r.error.issues));process.exit(1)}})"
    )
    proc = subprocess.run([node, "--input-type=module", "-e", script], input=json.dumps(body),
                          capture_output=True, text=True, check=False)
    assert proc.returncode == 0, proc.stderr


def validate_policy_with_models(body: dict[str, Any]) -> None:
    node = shutil.which("node")
    dist = MODELS / "dist" / "index.js"
    if node is None or not dist.exists():
        assert set(body) == {"jobs", "extensions", "upgrades", "gc"}
        return
    script = (
        f"import {{ PolicySchema }} from {json.dumps(str(dist))};"
        "let s='';process.stdin.on('data',d=>s+=d).on('end',()=>{"
        "const r=PolicySchema.safeParse(JSON.parse(s));"
        "if(!r.success){console.error(JSON.stringify(r.error.issues));process.exit(1)}})"
    )
    proc = subprocess.run([node, "--input-type=module", "-e", script], input=json.dumps(body),
                          capture_output=True, text=True, check=False)
    assert proc.returncode == 0, proc.stderr


class TestPush:
    def test_a_draft_push_round_trip(self, fake, package):
        out = call("extension", "push", {"dir": str(package), "draft": True})
        (path, data), = fake.stored.items()
        assert path.startswith("alice/lab/extensions/count-things/sdist/")
        with tarfile.open(fileobj=__import__("io").BytesIO(data)) as tar:
            names = tar.getnames()
        assert any(n.endswith("count_things/ops/records/count.py") for n in names)
        put = [c for c in fake.calls if c[0] == "PUT" and c[1].startswith("/extensions/")]
        assert [c[1] for c in put] == ["/extensions/alice/lab/count-things"]
        body = put[0][3]
        assert body["package"]["sdist"] == out["sdist"]["ref"]
        assert [o["path"] for o in body["provides"]["ops"]] == ["alice/lab/ops/records/count"]
        assert not {"state", "visibility", "party", "flags", "conformance"} & set(body)
        validate_with_models(body)
        assert out["pin"] == f"alice/lab/extensions/count-things@{HASH}"
        assert out["version"] == 1 and "check" not in out
        assert "shown to no one else" in out["consent"]
        assert not any(c[1].endswith("/check") for c in fake.calls)

    def test_push_without_draft_queues_the_checks(self, fake, package):
        fake.existing = True
        out = call("extension", "push", {"dir": str(package)})
        assert fake.calls[-1][:2] == ("POST", "/extensions/alice/lab/extensions/count-things@1/check")
        assert out["check"] == {"state": "pending", "job": "j_9",
                                 "waitingFor": ["no runner of yours is connected"]}

    def test_the_consent_line_follows_the_visibility(self):
        from mechbench_runner.verbs.extension import push_consent

        assert "shown to no one else" in push_consent("public", True)
        assert "shown to you only" in push_consent(None, False)
        assert "shown to everyone (public)" in push_consent("public", False)
        assert "org's members" in push_consent("org", False)


class TestExtensionVerbs:
    def test_versions_and_their_routes(self, fake):
        fake.existing = True
        call("extension", "read", {"address": "alice/lab/extensions/interp-extras@2"})
        assert fake.calls[-1][1] == "/extensions/alice/lab/extensions/interp-extras@2"
        out = call("extension", "history", {"address": "alice/lab/interp-extras@2"})
        assert fake.calls[-1][1] == "/extensions/alice/lab/extensions/interp-extras"
        assert out["versions"][0]["version"] == 1
        call("extension", "withdraw", {"address": "alice/lab/extensions/x@3", "reason": "broken"})
        assert fake.calls[-1][1:] == ("/extensions/alice/lab/extensions/x@3/withdraw", {}, {"reason": "broken"})
        call("extension", "visibility", {"address": "alice/lab/extensions/x@3", "visibility": "public"})
        assert fake.calls[-1][3] == {"visibility": "public"}
        with pytest.raises(VerbError, match="names no version"):
            call("extension", "check", {"address": "alice/lab/extensions/x"})

    def test_check_queues_the_checks(self, fake):
        call("extension", "check", {"address": "alice/lab/extensions/x@3"})
        assert fake.calls[-1][:2] == ("POST", "/extensions/alice/lab/extensions/x@3/check")

    def test_verify_is_the_site_admin_s(self, fake):
        call("extension", "verify", {"address": "alice/lab/extensions/x@3"})
        assert fake.calls[-1][1:] == ("/extensions/alice/lab/extensions/x@3/verify", {}, {})
        call("extension", "verify", {"address": "alice/lab/extensions/x@3", "override": "read it"})
        assert fake.calls[-1][3] == {"override": "read it"}

    def test_approve_and_revoke_name_the_org_by_its_id(self, fake):
        call("extension", "approve", {"address": "alice/lab/extensions/x@3", "org": "@acme",
                                      "note": "read by Jo", "override": "the flag is a test"})
        assert fake.calls[-1][1:] == ("/extensions/alice/lab/extensions/x@3/approve", {}, {
            "org": "org_acme", "note": "read by Jo", "override": "the flag is a test"})
        call("extension", "revoke", {"address": "alice/lab/extensions/x@3", "org": "acme"})
        assert fake.calls[-1][1:] == ("/extensions/alice/lab/extensions/x@3/revoke", {},
                                      {"org": "org_acme"})
        with pytest.raises(VerbError, match="no org @nope"):
            call("extension", "approve", {"address": "alice/lab/extensions/x@3", "org": "nope"})

    def test_review_for_an_org_or_the_platform(self, fake):
        call("extension", "review", {"address": "alice/lab/extensions/x@3", "org": "acme"})
        assert fake.calls[-1][1:] == ("/extensions/alice/lab/extensions/x@3/review", {},
                                      {"org": "org_acme"})
        call("extension", "review", {"address": "alice/lab/extensions/x@3"})
        assert fake.calls[-1][3] == {}

    def test_read_and_list_show_approvals(self, fake, monkeypatch):
        fake.existing = True
        out = call("extension", "read", {"address": "alice/lab/extensions/count-things"})
        assert out["approved"] == "org_acme"
        assert out["approvals"][0]["by"] == "u_jo"
        monkeypatch.setattr(fake, "api", lambda _c, m, r, **k: (
            {"extensions": [{"address": "a/b/extensions/c", "approvedBy": []},
                            {"address": "a/b/extensions/d",
                             "approvals": [{"org": "org_acme"}, {"org": "org_b"}]}]}, {}))
        rows = call("extension", "list", {})
        rows = rows["items"] if isinstance(rows, dict) else rows
        assert [r["approved"] for r in rows] == ["no org", "org_acme, org_b"]

    def test_list_filters(self, fake):
        call("extension", "list", {"owner": "alice", "state": "draft", "search": "ext"})
        assert fake.calls[-1][2] == {"owner": "alice", "state": "draft", "q": "ext"}


class TestPolicy:
    def test_create_by_option_names(self, fake):
        call("policy", "create", {"name": "lab", "serve": "me and my org",
                                  "admit": "mechbench-verified"})
        body = fake.calls[-1][3]["body"]
        assert body["jobs"] == {"serve": ["own", "org"], "allow": []}
        assert body["extensions"] == {"admit": ["own", "verified"], "allow": [],
                                      "network": "none"}
        validate_policy_with_models(body)

    def test_an_org_policy_starts_from_the_org_default(self, fake):
        call("policy", "create", {"name": "gpu", "org_id": "org_acme"})
        body = fake.calls[-1][3]["body"]
        assert body["jobs"]["serve"] == ["own"]
        assert body["extensions"]["admit"] == ["own", "approved"]
        assert fake.calls[-1][3]["orgId"] == "org_acme"

    def test_an_option_offered_on_the_other_machine_is_refused(self, fake):
        with pytest.raises(VerbError, match="not offered on an org's machine"):
            call("policy", "create", {"name": "gpu", "org_id": "org_acme",
                                      "serve": "me and my org"})

    def test_sources_and_allow_lists(self, fake):
        call("policy", "create", {
            "name": "custom", "serve": "own,listed", "admit": "own, listed",
            "serve_allow": ["project=prj_1", "org=org_acme"],
            "admit_allow": ["owner=u_bob,extension=bob/tools/extensions/x"],
            "network": "declared", "upgrades": "hold", "unused_days": 7})
        body = fake.calls[-1][3]["body"]
        assert body["jobs"] == {"serve": ["own", "listed"],
                                "allow": [{"project": "prj_1"}, {"org": "org_acme"}]}
        assert body["extensions"] == {
            "admit": ["own", "listed"], "network": "declared",
            "allow": [{"owner": "u_bob", "extension": "bob/tools/extensions/x"}]}
        assert body["upgrades"] == {"compute": "hold"} and body["gc"] == {"unused_days": 7}
        validate_policy_with_models(body)

    @pytest.mark.parametrize("args,match", [
        ({"serve": "own,pool"}, "neither an option"),
        ({"admit": "own,own"}, "twice"),
        ({"admit_allow": ["project=prj_1"]}, "each part is"),
    ])
    def test_bad_words_are_refused(self, fake, args, match):
        with pytest.raises(VerbError, match=match):
            call("policy", "create", {"name": "x", **args})

    def test_update_changes_one_axis_of_the_current_version(self, fake):
        call("policy", "update", {"id": "pol_1", "admit": "mine and my org's approved",
                                  "yes": True})
        body = fake.calls[-1][3]["body"]
        assert body["jobs"]["serve"] == ["own"]
        assert body["extensions"]["admit"] == ["own", "approved"]

    def test_read_shows_the_options_or_custom(self, fake):
        out = call("policy", "read", {"id": "pol_1"})
        assert out["shown"]["serve"]["option"] == "me only"
        assert out["shown"]["admit"]["option"] == "mine"
        from mechbench_runner.verbs.policy import shown

        custom = shown({"id": "pol_x", "orgId": "org_acme", "body": {
            "jobs": {"serve": ["own", "listed"], "allow": [{"project": "prj_1"}]},
            "extensions": {"admit": ["own", "org"], "allow": [], "network": "none"},
            "upgrades": {"compute": "auto"}, "gc": {"unused_days": 30}}})
        assert custom["serve"]["option"] == "Custom"
        assert custom["serve"]["sources"] == ["own", "listed"]
        assert custom["serve"]["means"][1] == "Jobs on the projects, owners or orgs listed."
        assert custom["admit"]["option"] == "Custom"
        assert custom["machines"] == "an org's machine"

    def test_update_names_the_runners_it_reaches_and_waits_for_yes(self, fake):
        out = call("policy", "update", {"id": "pol_1", "upgrades": "hold"})
        assert out["updated"] is False
        assert out["consent"] == "a new version of pol_1 reaches 1 runner: studio (rnr_1)"
        assert not any(c[0] == "PUT" for c in fake.calls)
        out = call("policy", "update", {"id": "pol_1", "upgrades": "hold", "yes": True})
        assert fake.calls[-1][:2] == ("PUT", "/policies/pol_1") and out["updated"] is True
        assert fake.calls[-1][3]["body"]["upgrades"] == {"compute": "hold"}

    def test_apply_names_the_runner(self, fake):
        out = call("policy", "apply", {"runner": "rnr_2", "policy": "pol_1"})
        assert out["consent"] == "putting it under pol_1 reaches 1 runner: laptop (rnr_2)"
        call("policy", "apply", {"runner": "rnr_2", "policy": "pol_1", "yes": True})
        assert fake.calls[-1][1:] == ("/runners/rnr_2/policy", {}, {"policyId": "pol_1"})

    def test_create_takes_a_file_or_json(self, fake, tmp_path):
        f = tmp_path / "p.json"
        body = {"jobs": {"serve": ["own"], "allow": []},
                "extensions": {"admit": ["own", "verified"], "allow": [], "network": "none"},
                "upgrades": {"compute": "auto"}, "gc": {"unused_days": 30}}
        f.write_text(json.dumps(body))
        call("policy", "create", {"name": "open", "body": str(f)})
        assert fake.calls[-1][3] == {"name": "open", "body": body}
        call("policy", "create", {"name": "open", "body": json.dumps(body), "serve": "nobody (paused)"})
        assert fake.calls[-1][3]["body"]["jobs"]["serve"] == []
        with pytest.raises(VerbError, match="JSON"):
            call("policy", "create", {"name": "x", "body": "not json"})
        with pytest.raises(VerbError, match="current shape"):
            call("policy", "create", {"name": "x", "body": '{"extensions": {"install": "mine"}}'})

    def test_list_searches_here(self, fake):
        out = call("policy", "list", {"search": "pers"})
        assert [p["id"] for p in out["items"]] == ["pol_personal"]


class TestConsent:
    @pytest.mark.parametrize("verb", ["new", "test", "list", "read", "history"])
    def test_extension_reads_are_free(self, verb):
        assert not noun("extension").verb(verb).needs_consent({})

    def test_push_and_withdraw_always_ask(self):
        assert noun("extension").verb("push").needs_consent({"draft": True})
        assert noun("extension").verb("withdraw").needs_consent({})

    def test_visibility_asks_when_it_widens(self):
        v = noun("extension").verb("visibility")
        assert v.needs_consent({"visibility": "public"})
        assert v.needs_consent({"visibility": "org"})
        assert not v.needs_consent({"visibility": "private"})

    def test_a_policy_change_asks_when_it_reaches_machines(self):
        for verb in ("update", "apply"):
            v = noun("policy").verb(verb)
            assert not v.needs_consent({})
            assert v.needs_consent({"yes": True})
        assert not noun("policy").verb("create").needs_consent({})

    def test_ops_are_reads(self):
        for v in noun("op").verbs:
            assert v.effect == "read"
