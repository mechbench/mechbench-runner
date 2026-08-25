"""MCP server exposing three mechbench tools over stdio.

Tools:

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
    """The three tools as PLAIN functions, keyed by their wire names.

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

    return {
        "run_protocol": run_protocol,
        "get_result": get_result,
        "list_jobs": list_jobs,
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
