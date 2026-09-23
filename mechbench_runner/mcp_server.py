"""MCP server exposing mechbench tools over stdio.

Each tool is a verb the command line has too, with the same arguments
(docs/CAPABILITIES.md): `run`, `runs`, `label`, `protocol_push` and
`protocol_export` are `mechbench run`, `runs`, `label`, `protocol push`
and `protocol export`, over the same `mechbench_compute.bench` calls.

The older three:

  run_protocol(prompt, protocol_kind?, model_id?)
      Runs the protocol *in-process* via mechbench-compute and returns
      the LayerAblationPayload dict. Does not round-trip through the
      mechbench-api job queue — the MCP caller wants the answer, and
      we are the compute target. (The job-runner subcommand is the
      queued path for UI-triggered jobs.)

  get_result(path)
      Fetches /objects/<path> from mechbench-api. Returns the parsed
      JSON payload.

  list_jobs()
      Lists the caller's jobs via GET /jobs.

Stdio transport only for v0. SSE / HTTP transports when remote
deploy earns its seat (deferred explicitly in task 000185).
"""

from __future__ import annotations

import json
from typing import Any

from mcp.server import MCPServer
from mechbench_compute.protocol import ProtocolExecutor, ProtocolSpec

from .api_client import ApiClient
from .config import Config


def _decode_object(raw: bytes) -> dict[str, Any]:
    """Decode a stored object, whichever way it was written.

    Results are canonical CBOR (task 000186), and this used to call
    `json.loads` — which had gone unnoticed because nothing exercised it
    against a store that had CBOR in it. The legacy JSON completion path
    is still inside its deprecation window (000181), so both shapes have
    to be read rather than one assumed.

    Sniffed rather than inferred from the path: the encoding is a
    property of the bytes, and a path says nothing about when they were
    written.
    """
    from mechbench_schema import load_raw

    if raw[:1] in (b"{", b"["):
        return json.loads(raw)  # type: ignore[no-any-return]
    decoded = load_raw(raw)
    if not isinstance(decoded, dict):
        raise ValueError(
            f"expected an object at the top level, got {type(decoded).__name__}"
        )
    return decoded


def build_tools(
    config: Config | None = None,
    executor: ProtocolExecutor | None = None,
) -> dict[str, Any]:
    """The tools as PLAIN functions, keyed by their wire names.

    The server registers these; the smoke test calls them directly.
    Under mcp 1.x the smoke reached into `server._tool_manager` for the
    bound functions — private access the 2.0 restructuring rightly
    broke (000298). Plain functions need no way in at all.
    """
    cfg = config or Config.from_env()
    _executor = executor or ProtocolExecutor()

    def run_protocol(
        prompt: str,
        protocol_kind: str = "layer_ablation",
        model_id: str | None = None,
    ) -> dict[str, Any]:
        """Run an interpretability protocol in-process and return
        the structured result. `protocol_kind` defaults to
        layer_ablation (the only kind wired in v0). `model_id`
        falls back to MECHBENCH_WARM_MODEL_ID, and is required if
        that is unset."""
        spec = ProtocolSpec(
            kind=protocol_kind,
            prompt=prompt,
            model_id=_require_model(model_id, cfg),
        )
        payload = _executor.run(spec)
        return payload.model_dump(mode="json")

    def get_result(path: str) -> dict[str, Any]:
        """Fetch a cached result from mechbench-api by its
        MechbenchPath, decoded to a plain structure."""
        with ApiClient(cfg) as api:
            raw = api.fetch_object(path)
        return _decode_object(raw)

    def list_jobs() -> list[dict[str, Any]]:
        """List the caller's queued / running / completed jobs."""
        with ApiClient(cfg) as api:
            return api.list_jobs()

    def _bench():
        from mechbench_compute import bench

        bench.configure(api_url=cfg.api_base_url, api_key=cfg.require_api_key())
        return bench

    def run(
        protocol: str,
        params: dict[str, Any] | None = None,
        inputs: dict[str, str] | None = None,
        keep: str | None = None,
        budget: float | None = None,
        label: str | None = None,
    ) -> dict[str, Any]:
        """Launch a protocol (its id) on the queue: `params` and `inputs`
        (stored objects, by path) bind by the names the protocol declares;
        `keep="outputs"` holds intermediates on the runner; `budget` caps
        the run in USD; `label`, one line, says what the run is for and
        finds it again with `runs`. Returns the run, with its `jobId`."""
        return _bench().launch(protocol, params=params, inputs=inputs,
                               keep=keep, budget=budget, label=label)

    def runs(
        label: str | None = None,
        label_contains: str | None = None,
        protocol: str | None = None,
        project: str | None = None,
        owner: str | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """Runs newest first, by exact `label` or `label_contains`, a
        `protocol` id, a `project` (`owner/project`), or an `owner`'s
        (your own by default). Each row carries its job id and status,
        result path, protocol and compute versions, spend and label."""
        return _bench().runs(label=label, label_contains=label_contains,
                             protocol=protocol, project=project, owner=owner,
                             limit=limit)

    def label(run: str, label: str | None) -> dict[str, Any]:
        """Relabel a run (its id or its job's id), or clear it with null.
        The change is kept in the job's history."""
        return _bench().label_run(run, label)

    def protocol_push(file: str, into: str, org: bool = False) -> dict[str, Any]:
        """Push a protocol file (a path to its JSON) into `into`,
        `owner/project` (`org` when the owner is an org). The action is
        `created`, `versioned`, `described` or `unchanged`; a file in the
        legacy form or failing the wiring checks is `refused`, with its
        code and findings, and nothing is stored."""
        bench = _bench()
        try:
            return bench.push_protocol(file, into, owner_kind="org" if org else "user")
        except bench.BenchError as e:
            if e.status is None or not isinstance(e.body, dict):
                raise
            return {"action": "refused", **e.body}

    def protocol_export(
        protocol: str, version: int | None = None, path: str | None = None
    ) -> dict[str, Any]:
        """A protocol version (the head by default) as its canonical file
        text, which a push reads back as unchanged; written to `path`
        exactly when one is given. Returns `{protocolId, name, version,
        text}`."""
        return _bench().export_protocol(protocol, version=version, path=path)

    return {
        "run_protocol": run_protocol,
        "get_result": get_result,
        "list_jobs": list_jobs,
        "run": run,
        "runs": runs,
        "label": label,
        "protocol_push": protocol_push,
        "protocol_export": protocol_export,
    }


def build_server(
    config: Config | None = None,
    executor: ProtocolExecutor | None = None,
) -> MCPServer:
    """Construct the MCP server (mcp 2.x, task 000298). The tool
    names, signatures and docstrings are the contract an agent sees —
    identical to the 1.x surface, because the library changing is not
    a reason the contract should."""
    server = MCPServer("mechbench")
    for fn in build_tools(config, executor).values():
        server.tool()(fn)
    return server


def run_stdio(config: Config | None = None) -> None:
    """Run the MCP server over stdio. Invoked by `mechbench mcp`."""
    server = build_server(config)
    server.run(transport="stdio")


def _require_model(model_id: str | None, cfg: Config) -> str:
    """A protocol has to name its model; this layer will not choose one."""
    resolved = model_id or cfg.warm_model_id
    if not resolved:
        raise ValueError(
            "model_id is required: pass one, or set MECHBENCH_WARM_MODEL_ID "
            "for this runner."
        )
    return resolved
