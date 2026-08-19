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

    def run(self, spec: ExperimentSpec, on_progress=None) -> Any:
        """Execute a job spec. `on_progress(done, total)` is invoked
        after each unit of work for kinds that have a natural unit
        (decision_distribution: one condition); it must be cheap and
        may be None."""
        if spec.kind == "layer_ablation":
            return self._run_layer_ablation(spec.prompt, spec.model_id)
        if spec.kind == "decision_distribution":
            return self._legacy_decision_distribution(spec, on_progress)
        if spec.kind == "pipeline":
            return self._run_pipeline(spec, on_progress)
        raise ValueError(f"unsupported experimentKind: {spec.kind!r}")



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


    def _legacy_decision_distribution(self, spec: ExperimentSpec,
                                      on_progress=None) -> Any:
        """The pre-protocol decision_distribution kind, kept as a thin
        shim over the decision-read block (superseded-code cleanup,
        2026-08-19): same spec in, same payload shape out, one
        implementation."""
        from datetime import datetime, timezone

        import mechbench_schema as ms
        from mechbench_core import __version__ as core_version

        extra = spec.extra or {}
        conditions = extra.get("conditions", [])
        result = self._run_model_block(
            self._block_decision_read,
            {"conditions": conditions},
            {"model": spec.model_id},
            on_item=None, on_start=None)
        prov = ms.Provenance(
            created_at=datetime.now(timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%SZ"),
            produced_by=ms.ToolInfo(tool="mechbench-agent",
                                    version=core_version),
            inputs=[],
            params_fingerprint=ms.fingerprint_params(
                {k: v for k, v in extra.items() if k != "resultPath"}),
            schema_version=ms.__version__,
        )
        return ms.Emitted(
            payload={"kind": "decision_distribution",
                     "model": spec.model_id,
                     "conditions": result["conditions"]},
            provenance=prov)

    def _run_pipeline(self, spec: ExperimentSpec, on_progress=None) -> Any:
        """Execute a protocol graph (epic 000258, arc B): topological
        order over the nodes, pure blocks resolved from the core
        registry, model blocks executed in-process with the prefix
        cache. v1 restrictions: single output per node (edges' port
        names select inputs but every node produces one value) and the
        whole graph runs in this one job — multi-job planning is the
        planner's future concern, not the executor's.

        Params may reference bindings: any string param "$name"
        resolves to spec bindings[name]."""
        from datetime import datetime, timezone

        import mechbench_schema as ms
        from mechbench_core import __version__ as core_version
        from mechbench_core.blocks import PURE_BLOCKS
        from mechbench_core.distill import (
            encode, expand_top_outcomes_cached, prefill_decision,
            render_chat,
        )

        extra = spec.extra or {}
        graph = extra.get("graph") or {}
        bindings = extra.get("bindings") or {}
        nodes = {n["id"]: n for n in graph.get("nodes", [])}
        edges = graph.get("edges", [])

        # What actually resolved (task 000260): every $fetch's content
        # hash and every model ref's snapshot commit, recorded into the
        # result manifest — reproducibility by record; pins opt into
        # strictness.
        resolved: dict[str, dict] = {"objects": {}, "models": {}}

        def resolve_value(v):
            """Recursive param resolution. Forms beyond literals:
            "$name"                    -> the run binding (a string).
            {"$fetch": ref}            -> the bench object's payload.
            {"$fetch": ref, "sha256":} -> same, verified against the
                                          pinned content hash."""
            if isinstance(v, str) and v.startswith("$"):
                name = v[1:]
                if name not in bindings:
                    raise ValueError(f"unbound hole: {v}")
                return bindings[name]
            if isinstance(v, dict) and "$fetch" in v                     and set(v.keys()) <= {"$fetch", "sha256"}:
                from mechbench_core import bench
                ref = resolve_value(v["$fetch"])
                fetched, meta = bench.fetch(ref, with_meta=True)
                got = (meta or {}).get("content_hash") or ""
                resolved["objects"][str(ref)] = got
                want = v.get("sha256")
                if want and not got.endswith(str(want)):
                    raise ValueError(
                        f"pinned object {ref!r} resolved to {got!r}, "
                        f"expected sha256 {want!r}")
                return fetched.get("payload", fetched)                     if isinstance(fetched, dict) else fetched
            if isinstance(v, dict):
                return {k: resolve_value(x) for k, x in v.items()}
            if isinstance(v, list):
                return [resolve_value(x) for x in v]
            return v

        def resolve_hf_dataset(spec):
            """{"$hf_dataset": {repo, split, config?, revision?, limit?,
            columns?: {id?, coords?: [...]}}} -> a record stream shaped
            like our own: {id, coords, values} per row, columns as
            values (Template substitutes them), declared coords
            columns lifted into coords. Resolution recorded (repo,
            requested revision, arrow fingerprint, rows)."""
            from datasets import load_dataset

            repo = spec["repo"]
            split = spec.get("split", "train")
            config = spec.get("config")
            revision = spec.get("revision")
            limit = spec.get("limit")
            colmap = spec.get("columns") or {}
            kwargs = {"split": split}
            if revision:
                kwargs["revision"] = revision
            ds = (load_dataset(repo, config, **kwargs) if config
                  else load_dataset(repo, **kwargs))
            n = min(int(limit), len(ds)) if limit else len(ds)
            id_col = colmap.get("id")
            coord_cols = list(colmap.get("coords") or [])
            records = []
            for i in range(n):
                row = ds[i]
                rid = str(row[id_col]) if id_col else f"{split}-{i}"
                coords = {c: str(row[c]) for c in coord_cols}
                values = {k: str(v) for k, v in row.items()
                          if k not in coord_cols}
                records.append({"id": rid,
                                 "coords": {"split": split, **coords},
                                 "values": values})
            key = f"{repo}@{revision}" if revision else repo
            resolved.setdefault("datasets", {})[key] = {
                "repo": repo, "config": config, "split": split,
                "revision": revision,
                "fingerprint": getattr(ds, "_fingerprint", None),
                "rows_total": len(ds), "rows_used": n}
            return {"kind": "record_set", "records": records}

        def record_model(ref):
            if not isinstance(ref, str) or ref in resolved["models"]:
                return
            from mechbench_core.hub import (
                parse_model_ref, resolve_cached_revision,
            )
            try:
                repo, rev = parse_model_ref(ref)
                resolved["models"][ref] = {
                    "repo": repo, "pinned": rev,
                    "commit": resolve_cached_revision(repo, rev)}
            except Exception:  # noqa: BLE001 — recording is best-effort
                resolved["models"][ref] = {"repo": ref, "pinned": None,
                                            "commit": None}

        def resolve_params(params):
            return {k: resolve_value(v) for k, v in (params or {}).items()}

        # Topological order (Kahn). The API validated acyclicity, but a
        # runner never trusts its inputs to be well-formed.
        indeg = {nid: 0 for nid in nodes}
        for e in edges:
            indeg[e["to"]["node"]] += 1
        order = [nid for nid, d in indeg.items() if d == 0]
        i = 0
        while i < len(order):
            for e in edges:
                if e["from"]["node"] == order[i]:
                    t = e["to"]["node"]
                    indeg[t] -= 1
                    if indeg[t] == 0:
                        order.append(t)
            i += 1
        if len(order) != len(nodes):
            raise ValueError("pipeline graph has a cycle")

        # Progress: one unit per node, but model blocks expand the
        # denominator to their item count on entry and tick per item —
        # the board's bar moves per condition/story, not per node.
        results: dict[str, Any] = {}
        total_units = len(order)
        done_units = 0

        def report():
            if on_progress:
                on_progress(done_units, total_units)

        def bump(n=1):
            nonlocal done_units
            done_units += n
            report()

        def expand(n_items):
            nonlocal total_units
            if n_items > 1:
                total_units += n_items - 1
                report()

        def on_item():
            bump(1)

        # Per-node emission (arc B second half): every node's output
        # becomes a bench object under the job's result namespace, with
        # lineage inputs = its upstream nodes' paths and operation =
        # the block ref. The protocol graph and the lineage graph are
        # then the same graph, by construction.
        from mechbench_core import bench

        result_base = extra.get("resultPath")
        node_paths: dict[str, str] = {}

        for nid in order:
            node = nodes[nid]
            block = node["block"]
            params = resolve_params(node.get("params"))
            if "model" in params:
                record_model(params.get("model"))
            in_edges = [e for e in edges if e["to"]["node"] == nid]
            inputs = {
                e["to"]["port"]: results[e["from"]["node"]]
                for e in in_edges
            }
            if block in PURE_BLOCKS:
                results[nid] = PURE_BLOCKS[block](inputs, params)
            elif block == "~canonical/ops/decision-read/1":
                results[nid] = self._run_model_block(
                    self._block_decision_read, inputs, params,
                    on_item=on_item, on_start=expand)
            elif block == "~canonical/ops/generate/1":
                results[nid] = self._run_model_block(
                    self._block_generate, inputs, params,
                    on_item=on_item, on_start=expand)
            elif block == "~canonical/ops/lens-trajectory/1":
                results[nid] = self._run_model_block(
                    self._block_lens, inputs, params,
                    on_item=on_item, on_start=expand)
            elif block == "~canonical/ops/finetune/lora/1":
                results[nid] = self._block_finetune_lora(
                    inputs, params, on_item=on_item, on_start=expand)
            elif block == "~canonical/ops/score/1":
                input_paths = {
                    e["to"]["port"]: node_paths.get(e["from"]["node"], "")
                    for e in in_edges
                }
                results[nid] = self._run_model_block(
                    self._block_score, inputs, params, input_paths,
                    on_item=on_item, on_start=expand)
            else:
                raise ValueError(f"unknown block: {block!r}")
            if result_base:
                out = bench.emit(
                    f"{result_base}/{nid}",
                    results[nid],
                    inputs=[node_paths[e["from"]["node"]]
                            for e in in_edges],
                    operation=block,
                    params=params,
                )
                node_paths[nid] = out["path"]
            bump()

        terminals = [nid for nid in nodes
                     if not any(e["from"]["node"] == nid for e in edges)]

        def sanitize(v, at):
            """Manifests reference binary, never embed it: bytes are
            replaced by a stub pointing at the node object that holds
            the real payload."""
            if isinstance(v, bytes):
                return {"$binary": {"bytes": len(v), "stored_at": at}}
            if isinstance(v, dict):
                return {k: sanitize(x, at) for k, x in v.items()}
            if isinstance(v, list):
                return [sanitize(x, at) for x in v]
            return v

        payload = {
            "kind": "pipeline_result",
            "outputs": {nid: sanitize(results[nid], node_paths.get(nid, ""))
                         for nid in terminals},
            "nodes_executed": order,
            "node_paths": node_paths,
            "resolved": resolved,
        }
        prov = ms.Provenance(
            created_at=datetime.now(timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%SZ"),
            produced_by=ms.ToolInfo(tool="mechbench-agent",
                                    version=core_version),
            inputs=[],
            params_fingerprint=ms.fingerprint_params(
                {"graph": graph, "bindings": bindings}),
            schema_version=ms.__version__,
        )
        return ms.Emitted(payload=payload, provenance=prov)

    def _block_generate(self, inputs, params, on_item=None,
                        on_start=None) -> Any:
        """The Generate model block: per condition record, sample n
        completions into a text-fidelity DocumentCollection. Range
        rule (epic 000258 amendment 4): each sample's rng derives from
        (seed, record id, index), indices [start, start+n) — growing a
        corpus is the same node over a later range plus Union. The
        prompt is prefilled once per record and the KV cache copied
        per sample."""
        import hashlib as _hashlib

        import numpy as _np

        from mechbench_core.distill import encode, prefill_decision, render_chat
        from mechbench_core.generate import sample_completion_cached

        model = self._model_loaded(params.get("model"))
        tok = model.tokenizer
        records = inputs.get("records") or []
        if isinstance(records, dict):
            records = records.get("conditions") or records.get("records") or []
        f_system = params.get("system_field", "system")
        f_user = params.get("user_field", "user")
        n = int(params.get("n", 1))
        start = int(params.get("start", 0))
        seed = params.get("seed", 0)
        temperature = float(params.get("temperature", 0.9))
        top_p = float(params.get("top_p", 0.95))
        max_tokens = int(params.get("max_tokens", 256))
        fidelity = params.get("fidelity", "text")
        if fidelity not in ("text", "trace"):
            raise ValueError(f"generate: unsupported fidelity {fidelity!r}")

        from mechbench_core.generate import offsets_by_cumulative_decode

        if on_start:
            on_start(len(records) * n)
        items = []
        for rec in records:
            if f_user not in rec:
                raise ValueError(
                    f"generate: record {rec.get('id')!r} has no "
                    f"{f_user!r} field")
            rendered = render_chat(tok, rec.get(f_system, ""),
                                   rec[f_user], "")
            ids = encode(tok, rendered)
            prefill = prefill_decision(model, ids)
            for k in range(start, start + n):
                digest = _hashlib.sha256(
                    f"{seed}:{rec['id']}:{k}".encode()).digest()
                rng = _np.random.default_rng(
                    int.from_bytes(digest[:8], "little"))
                text, out_ids = sample_completion_cached(
                    model, ids, max_tokens=max_tokens,
                    temperature=temperature, top_p=top_p, rng=rng,
                    prefill=prefill, return_ids=True)
                item = {
                    "id": f"{rec['id']}-s{k}",
                    "kind": "~canonical/kinds/text",
                    "text": text,
                    "metadata": {
                        "coords": {**rec.get("coords", {}), "sample": k},
                        "sampling": {"temperature": temperature,
                                     "top_p": top_p, "seed": seed,
                                     "index": k},
                        "model": params.get("model"),
                    },
                }
                if fidelity == "trace":
                    full_ids = list(ids) + list(out_ids)
                    offs, full_text = offsets_by_cumulative_decode(
                        tok, full_ids)
                    item["trace"] = {
                        "token_ids": [int(t) for t in full_ids],
                        "tokenizer": str(params.get("model")),
                        "text": full_text,
                        "offsets": [[int(a), int(b)] for a, b in offs],
                        "generation_spans": [{
                            "token_start": len(ids),
                            "token_end": len(full_ids),
                            "model": params.get("model"),
                            "temperature": temperature,
                            "top_p": top_p,
                            "seed": k,
                        }],
                    }
                    item["segmentations"] = [{
                        "schema_name": "envelope",
                        "segments": [
                            {"role": "prompt", "token_start": 0,
                             "token_end": len(ids)},
                            {"role": "body", "token_start": len(ids),
                             "token_end": len(full_ids)},
                        ],
                    }]
                items.append(item)
                if on_item:
                    on_item()
        return {
            "kind": "document_collection",
            "name": params.get("name", "generated"),
            "description": params.get("description", ""),
            "fidelity": fidelity,
            "item_kind": "~canonical/kinds/text",
            "items": items,
        }

    def _run_model_block(self, fn, inputs, params, *args, **kwargs):
        """Model-block wrapper: load the bound model, fuse an adapter
        when one arrives (input port `adapter` or params.adapter), run
        the block, restore. Blocks themselves stay adapter-unaware —
        their own _model_loaded call returns the same fused instance."""
        model = self._model_loaded(params.get("model"))
        with self._adapter_fused(model, inputs, params):
            return fn(inputs, params, *args, **kwargs)

    def _adapter_fused(self, model, inputs, params):
        """Context manager: when the block has an adapter (input port
        `adapter`, or params.adapter already $fetch-resolved to an
        adapter payload), write its safetensors bytes to a temp file,
        fuse onto the model, and restore on exit. Returns the fuse
        handle or None."""
        import contextlib
        import os
        import tempfile

        from mechbench_core.lora import fuse, load_adapter, restore

        @contextlib.contextmanager
        def _cm():
            payload = inputs.get("adapter") or params.get("adapter")
            if not payload:
                yield None
                return
            if not isinstance(payload, dict) or "data" not in payload:
                raise ValueError(
                    "adapter must be an adapter object payload with "
                    "safetensors bytes under 'data'")
            lora_cfg = payload.get("lora") or {}
            scale = float(params.get(
                "adapter_scale",
                lora_cfg.get("alpha", 16) / lora_cfg.get("rank", 8)))
            fd, path = tempfile.mkstemp(suffix=".safetensors")
            os.close(fd)
            try:
                with open(path, "wb") as f:
                    f.write(payload["data"])
                handle = fuse(model.lm, load_adapter(path), scale=scale)
                try:
                    yield handle
                finally:
                    restore(model.lm, handle)
            finally:
                os.unlink(path)
        return _cm()

    def _block_finetune_lora(self, inputs, params, on_item=None,
                             on_start=None) -> Any:
        """The finetune/lora block (epic 000259): Regime D soft-target
        training as an artifact-producing operation. Consumes training
        prompt records (and optionally anchor records with an answer
        field); produces an ADAPTER OBJECT — safetensors bytes plus
        config — whose lineage is the training's methods section.

        The training mutates the in-process model via apply_lora; after
        saving the adapter the runner's model is force-reloaded so
        downstream blocks (and later jobs) see a clean base."""
        import os
        import tempfile

        from mechbench_core.distill import render_chat
        from mechbench_core.finetune import (
            build_anchor_items, build_target_items, target_map_from_spec,
            train_soft_ce,
        )
        from mechbench_core.lora import apply_lora, save_adapter

        model = self._model_loaded(params.get("model"))
        tok = model.tokenizer

        records = inputs.get("records") or params.get("records") or []
        if isinstance(records, dict):
            records = records.get("conditions") or records.get("records") or []
        f_system = params.get("system_field", "system")
        f_user = params.get("user_field", "user")
        f_prefill = params.get("prefill_field", "prefill")

        def rendered_of(rec):
            return render_chat(tok, rec.get(f_system, ""),
                               rec[f_user], rec.get(f_prefill, ""))

        target_spec = params.get("target")
        if not target_spec:
            raise ValueError("finetune/lora: params.target is required")
        target = target_map_from_spec(target_spec)
        closer = params.get("closer", " }")
        marginals, continuations = build_target_items(
            tok, target, [rendered_of(r) for r in records], closer=closer)

        anchor_records = inputs.get("anchors") or params.get("anchors") or []
        if isinstance(anchor_records, dict):
            anchor_records = (anchor_records.get("conditions")
                              or anchor_records.get("records") or [])
        f_answer = params.get("answer_field", "answer")
        anchors = build_anchor_items(
            tok, [(rendered_of(r), r[f_answer]) for r in anchor_records])

        lora_cfg = params.get("lora") or {}
        rank = int(lora_cfg.get("rank", 8))
        alpha = float(lora_cfg.get("alpha", 16))
        steps = int(params.get("steps", 250))
        lr = float(params.get("lr", 1e-4))
        seed = int(params.get("seed", 7))
        batch = params.get("batch") or {"target": 3, "anchor": 1,
                                         "continuation": 2}

        n_lora = apply_lora(model.lm, rank, alpha)
        if on_start:
            on_start(steps)
        final_loss = train_soft_ce(
            model.lm,
            {"target": marginals, "anchor": anchors,
             "continuation": continuations},
            batch, steps=steps, lr=lr, seed=seed,
            on_step=(lambda s, l: on_item()) if on_item else None)

        fd, path = tempfile.mkstemp(suffix=".safetensors")
        os.close(fd)
        try:
            save_adapter(model.lm, path)
            with open(path, "rb") as f:
                data = f.read()
        finally:
            os.unlink(path)

        # The in-process model now carries LoRA wrappers; evict it so
        # every later block starts from a clean base.
        self._model = None
        self._model_id = None

        return {
            "kind": "adapter",
            "format": "safetensors",
            "base_model": params.get("model"),
            "lora": {"rank": rank, "alpha": alpha,
                      "scale": alpha / rank,
                      "params": n_lora},
            "train": {"steps": steps, "lr": lr, "seed": seed,
                       "batch": batch, "final_loss": round(final_loss, 4),
                       "n_prompts": len(records),
                       "n_anchors": len(anchor_records),
                       "closer": closer,
                       "target": target_spec},
            "data": data,
        }

    def _block_lens(self, inputs, params, on_item=None,
                    on_start=None) -> Any:
        """The LensTrajectory model block: per condition record, the
        logit-lens trajectory at the final position — every layer's
        residual projected through the head, recording top-1 token, its
        probability, and entropy (the commitment-funnel instrument,
        020's lens_rows as a registered block). Output items are
        lens-trajectory/2, so collections render as overlaid funnel
        curves."""
        import numpy as _np

        from mechbench_core import Capture
        from mechbench_core.distill import encode, render_chat

        model = self._model_loaded(params.get("model"))
        tok = model.tokenizer
        records = inputs.get("records") or []
        if isinstance(records, dict):
            records = records.get("conditions") or records.get("records") or []
        f_system = params.get("system_field", "system")
        f_user = params.get("user_field", "user")
        f_prefill = params.get("prefill_field", "prefill")
        n_layers = len(model.lm.model.layers)

        if on_start:
            on_start(len(records))
        items = []
        for rec in records:
            if f_user not in rec:
                raise ValueError(
                    f"lens: record {rec.get('id')!r} has no {f_user!r} field")
            rendered = render_chat(tok, rec.get(f_system, ""),
                                   rec[f_user], rec.get(f_prefill, ""))
            ids = encode(tok, rendered)
            r = model.run(
                mx.array([ids]),
                interventions=[Capture.residual(layers=range(n_layers))])
            layers = []
            for i in range(n_layers):
                row = model.project_to_logits(
                    r.cache[f"blocks.{i}.resid_post"])[0, -1, :]
                z = _np.array(row.astype(mx.float32)).astype(_np.float64)
                z -= z.max()
                pr = _np.exp(z) / _np.exp(z).sum()
                top = int(_np.argmax(pr))
                nz = pr[pr > 0]
                layers.append({
                    "layer": i,
                    "top1": tok.decode([top]),
                    "p": round(float(pr[top]), 4),
                    "entropy_bits": round(
                        float(-(nz * _np.log2(nz)).sum()), 3),
                })
            final = layers[-1]
            items.append({
                "id": rec["id"],
                "kind": "~canonical/kinds/lens-trajectory/2",
                "text": f"Lens trajectory, {rec['id']}: final-layer "
                        f"top1={final['top1']!r} (p={final['p']}, "
                        f"H={final['entropy_bits']} bits).",
                "metadata": {"coords": dict(rec.get("coords", {})),
                              "layers": layers},
            })
            if on_item:
                on_item()
        return {
            "kind": "document_collection",
            "name": params.get("name", "lens-trajectories"),
            "description": params.get("description", ""),
            "fidelity": "text",
            "item_kind": "~canonical/kinds/lens-trajectory/2",
            "items": items,
        }

    def _block_score(self, inputs, params, input_paths=None,
                     on_item=None, on_start=None) -> Any:
        """The Score model block: per-token surprisal (bits) under the
        scoring model, over a TRACE-fidelity collection — the 019
        backfill's rows-only scorer as a registered block. Values cover
        every position (prompt and envelope included): surprisal of
        token i given tokens < i. Output is a numeric AnnotationLayer
        whose anchors are token spans, rendered by the DocBrowser as an
        overlay on the collection it scores."""
        import numpy as _np

        model = self._model_loaded(params.get("model"))
        coll = inputs.get("collection")
        coll_path = (input_paths or {}).get("collection", "")
        if coll is None:
            ref = params.get("collection_path")
            if not ref:
                raise ValueError(
                    "score: no collection input edge and no "
                    "collection_path param")
            from mechbench_core import bench
            fetched = bench.fetch(str(ref))
            coll = fetched.get("payload", fetched)
            coll_path = str(ref)
        items = coll.get("items") or []
        if on_start:
            on_start(len(items))
        values = []
        for it in items:
            trace = it.get("trace")
            if not trace:
                raise ValueError(
                    f"score: item {it.get('id')!r} has no trace — Score "
                    "requires a trace-fidelity collection (set the "
                    "Generate block's fidelity to 'trace')")
            ids = trace["token_ids"]
            h = model.trunk_hidden(mx.array([ids]))
            rows = model.head_logits(h[:, :-1, :]).astype(mx.float32)
            tgt = mx.array(ids[1:])
            lp = (mx.take_along_axis(rows[0], tgt[:, None], axis=-1)[:, 0]
                  - mx.logsumexp(rows[0], axis=-1))
            surp = -_np.array(lp) / _np.log(2.0)
            for j, sv in enumerate(surp.tolist()):
                values.append({
                    "anchor": {"item_id": it["id"],
                               "token_start": j + 1,
                               "token_end": j + 2},
                    "value": round(float(sv), 3),
                })
            if on_item:
                on_item()
        return {
            "kind": "annotation_layer",
            "name": params.get("name", "surprisal"),
            "description": params.get(
                "description",
                "Per-token surprisal (bits); values cover every position "
                "including prompt and envelope tokens."),
            "collection": coll_path,
            "value_type": "numeric",
            "required_fidelity": "trace",
            "values": values,
        }

    def _block_decision_read(self, inputs, params, on_item=None,
                             on_start=None) -> Any:
        """The decision-read model block: per condition record, the
        exact decision-token distribution (prefix-cached) and optional
        best-first outcome expansion. Records keep their coords — the
        whole point of the Grid split."""
        import numpy as np

        from mechbench_core.distill import (
            encode, expand_top_outcomes_cached, prefill_decision,
            render_chat, suffix_tokens,
        )

        model = self._model_loaded(params.get("model"))
        tok = model.tokenizer
        conditions = inputs.get("conditions") or params.get("conditions") or []
        if isinstance(conditions, dict):
            conditions = (conditions.get("conditions")
                          or conditions.get("records") or [])
        if on_start:
            on_start(len(conditions))
        rollout = params.get("rollout")
        outcomes = params.get("outcomes")
        # The consumed field names are params, not convention (Benji's
        # composer-legibility review): a Template producing `question`
        # wires user_field: "question" instead of renaming its output.
        f_system = params.get("system_field", "system")
        f_user = params.get("user_field", "user")
        f_prefill = params.get("prefill_field", "prefill")
        out = []
        for cond in conditions:
            if f_user not in cond:
                raise ValueError(
                    f"decision-read: record {cond.get('id')!r} has no "
                    f"{f_user!r} field (fields present: "
                    f"{sorted(k for k in cond if k not in ('id', 'coords'))})")
            rendered = render_chat(tok, cond.get(f_system, ""),
                                   cond[f_user], cond.get(f_prefill, ""))
            ids = encode(tok, rendered)
            prefill = prefill_decision(model, ids)
            lp = np.array(prefill[1] - mx.logsumexp(prefill[1]))
            probs = np.exp(lp.astype(np.float64))
            order_ = np.argsort(-probs)
            nz = probs[probs > 0]
            entry: dict[str, Any] = {
                "id": cond["id"],
                "coords": dict(cond.get("coords", {})),
                "entropy_bits": round(float(-(nz * np.log2(nz)).sum()), 4),
                "top_tokens": [
                    {"token": tok.decode([int(t)]),
                     "p": round(float(probs[t]), 5)}
                    for t in order_[:10]
                ],
            }
            if rollout:
                entry["rollout"] = expand_top_outcomes_cached(
                    model, tok, ids, rollout, prefill=prefill)
            # Per-record outcome sets override the block-level param —
            # heterogeneous batteries (d6 vs coin vs open-ended) carry
            # their outcomes as data.
            cond_outcomes = cond.get("outcomes", outcomes)
            if cond_outcomes:
                masses = {}
                for o in cond_outcomes:
                    t0 = suffix_tokens(tok, rendered, ids, o)[0]
                    masses[o] = round(float(probs[t0]), 5)
                entry["outcome_mass"] = masses
            out.append(entry)
            if on_item:
                on_item()
        return {"kind": "decision_read", "conditions": out}


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
