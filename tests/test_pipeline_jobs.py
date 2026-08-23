"""Pipeline jobs name their model in the graph, not in spec.modelId."""
from __future__ import annotations

import pytest

pytest.importorskip("mechbench_compute")
from mechbench_runner import job_runner as jr  # noqa: E402


class TestPipelineModelResolution:
    """Every protocol run queued from the website was rejected by this.

    A flat job carries `spec.modelId`. A pipeline does not: its graph
    names models per node as `params.model`, usually a `$model` hole the
    run bindings fill. Demanding spec.modelId of every kind failed them
    all before the executor ever saw the graph.
    """

    def test_a_binding_is_used_as_the_label(self):
        spec = {"graph": {}, "bindings": {"model": "mlx-community/gemma-4@abc"}}
        assert jr._binding_model(spec) == "mlx-community/gemma-4@abc"  # noqa: SLF001

    def test_no_bindings_is_not_an_error(self):
        assert jr._binding_model({"graph": {}}) is None  # noqa: SLF001
        assert jr._binding_model({"bindings": []}) is None  # noqa: SLF001
        assert jr._binding_model({"bindings": {"other": "x"}}) is None  # noqa: SLF001

    def test_a_flat_job_still_demands_a_model(self, monkeypatch, tmp_path):
        """The guard that matters is kept for the kinds that need it."""
        from mechbench_runner.config import Config

        class Stub:
            def __init__(self, _s, path=None): self.path = path or "/tmp/s.sock"
            def start(self): return None
            def stop(self): return None

        monkeypatch.setattr(jr, "ControlServer", Stub)
        runner = jr.JobRunner(Config(
            api_base_url="http://x", api_key="k", poll_interval_seconds=0.01,
            warm_model_id=None,
        ))
        job = {"id": "j1", "protocolKind": "layer_ablation",
               "spec": {"prompt": "hi"}}
        with pytest.raises(ValueError, match="spec.modelId is missing"):
            runner._handle(None, job)  # noqa: SLF001
