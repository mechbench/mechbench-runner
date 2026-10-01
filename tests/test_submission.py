from __future__ import annotations

import hashlib
import io
import json
import tarfile
from typing import Any

import pytest

from mechbench import cli
from mechbench_runner.config import Config
from mechbench_runner.verbs import Ctx, VerbError, invoke, noun

CFG = Config(api_base_url="http://api.test", api_key="mbk_test",
             poll_interval_seconds=0.01, warm_model_id=None, runner_id=None)
ADDR = "alice/lab/extensions/x@3"
ROUTE = "/extensions/alice/lab/extensions/x@3"


def tarball(*members: tarfile.TarInfo | tuple[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as t:
        for m in members:
            if isinstance(m, tarfile.TarInfo):
                t.addfile(m)
            else:
                info = tarfile.TarInfo(m[0])
                info.size = len(m[1])
                t.addfile(info, io.BytesIO(m[1]))
    return buf.getvalue()


def dir_member(name: str) -> tarfile.TarInfo:
    info = tarfile.TarInfo(name)
    info.type = tarfile.DIRTYPE
    return info


GOOD = tarball(dir_member("x-0.1.0"), ("x-0.1.0/pyproject.toml", b"[project]\n"),
               ("x-0.1.0/x/__init__.py", b"MANIFEST = None\n"))


class Fake:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict, Any]] = []
        self.blob = GOOD
        self.served: bytes | None = None
        self.submission: dict | None = None

    def ref(self) -> str:
        return f"~hash/sha256:{hashlib.sha256(self.blob).hexdigest()}"

    def api(self, method, route, *, query=None, body=None):
        self.calls.append((method, route, {k: v for k, v in (query or {}).items() if v is not None}, body))
        if method == "GET":
            return {"address": "alice/lab/extensions/x", "version": 3, "hash": "sha256:" + "cd" * 32,
                    "manifest": {"package": {"name": "x", "python": ">=3.12", "sdist": self.ref()}},
                    "versions": [], "usage": {}, "submission": self.submission}, {}
        return {"ok": True}, {}

    def fetch_by_hash(self, ref):
        self.calls.append(("GET", f"/objects/{ref}", {}, None))
        return self.blob if self.served is None else self.served


@pytest.fixture
def fake(monkeypatch):
    f = Fake()
    monkeypatch.setattr(Ctx, "api", lambda self, m, r, **k: f.api(m, r, **k))
    monkeypatch.setattr(Ctx, "fetch_by_hash", lambda self, ref: f.fetch_by_hash(ref))
    monkeypatch.setattr(Config, "from_env", classmethod(lambda cls: CFG))
    return f


def call(verb: str, args: dict) -> Any:
    return invoke(Ctx(CFG), "extension", verb, args)


def test_submit_flag_and_reject_post_their_bodies(fake):
    call("submit", {"address": ADDR})
    assert fake.calls[-1] == ("POST", f"{ROUTE}/submit", {}, {})
    call("flag", {"address": ADDR, "code": "net-call", "severity": "blocker",
                  "summary": "Opens a socket.", "where": "x/net.py:42-44", "details": "At import."})
    assert fake.calls[-1] == ("POST", f"{ROUTE}/flags", {}, {
        "code": "net-call", "severity": "blocker", "summary": "Opens a socket.", "source": "review",
        "where": "x/net.py:42-44", "details": "At import."})
    call("reject", {"address": ADDR, "reason": "Drop the socket."})
    assert fake.calls[-1] == ("POST", f"{ROUTE}/reject", {}, {"reason": "Drop the socket."})
    with pytest.raises(VerbError, match="names no version"):
        call("submit", {"address": "alice/lab/extensions/x"})


@pytest.mark.parametrize("where", ["x/net.py", "/etc/passwd:1", "x.py:0", "x.py:4-", " x.py:3"])
def test_a_flag_s_where_is_path_and_line(fake, where):
    with pytest.raises(VerbError, match="path:line"):
        call("flag", {"address": ADDR, "code": "c", "severity": "notice", "summary": "s", "where": where})
    assert not fake.calls


def test_the_cli_spells_the_flags(fake):
    assert cli.main(["extension", "flag", ADDR, "--code", "net-call", "--severity", "warning",
                     "--summary", "S.", "--where", "a.py:1"]) == 0
    assert fake.calls[-1][3]["where"] == "a.py:1"
    assert cli.main(["extension", "reject", ADDR, "--reason", "R."]) == 0


def test_read_says_in_review_since(fake):
    assert "inReview" not in call("read", {"address": ADDR})
    fake.submission = {"id": "cas_9", "status": "open", "openedAt": "2026-09-30T10:00:00Z"}
    out = call("read", {"address": ADDR})
    assert out["inReview"] == "in review since 2026-09-30T10:00:00Z (case cas_9, open)"


def test_fetch_reads_by_hash_verifies_and_unpacks(fake, tmp_path, capsys):
    dest = tmp_path / "review"
    out = call("fetch", {"address": ADDR, "to": str(dest)})
    assert [c[1] for c in fake.calls] == [ROUTE, f"/objects/{fake.ref()}"]
    assert out["sha256"] == hashlib.sha256(GOOD).hexdigest()
    assert out["files"] == ["x-0.1.0/pyproject.toml", "x-0.1.0/x/__init__.py"]
    assert (dest / "x-0.1.0/x/__init__.py").read_bytes() == b"MANIFEST = None\n"
    assert cli.main(["extension", "fetch", ADDR, "--to", str(tmp_path / "again")]) == 0
    assert json.loads(capsys.readouterr().out)["files"] == out["files"]


def test_fetch_refuses_bytes_that_are_not_the_hash_and_writes_nothing(fake, tmp_path):
    fake.served = GOOD + b"tampered"
    with pytest.raises(VerbError, match="refused it and wrote nothing"):
        call("fetch", {"address": ADDR, "to": str(tmp_path / "r")})
    assert not (tmp_path / "r").exists()


def link(name: str, target: str, kind: bytes) -> tarfile.TarInfo:
    info = tarfile.TarInfo(name)
    info.type = kind
    info.linkname = target
    return info


@pytest.mark.parametrize("bad", [
    ("../escape.py", b"x"),
    ("/abs/path.py", b"x"),
    ("x-0.1.0/../../escape.py", b"x"),
    link("x-0.1.0/evil", "/etc/passwd", tarfile.SYMTYPE),
    link("x-0.1.0/hard", "x-0.1.0/pyproject.toml", tarfile.LNKTYPE),
])
def test_fetch_refuses_a_member_that_escapes_or_links(fake, tmp_path, bad):
    fake.blob = tarball(("x-0.1.0/ok.py", b"ok"), bad)
    with pytest.raises(VerbError, match="refused the sdist"):
        call("fetch", {"address": ADDR, "to": str(tmp_path / "r")})
    assert not (tmp_path / "r").exists()
    assert not (tmp_path / "escape.py").exists()


def test_fetch_wants_an_empty_directory(fake, tmp_path):
    (tmp_path / "full").mkdir()
    (tmp_path / "full" / "a").write_text("a")
    with pytest.raises(VerbError, match="not an empty directory"):
        call("fetch", {"address": ADDR, "to": str(tmp_path / "full")})
    (tmp_path / "empty").mkdir()
    assert call("fetch", {"address": ADDR, "to": str(tmp_path / "empty")})["files"]


def test_the_new_verbs_effects():
    n = noun("extension")
    assert {v: n.verb(v).effect for v in ("submit", "fetch", "flag", "reject")} == {
        "submit": "draft", "fetch": "read", "flag": "draft", "reject": "draft"}
    assert n.verb("fetch").local
