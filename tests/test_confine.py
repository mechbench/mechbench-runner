from __future__ import annotations

import hashlib

import pytest

pytest.importorskip("mechbench_compute")

from mechbench_runner import job_runner as jr  # noqa: E402
from mechbench_runner import paths  # noqa: E402
from mechbench_runner.confine import (  # noqa: E402
    MARKER,
    PathRefusedError,
    remove_owned,
    under,
)
from mechbench_runner.extensions import InstallError, digest_of  # noqa: E402
from mechbench_runner.spool import JobSpool, job_dir, make_job_dir  # noqa: E402
from mechbench_runner.verification import StageError, Verification  # noqa: E402

CRAFTED = ["../x", "/etc/passwd", "a/../../b", "", "." * 64, "..", "a" * 65, None]


def _tree() -> list[str]:
    home = paths.mechbench_dir()
    return sorted(str(p.relative_to(home)) for p in home.rglob("*"))


@pytest.fixture
def spool():
    return paths.spool_dir()


class TestCraftedIds:
    @pytest.mark.parametrize("bad", CRAFTED)
    def test_a_crafted_job_id_is_refused_before_any_change(self, spool, bad):
        before = _tree()
        with pytest.raises(PathRefusedError, match="job id"):
            JobSpool(bad)
        with pytest.raises(PathRefusedError, match="job id"):
            jr._spool_result(bad, b"\xa0", "00", "tok")
        with pytest.raises(PathRefusedError, match="job id"):
            jr._check_claim({"id": bad, "spec": {}})
        jr._clear_spool(bad)
        assert _tree() == before

    @pytest.mark.parametrize("bad", CRAFTED)
    def test_a_crafted_node_id_is_refused_before_any_change(self, spool, bad):
        sp = JobSpool("j_ok")
        before = _tree()
        for call in (lambda: sp.node_start(bad, "fp"),
                     lambda: sp.item(bad, "k", {"v": 1}),
                     lambda: sp.node_done(bad, "p", "fp"),
                     lambda: sp.node_kept(bad, "fp", {"v": 1}),
                     lambda: sp.checkpoint(bad, {})):
            with pytest.raises(PathRefusedError, match="node id") as got:
                call()
            assert repr(bad) in str(got.value)
        assert _tree() == before

    @pytest.mark.parametrize("bad", CRAFTED)
    def test_a_claim_with_a_crafted_node_id_is_refused(self, bad):
        job = {"id": "j_ok", "spec": {"graph": {"nodes": [
            {"id": "gen", "block": "text/generate"},
            {"id": bad, "block": "text/generate"},
        ]}}}
        with pytest.raises(PathRefusedError, match="node id") as got:
            jr._check_claim(job)
        assert repr(bad) in str(got.value)

    @pytest.mark.parametrize("bad", ["../../../outside", "u_a/../../x", "/etc/passwd",
                                     "u_a/./p", "u_a//p", "u_a/p\0x", "solo"])
    def test_a_crafted_object_path_is_refused_before_it_is_fetched(self, tmp_path, bad):
        fetched: list[str] = []

        class Api:
            def fetch_object(self, where):
                fetched.append(where)
                return b"{}"

        manifest = {"provides": {"ops": [{"example_inputs": {
            "ok": {"$ref": {"bench": "u_a/p/records"}},
            "bad": {"$ref": {"bench": bad}},
        }}]}}
        root = tmp_path / "inputs"
        with pytest.raises(StageError, match="not a bench path"):
            Verification()._inputs(Api(), root, manifest)
        assert fetched == []
        assert list(root.rglob("*")) == []
        assert not (tmp_path.parent / "outside.json").exists()

    def test_a_package_reference_that_is_not_hex_is_refused(self):
        with pytest.raises(InstallError):
            digest_of("sha256:" + "../" * 21 + "x")

    def test_under_refuses_absolute_parts_and_the_root_itself(self, tmp_path):
        with pytest.raises(PathRefusedError):
            under(tmp_path, "/etc", what="x", ident="x")
        with pytest.raises(PathRefusedError):
            under(tmp_path, ".", what="x", ident="x")
        assert under(tmp_path, "a", "b", what="x", ident="x") == (
            tmp_path.resolve() / "a" / "b")


class TestHappyPath:
    def test_directories_are_named_by_the_ids_hash_and_marked(self, spool):
        sp = JobSpool("j_happy")
        sp.node_start("gen-1", "fp")
        sp.item("gen-1", "k", {"v": 1})
        name = hashlib.sha256(b"j_happy").hexdigest()[:32]
        assert sp.root == spool.resolve() / name
        assert (sp.root / MARKER).read_text() == "j_happy"
        node = sp.node_dir("gen-1")
        assert node.name == hashlib.sha256(b"gen-1").hexdigest()[:32]
        assert (node / MARKER).read_text() == "gen-1"
        assert sp.resume_map() == {
            "gen-1": {"fingerprint": "fp", "items": {"k": {"v": 1}}}}
        assert [p.name for p in spool.iterdir()] == [name]

    def test_a_spooled_result_is_found_by_its_job_id(self):
        jr._spool_result("j_r", b"\xa0", "00")
        assert jr._spooled_job_ids() == ["j_r"]
        jr._clear_spool("j_r")
        assert jr._spooled_job_ids() == []
        assert not job_dir("j_r").exists()

    def test_a_result_spooled_under_the_old_layout_is_adopted(self, spool):
        old = spool / "j_old"
        old.mkdir()
        (old / "result.cbor").write_bytes(b"\xa0")
        (old / "result.sha256").write_text("00")
        assert jr._spooled_job_ids() == ["j_old"]
        assert not old.exists()
        assert jr._spooled_result("j_old") == (b"\xa0", "00")


class TestDeletesAreLimitedToOwnedDirectories:
    def test_an_unmarked_directory_is_not_deleted(self, spool):
        d = job_dir("j_x")
        d.mkdir()
        (d / "keep").write_text("mine")
        jr._clear_spool("j_x")
        assert (d / "keep").read_text() == "mine"
        with pytest.raises(PathRefusedError, match="did not create"):
            JobSpool("j_x").clear()
        with pytest.raises(PathRefusedError, match="did not create"):
            jr._spool_result("j_x", b"\xa0", "00")
        assert sorted(p.name for p in d.iterdir()) == ["keep"]

    def test_a_symlinked_node_directory_is_not_followed(self, spool, tmp_path):
        outside = tmp_path / "victim"
        (outside / "items").mkdir(parents=True)
        (outside / "items" / "precious").write_text("x")
        sp = JobSpool("j_link")
        make_job_dir("j_link")
        sp.node_dir("gen").symlink_to(outside, target_is_directory=True)
        with pytest.raises(PathRefusedError):
            sp.node_start("gen", "fp2")
        assert (outside / "items" / "precious").read_text() == "x"
        assert not (outside / "fingerprint").exists()

    def test_a_symlinked_subdirectory_is_not_deleted_through(self, spool, tmp_path):
        outside = tmp_path / "victim"
        outside.mkdir()
        (outside / "precious").write_text("x")
        sp = JobSpool("j_sub")
        sp.node_start("gen", "fp1")
        (sp.node_dir("gen") / "items").symlink_to(outside, target_is_directory=True)
        with pytest.raises(PathRefusedError):
            sp.node_start("gen", "fp2")
        assert (outside / "precious").read_text() == "x"

    def test_remove_owned_does_nothing_to_a_missing_directory(self, spool):
        remove_owned(spool / "absent")
