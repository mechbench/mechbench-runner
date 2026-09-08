"""The job spool: work-in-progress that survives the process (epic
000320, task 000323 second half).

Under ``~/.mechbench/spool/<job>/``:

    result.cbor, result.sha256      the finished result (first half)
    <node>/fingerprint              the process identity the node ran under
    <node>/items/<hash>.cbor        one completed item of an item-resumable
                                    block: {"key", "item"}
    <node>/checkpoint/weights.safetensors, opt.safetensors, state.cbor
                                    a training checkpoint
    <node>/done                     {"path", "fingerprint"} of an emitted node

Every write is whole-file-then-rename, so a crash leaves no half
item. A node whose fingerprint CHANGES between attempts has its
partials discarded here as well as ignored by the executor — belt and
braces, because a stale partial is worse than none.

The resume map the executor takes is rebuilt from this directory on
re-claim; the executor re-derives every fingerprint and honours an
entry only under equality, so this module never has to be right about
process identity on its own.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from pathlib import Path
from typing import Any

from .paths import spool_dir


def _write_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


class JobSpool:
    """One job's spool. Methods mirror the executor's hooks."""

    def __init__(self, job_id: str) -> None:
        self.job_id = job_id
        self.root = spool_dir() / job_id

    # --- hooks --------------------------------------------------------------

    def node_start(self, nid: str, fingerprint: str) -> None:
        """Record the identity the node runs under. A different
        fingerprint from last time invalidates that node's partials."""
        d = self.root / nid
        fp = d / "fingerprint"
        if fp.is_file() and fp.read_text().strip() != fingerprint:
            for sub in ("items", "checkpoint"):
                shutil.rmtree(d / sub, ignore_errors=True)
            (d / "done").unlink(missing_ok=True)
        _write_atomic(fp, fingerprint.encode())

    def item(self, nid: str, key: str, item: Any) -> None:
        from mechbench_schema import dump_canonical

        name = hashlib.sha256(key.encode()).hexdigest()[:24] + ".cbor"
        _write_atomic(self.root / nid / "items" / name,
                      dump_canonical({"key": key, "item": item}))

    def checkpoint(self, nid: str, state: dict[str, Any]) -> None:
        """Weights and optimizer arrays as safetensors; the rest as
        CBOR. The numpy generator state holds 128-bit integers, which
        CBOR cannot carry natively, so it travels as a JSON string."""
        import mlx.core as mx
        from mechbench_schema import dump_canonical

        d = self.root / nid / "checkpoint"
        d.mkdir(mode=0o700, parents=True, exist_ok=True)
        tmp = d.with_name("checkpoint.tmp")
        shutil.rmtree(tmp, ignore_errors=True)
        tmp.mkdir(mode=0o700)
        mx.save_safetensors(str(tmp / "weights.safetensors"),
                            {k: mx.array(v) for k, v in state["weights"].items()})
        arrays = {k: mx.array(v) for k, v in state["opt_state"].items()
                  if hasattr(v, "shape")}
        scalars = {k: v for k, v in state["opt_state"].items()
                   if not hasattr(v, "shape")}
        mx.save_safetensors(str(tmp / "opt.safetensors"), arrays)
        meta = {
            "step": int(state["step"]),
            "opt_scalars": scalars,
            "np_rng_json": json.dumps(state["np_rng"]),
            "mx_key": (None if state.get("mx_key") is None
                       else [int(x) for x in state["mx_key"]]),
        }
        (tmp / "state.cbor").write_bytes(dump_canonical(meta))
        # Swap the finished checkpoint in whole: never a torn one.
        shutil.rmtree(d, ignore_errors=True)
        os.replace(tmp, d)

    def node_done(self, nid: str, path: str | None, fingerprint: str) -> None:
        from mechbench_schema import dump_canonical

        _write_atomic(self.root / nid / "done",
                      dump_canonical({"path": path, "fingerprint": fingerprint}))

    # --- reading back ---------------------------------------------------------

    def resume_map(self) -> dict[str, dict[str, Any]]:
        """What the executor's `resume=` takes, rebuilt from disk."""
        import mlx.core as mx
        import numpy as np
        from mechbench_schema import load_raw

        out: dict[str, dict[str, Any]] = {}
        if not self.root.is_dir():
            return out
        for d in sorted(p for p in self.root.iterdir() if p.is_dir()):
            fp_file = d / "fingerprint"
            if not fp_file.is_file():
                continue
            entry: dict[str, Any] = {"fingerprint": fp_file.read_text().strip()}
            done = d / "done"
            if done.is_file():
                rec = load_raw(done.read_bytes())
                if isinstance(rec, dict) and rec.get("path"):
                    entry["done"] = rec["path"]
                    out[d.name] = entry
                    continue
            items_dir = d / "items"
            if items_dir.is_dir():
                items: dict[str, Any] = {}
                for f in sorted(items_dir.glob("*.cbor")):
                    try:
                        rec = load_raw(f.read_bytes())
                    except Exception:  # noqa: BLE001 — a torn frame is not an item
                        continue
                    if isinstance(rec, dict) and "key" in rec:
                        items[str(rec["key"])] = rec["item"]
                if items:
                    entry["items"] = items
            ck = d / "checkpoint"
            if (ck / "state.cbor").is_file():
                meta = load_raw((ck / "state.cbor").read_bytes())
                weights = {k: np.array(v) for k, v in
                           mx.load(str(ck / "weights.safetensors")).items()}
                opt = {k: np.array(v) for k, v in
                       mx.load(str(ck / "opt.safetensors")).items()}
                opt.update(meta.get("opt_scalars") or {})
                entry["checkpoint"] = {
                    "step": int(meta["step"]),
                    "weights": weights,
                    "opt_state": opt,
                    "np_rng": json.loads(meta["np_rng_json"]),
                    "mx_key": (None if meta.get("mx_key") is None
                               else np.array(meta["mx_key"], dtype=np.uint32)),
                }
            if len(entry) > 1:
                out[d.name] = entry
        return out

    def summary(self) -> dict[str, Any]:
        """For the first progress report after a resume: where it picks
        up and how much it reuses."""
        m = self.resume_map()
        reused_items = sum(len(e.get("items", {})) for e in m.values())
        done_nodes = [n for n, e in m.items() if "done" in e]
        step = max((e["checkpoint"]["step"] for e in m.values()
                    if "checkpoint" in e), default=0)
        first_partial = next((n for n, e in m.items() if "done" not in e), None)
        return {
            "node": first_partial or (done_nodes[-1] if done_nodes else ""),
            "reused": reused_items + len(done_nodes),
            **({"step": step} if step else {}),
        }

    def clear(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)
