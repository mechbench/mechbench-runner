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

from mcp.server.fastmcp import FastMCP
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


def build_server(
    config: Config | None = None,
    executor: ProtocolExecutor | None = None,
) -> FastMCP:
    """Construct the MCP server. Factored out so in-process tests can
    exercise the tools without spawning a stdio subprocess."""
    cfg = config or Config.from_env()
    _executor = executor or ProtocolExecutor()
    mcp = FastMCP("mechbench-runner")

    @mcp.tool()
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

    @mcp.tool()
    def get_result(path: str) -> dict[str, Any]:
        """Fetch a cached result from mechbench-api by its
        MechbenchPath, decoded to a plain structure."""
        with ApiClient(cfg) as api:
            raw = api.fetch_object(path)
        return _decode_object(raw)

    @mcp.tool()
    def list_jobs() -> list[dict[str, Any]]:
        """List the caller's queued / running / completed jobs."""
        with ApiClient(cfg) as api:
            return api.list_jobs()

    return mcp


def run_stdio(config: Config | None = None) -> None:
    """Run the MCP server over stdio. Invoked by `mechbench-runner mcp`."""
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
