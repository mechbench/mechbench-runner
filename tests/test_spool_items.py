"""The item / checkpoint spool and resume on re-claim (epic 000320,
task 000323 second half). The executor decides what a partial is
worth; this side only has to keep it faithfully and hand it back."""

from __future__ import annotations

import numpy as np
import pytest

pytest.importorskip("mechbench_compute")

from mechbench_runner import job_runner as jr  # noqa: E402
from mechbench_runner.config import Config  # noqa: E402
from mechbench_runner.spool import JobSpool  # noqa: E402


def _runner(monkeypatch, runner_id="r_mine"):
    class StubControl:
        def __init__(self, _state, path=None):
            self.path = path or "/tmp/stub.sock"

        def start(self):
            return None

        def stop(self):
            return None

    monkeypatch.setattr(jr, "ControlServer", StubControl)
    # No real caffeinate in tests.
    monkeypatch.setattr(jr.shutil, "which", lambda _name: None)
    config = Config(
        api_base_url="http://127.0.0.1:1", api_key="k",
        poll_interval_seconds=0.01, warm_model_id=None, runner_id=runner_id,
    )
    return jr.JobRunner(config)


class TestJobSpool:
    def test_items_round_trip_under_a_fingerprint(self):
        sp = JobSpool("j_a")
        sp.node_start("gen", "sha256:fp1")
        sp.item("gen", "flash:0", {"id": "flash-s0", "text": "once"})
        sp.item("gen", "flash:1", {"id": "flash-s1", "text": "twice"})
        m = sp.resume_map()
        assert m == {"gen": {"fingerprint": "sha256:fp1",
                             "items": {"flash:0": {"id": "flash-s0", "text": "once"},
                                       "flash:1": {"id": "flash-s1", "text": "twice"}}}}
        assert sp.summary() == {"node": "gen", "reused": 2}

    def test_rewriting_a_key_is_idempotent(self):
        sp = JobSpool("j_b")
        sp.node_start("gen", "fp")
        sp.item("gen", "k", {"v": 1})
        sp.item("gen", "k", {"v": 1})
        assert len(sp.resume_map()["gen"]["items"]) == 1

    def test_a_changed_fingerprint_discards_the_nodes_partials(self):
        sp = JobSpool("j_c")
        sp.node_start("gen", "fp1")
        sp.item("gen", "k", {"v": 1})
        sp.node_start("gen", "fp2")  # the process changed
        assert sp.resume_map() == {}  # fingerprint alone is not an entry

    def test_a_done_node_wins_over_its_items(self):
        sp = JobSpool("j_d")
        sp.node_start("gen", "fp")
        sp.item("gen", "k", {"v": 1})
        sp.node_done("gen", "u/p/results/j_d/gen", "fp")
        assert sp.resume_map() == {"gen": {"fingerprint": "fp",
                                           "done": "u/p/results/j_d/gen"}}
        assert sp.summary()["reused"] == 1

    def test_a_torn_item_is_not_an_item(self):
        sp = JobSpool("j_e")
        sp.node_start("gen", "fp")
        sp.item("gen", "k", {"v": 1})
        (sp.root / "gen" / "items" / "torn.cbor").write_bytes(b"\xff\x00")
        assert list(sp.resume_map()["gen"]["items"]) == ["k"]

    def test_checkpoint_round_trip(self):
        sp = JobSpool("j_f")
        sp.node_start("train", "fp")
        rng = np.random.default_rng(5)
        rng.random()
        state = {
            "step": 50,
            "weights": {"model.layers.0.self_attn.q_proj.lora_a":
                        np.ones((2, 4), np.float32),
                        "model.layers.0.self_attn.q_proj.lora_b":
                        np.zeros((4, 2), np.float32)},
            "opt_state": {"step": np.array(50, dtype=np.int32),
                          "learning_rate": 1e-4,
                          "model.layers.0.self_attn.q_proj.lora_a.m":
                          np.full((2, 4), 0.5, np.float32)},
            "np_rng": rng.bit_generator.state,
            "mx_key": None,
        }
        sp.checkpoint("train", state)
        back = sp.resume_map()["train"]["checkpoint"]
        assert back["step"] == 50
        assert np.array_equal(back["weights"]["model.layers.0.self_attn.q_proj.lora_a"],
                              state["weights"]["model.layers.0.self_attn.q_proj.lora_a"])
        assert back["opt_state"]["learning_rate"] == 1e-4
        assert int(back["opt_state"]["step"]) == 50
        assert back["np_rng"] == state["np_rng"]  # 128-bit ints intact
        g = np.random.default_rng(0)
        g.bit_generator.state = back["np_rng"]
        assert g.random() == rng.random()
        assert back["mx_key"] is None
        assert sp.summary() == {"node": "train", "reused": 0, "step": 50}

    def test_clear(self):
        sp = JobSpool("j_g")
        sp.node_start("gen", "fp")
        sp.item("gen", "k", {"v": 1})
        sp.clear()
        assert sp.resume_map() == {}


class RecordingApi:
    def __init__(self):
        self.progress: list[dict] = []
        self.completed: list[str] = []
        self.failed: list[str] = []

    def report_progress(self, job_id, num, den, *, unit=None, status=None,
                        node=None, resumed_from=None):
        self.progress.append({"status": status, "resumed_from": resumed_from})

    def complete_job_cbor(self, job_id, cbor_bytes, content_hash):
        self.completed.append(job_id)

    def fail_job(self, job_id, message, timeout=None):
        self.failed.append(job_id)

    def declare_preparing(self, *a, **k):
        return None

    def report_preparing_step(self, *a, **k):
        return None


def _job(job_id, resume=False):
    return {"id": job_id, "protocolKind": "pipeline",
            "resume": resume, "resumeCount": 1 if resume else 0,
            "spec": {"bindings": {"model": "org/m@r"}}}


class TestHandleResumes:
    def test_a_resumed_job_hands_its_spool_to_the_executor(self, monkeypatch):
        runner = _runner(monkeypatch)
        sp = JobSpool("j_1")
        sp.node_start("gen", "fp")
        sp.item("gen", "flash:0", {"id": "flash-s0"})
        seen = {}

        def run(_spec, on_progress=None, secrets=None, resume=None):
            seen["resume"] = resume
            on_progress(1, 1, {"index": 1, "count": 1, "id": "gen"})
            return {"protocol": "pipeline"}

        monkeypatch.setattr(runner._executor, "run", run)
        monkeypatch.setattr(runner._executor, "_model_loaded", lambda *a, **k: None)
        monkeypatch.setattr(jr, "dump_canonical", lambda _p: b"\xa0")
        api = RecordingApi()
        runner._handle(api, _job("j_1", resume=True))
        assert seen["resume"] == {"gen": {"fingerprint": "fp",
                                          "items": {"flash:0": {"id": "flash-s0"}}}}
        first = next(p for p in api.progress if p["resumed_from"] is not None)
        assert first["resumed_from"] == {"node": "gen", "reused": 1}
        assert api.completed == ["j_1"]
        assert sp.resume_map() == {}  # delivered: spool cleared

    def test_a_fresh_job_passes_no_resume_and_spools_as_it_goes(self, monkeypatch):
        runner = _runner(monkeypatch)
        seen = {}

        def run(_spec, on_progress=None, secrets=None, **kw):
            seen.update(kw)
            # the executor's hooks reach the active job's spool
            runner._executor._on_node_start("gen", "fp")
            runner._executor._on_spool_item("gen", "flash:0", {"id": "s0"})
            assert (JobSpool("j_2").resume_map()["gen"]["items"]
                    == {"flash:0": {"id": "s0"}})
            on_progress(1, 1)
            return {"protocol": "pipeline"}

        monkeypatch.setattr(runner._executor, "run", run)
        monkeypatch.setattr(runner._executor, "_model_loaded", lambda *a, **k: None)
        monkeypatch.setattr(jr, "dump_canonical", lambda _p: b"\xa0")
        api = RecordingApi()
        runner._handle(api, _job("j_2"))
        assert "resume" not in seen
        assert runner._spool is None  # released after the job

    def test_a_real_failure_clears_the_partials(self, monkeypatch):
        runner = _runner(monkeypatch)
        sp = JobSpool("j_3")
        sp.node_start("gen", "fp")
        sp.item("gen", "k", {"v": 1})
        api = RecordingApi()
        runner._report_error(api, _job("j_3"), RuntimeError("block raised"))
        assert api.failed == ["j_3"]
        assert sp.resume_map() == {}

    def test_an_interruption_keeps_the_partials(self, monkeypatch):
        runner = _runner(monkeypatch)

        def run(_spec, on_progress=None, secrets=None, **kw):
            runner._executor._on_node_start("gen", "fp")
            runner._executor._on_spool_item("gen", "flash:0", {"id": "s0"})
            raise KeyboardInterrupt("asleep")

        monkeypatch.setattr(runner._executor, "run", run)
        monkeypatch.setattr(runner._executor, "_model_loaded", lambda *a, **k: None)
        with pytest.raises(KeyboardInterrupt):
            runner._handle(RecordingApi(), _job("j_4"))
        assert JobSpool("j_4").resume_map()["gen"]["items"] == {"flash:0": {"id": "s0"}}
