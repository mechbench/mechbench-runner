from __future__ import annotations

import dataclasses
import hashlib
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("mechbench_compute")

from mechbench_compute import bench  # noqa: E402
from mechbench_compute.lexicon._base import Output  # noqa: E402
from mechbench_compute.registry import REGISTRY  # noqa: E402

from mechbench_runner import job_runner as jr  # noqa: E402
from mechbench_runner.config import Config  # noqa: E402
from mechbench_runner.spool import JobSpool  # noqa: E402

RESULTS = "u/p/results/j_r"
RECORDS = [{"id": "b", "kind": "records/record", "n": 2},
           {"id": "a", "kind": "records/record", "n": 1}]


class Api:
    def __init__(self):
        self.progress: list[dict] = []
        self.completed: list[str] = []

    def report_progress(self, job_id, num, den, *, unit=None, status=None,
                        node=None, resumed_from=None, **kw):
        self.progress.append({"status": status, "resumed_from": resumed_from})

    def complete_job_cbor(self, job_id, cbor_bytes, content_hash):
        self.completed.append(job_id)

    def fail_job(self, *a, **k):
        raise AssertionError("the job must not fail")

    def declare_preparing(self, *a, **k):
        return None

    def report_preparing_step(self, *a, **k):
        return None


@pytest.fixture
def store(monkeypatch):
    objects: dict[str, object] = {}
    files: dict[str, bytes] = {}

    def emit(path, payload, **kw):
        objects[path] = payload
        return {"path": path}

    def fetch(path, **kw):
        if path not in objects:
            raise bench.BenchError(f"no object at {path}")
        got = {"payload": objects[path]}
        return (got, {"content_hash": ""}) if kw.get("with_meta") else got

    def put_file(label, path, **kw):
        files[label] = Path(path).read_bytes()
        return {"sizeBytes": len(files[label])}

    def get_file_chunks(label, **kw):
        yield files[label]

    def list_prefix_hashes(prefix, **kw):
        return {k[len(prefix) + 1:]: hashlib.sha256(v).hexdigest()
                for k, v in files.items() if k.startswith(prefix + "/")}

    for name, fn in (("emit", emit), ("fetch", fetch), ("put_file", put_file),
                     ("get_file_chunks", get_file_chunks),
                     ("list_prefix_hashes", list_prefix_hashes)):
        monkeypatch.setattr(bench, name, fn)
    return SimpleNamespace(objects=objects, files=files)


@pytest.fixture
def made(monkeypatch):
    runs = {"make": 0}
    held = REGISTRY.find("records/filter")

    def run(_ctx, inputs, _params):
        runs["make"] += 1
        items = [{**r, "data": bytes([65 + i]) * 3000} for i, r in enumerate(RECORDS)]
        collection = {"kind": "collection", "item_kind": "records/record",
                      "key": ["id"], "items": items}
        return {"out": collection, "extra": collection}

    fake = dataclasses.replace(
        held,
        op=dataclasses.replace(held.op, outputs={"extra": Output("records/record",
                                                                 collection=True)}),
        module=SimpleNamespace(run=run, OP=held.module.OP))
    monkeypatch.setitem(REGISTRY.table().ops, "records/filter", fake)
    return runs


def _graph(port):
    return {"dataflow": 2,
            "nodes": [{"id": "make", "block": "records/filter",
                       "params": {"where": "true"}, "inputs": {"records": RECORDS}},
                      {"id": "after", "block": "records/sort",
                       "params": {"by": ["id"]}}],
            "edges": [{"from": {"node": "make", **({"output": port} if port else {})},
                       "to": {"node": "after", "port": "records"}}]}


def _job(job_id, graph, resume):
    return {"id": job_id, "protocolKind": "pipeline", "resultPath": RESULTS,
            "resume": resume, "resumeCount": 1 if resume else 0,
            "spec": {"graph": graph,
                     "outputs": [{"name": "after", "from": {"node": "after"}}]}}


def _runner(monkeypatch):
    class StubControl:
        def __init__(self, _state, path=None):
            self.path = path or "/tmp/stub.sock"

        def start(self):
            return None

        def stop(self):
            return None

    monkeypatch.setattr(jr, "ControlServer", StubControl)
    monkeypatch.setattr(jr.shutil, "which", lambda _name: None)
    runner = jr.JobRunner(Config(api_base_url="http://127.0.0.1:1", api_key="k",
                                 poll_interval_seconds=0.01, warm_model_id=None,
                                 runner_id="r_mine"))
    monkeypatch.setattr(runner._executor, "_model_loaded", lambda *a, **k: None)
    return runner


def _kill_in(monkeypatch, block):
    resolved = REGISTRY.find(block)
    real = resolved.module.run

    def killed(*_a, **_k):
        raise KeyboardInterrupt("the runner was killed")

    monkeypatch.setattr(resolved.module, "run", killed)
    return lambda: monkeypatch.setattr(resolved.module, "run", real)


@pytest.fixture(params=["with mlx", "without mlx"])
def platform(request, monkeypatch):
    if request.param == "without mlx":
        for name in [m for m in sys.modules if m == "mlx" or m.startswith("mlx.")]:
            monkeypatch.delitem(sys.modules, name)
        monkeypatch.setitem(sys.modules, "mlx", None)
        monkeypatch.setitem(sys.modules, "mlx.core", None)
    return request.param


@pytest.mark.parametrize("port, limit", [
    ("extra", None),
    (None, 4096),
    ("extra", 4096),
], ids=["named output", "stored as files", "named output stored as files"])
def test_a_done_node_is_restored_after_a_restart(store, made, platform, port, limit,
                                                  monkeypatch, capsys):
    if limit is not None:
        monkeypatch.setattr(bench, "MAX_OBJECT_BYTES", limit)
    job_id = "j_restart"
    revive = _kill_in(monkeypatch, "records/sort")
    with pytest.raises(KeyboardInterrupt):
        _runner(monkeypatch)._handle(Api(), _job(job_id, _graph(port), resume=False))
    assert made["make"] == 1
    entry = JobSpool(job_id).resume_map()["make"]
    assert entry["done"] == f"{RESULTS}/nodes/make"
    if limit is not None:
        assert store.files

    revive()
    api = Api()
    _runner(monkeypatch)._handle(api, _job(job_id, _graph(port), resume=True))
    assert made["make"] == 1, "the done node was recomputed"
    assert api.completed == [job_id]
    assert [i["id"] for i in store.objects[f"{RESULTS}/after"]["items"]] == ["a", "b"]
    reported = [p["resumed_from"] for p in api.progress if p["resumed_from"]]
    assert reported[0] == {"node": "make", "reused": 1}
    assert "resuming from spool" in capsys.readouterr().out


def test_a_resume_that_finds_nothing_says_why_and_reports_no_reuse(
        store, made, monkeypatch, capsys):
    api = Api()
    _runner(monkeypatch)._handle(api, _job("j_fresh", _graph(None), resume=True))
    assert made["make"] == 1
    assert all(p["resumed_from"] is None for p in api.progress)
    out = capsys.readouterr().out
    assert "j_fresh: nothing to resume from" in out
    assert "no spool for this job on this machine" in out


def test_a_spool_that_cannot_be_read_is_logged_not_swallowed(
        store, made, monkeypatch, capsys):
    def broken(self):
        raise OSError("disk on fire")

    monkeypatch.setattr(JobSpool, "resume_map", broken)
    api = Api()
    _runner(monkeypatch)._handle(api, _job("j_broken", _graph(None), resume=True))
    assert all(p["resumed_from"] is None for p in api.progress)
    out = capsys.readouterr().out
    assert "j_broken: the spool could not be read (OSError: disk on fire)" in out
