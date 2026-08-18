"""In-process experiment execution via `mechbench-core`.

Supports only `layer_ablation` in v0 — the same kind the job-runner
shim handled. The MCP `run_experiment` tool calls this directly;
the job-runner dispatches to it after claiming a queued job.

The model is loaded once per process and reused. Load cost is
significant (minutes on first call, seconds on cached weights), so
callers should hold the runner for the process's lifetime rather
than instantiating per request.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import mlx.core as mx
import numpy as np
from mechbench_core import Ablate, GLOBAL_LAYERS, Model, N_LAYERS
from mechbench_schema import (
    AblationPrompt,
    LayerAblationPayload,
    LayerAggregates,
)


@dataclass
class ExperimentSpec:
    kind: str
    prompt: str
    model_id: str
    # Kind-specific spec payload (decision_distribution: conditions list).
    extra: dict[str, Any] | None = None


class ExperimentRunner:
    def __init__(self) -> None:
        self._model: Model | None = None
        self._model_id: str | None = None

    def _model_loaded(self, model_id: str | None = None) -> Model:
        # One model in memory at a time; swapping ids reloads.
        if self._model is None or (model_id and model_id != self._model_id):
            self._model = Model.load(model_id) if model_id else Model.load()
            self._model_id = model_id
        return self._model

    def run(self, spec: ExperimentSpec) -> Any:
        if spec.kind == "layer_ablation":
            return self._run_layer_ablation(spec.prompt, spec.model_id)
        if spec.kind == "decision_distribution":
            return self._run_decision_distribution(spec)
        raise ValueError(f"unsupported experimentKind: {spec.kind!r}")

    def _run_decision_distribution(self, spec: ExperimentSpec) -> Any:
        """Exact decision-token distributions for a battery of prompt
        conditions (the ai-randomness reformulation: one forward pass
        per condition instead of thousands of sampled rollouts)."""
        from datetime import datetime, timezone

        import mechbench_schema as ms
        from mechbench_core import __version__ as core_version
        from mechbench_core.distill import encode, render_chat, suffix_tokens

        model = self._model_loaded(spec.model_id)
        tok = model.tokenizer
        results = []
        for cond in (spec.extra or {}).get("conditions", []):
            rendered = render_chat(tok, cond.get("system", ""),
                                   cond["user"], cond.get("prefill", ""))
            ids = encode(tok, rendered)
            r = model.run(mx.array([ids]))
            last = r.last_logits.reshape(-1, r.last_logits.shape[-1])[-1]
            lp = np.array((last.astype(mx.float32)
                           - mx.logsumexp(last.astype(mx.float32))))
            probs = np.exp(lp.astype(np.float64))
            order = np.argsort(-probs)
            nz = probs[probs > 0]
            entry: dict[str, Any] = {
                "id": cond["id"],
                "entropy_bits": round(float(-(nz * np.log2(nz)).sum()), 4),
                "top_tokens": [
                    {"token": tok.decode([int(t)]),
                     "p": round(float(probs[t]), 5)}
                    for t in order[:10]
                ],
            }
            outcomes = cond.get("outcomes")
            if outcomes:
                masses = {}
                for o in outcomes:
                    t0 = suffix_tokens(tok, rendered, ids, o)[0]
                    masses[o] = round(float(probs[t0]), 5)
                entry["outcome_mass"] = masses
                entry["outcomes_total_mass"] = round(sum(masses.values()), 5)
            results.append(entry)

        prov = ms.Provenance(
            created_at=datetime.now(timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%SZ"),
            produced_by=ms.ToolInfo(tool="mechbench-agent",
                                    version=core_version),
            inputs=[],
            params_fingerprint=ms.fingerprint_params(spec.extra or {}),
            schema_version=ms.__version__,
        )
        return ms.Emitted(provenance=prov, payload={
            "kind": "decision_distribution",
            "model": spec.model_id,
            "conditions": results,
        })

    def _run_layer_ablation(
        self, prompt: str, model_id: str
    ) -> LayerAblationPayload:
        model = self._model_loaded()

        ids = model.tokenize(prompt)
        baseline = model.run(ids)
        baseline_lp = _last_logp(baseline.logits)
        top1_id = int(np.argmax(baseline_lp))
        baseline_top1 = float(baseline_lp[top1_id])

        damage = np.zeros(N_LAYERS, dtype=np.float32)
        for layer in range(N_LAYERS):
            ids = model.tokenize(prompt)
            result = model.run(ids, interventions=[Ablate.layer(layer)])
            lp = _last_logp(result.logits)
            damage[layer] = float(lp[top1_id]) - baseline_top1

        prompts = [
            AblationPrompt(
                text=prompt,
                target="",
                top1_id=top1_id,
                baseline_logprob=round(baseline_top1, 4),
                damage=[round(float(v), 4) for v in damage],
            )
        ]
        return LayerAblationPayload(
            experiment="mechbench-agent:layer_ablation",
            description=(
                "Single-prompt layer ablation: zero each decoder block's "
                "residual-stream update and measure Δ log p of the "
                "model's top-1 prediction."
            ),
            model=model_id,
            n_layers=N_LAYERS,
            global_layers=list(GLOBAL_LAYERS),
            prompts=prompts,
            aggregates=LayerAggregates(
                mean=[round(float(v), 4) for v in damage],
                median=[round(float(v), 4) for v in damage],
            ),
        )


def _last_logp(logits: mx.array) -> np.ndarray:
    last = logits[0, -1, :].astype(mx.float32)
    lp = last - mx.logsumexp(last)
    mx.eval(lp)
    return np.array(lp)


def canonical_json(payload: Any) -> str:
    """Python-side canonical JSON for the mechbench-api hash contract.

    Caveat: cross-language byte-identity is *aspirational* with JSON
    (Python emits `0.0`, JS emits `0`). The mechbench-api side hashes
    the bytes as received, so this canonical form only needs to be
    stable across Python invocations — task 000186 moves the
    contract to canonical CBOR, which pins the form across languages.
    """
    return json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
