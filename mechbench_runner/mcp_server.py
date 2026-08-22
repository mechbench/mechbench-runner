"""MCP server exposing three mechbench tools over stdio.

Tools:

  run_experiment(prompt, experiment_kind?, model_id?)
      Runs the experiment *in-process* via mechbench-compute and returns
      the LayerAblationPayload dict. Does not round-trip through the
      mechbench-api job queue — the MCP caller wants the answer, and
      we are the compute target. (The job-runner subcommand is the
      queued path for UI-triggered jobs.)

  get_result(path)
      Fetches /objects/<path> from mechbench-api. Returns the parsed
      JSON payload.

  list_experiments()
      Lists the caller's jobs via GET /jobs.

Stdio transport only for v0. SSE / HTTP transports when remote
deploy earns its seat (deferred explicitly in task 000185).
"""

from __future__ import annotations

import json
from typing import Any

from mcp.server.fastmcp import FastMCP

from .api_client import ApiClient
from .config import Config
from mechbench_compute.protocol import ProtocolExecutor, ProtocolSpec


def build_server(
    config: Config | None = None,
    runner: ProtocolExecutor | None = None,
) -> FastMCP:
    """Construct the MCP server. Factored out so in-process tests can
    exercise the tools without spawning a stdio subprocess."""
    cfg = config or Config.from_env()
    _runner = runner or ProtocolExecutor()
    mcp = FastMCP("mechbench-runner")

    @mcp.tool()
    def run_experiment(
        prompt: str,
        experiment_kind: str = "layer_ablation",
        model_id: str | None = None,
    ) -> dict[str, Any]:
        """Run an interpretability experiment in-process and return
        the structured result. `experiment_kind` defaults to
        layer_ablation (the only kind wired in v0). `model_id`
        defaults to MECHBENCH_DEFAULT_MODEL_ID."""
        spec = ProtocolSpec(
            kind=experiment_kind,
            prompt=prompt,
            model_id=model_id or cfg.default_model_id,
        )
        payload = _runner.run(spec)
        return payload.model_dump(mode="json")

    @mcp.tool()
    def get_result(path: str) -> dict[str, Any]:
        """Fetch a cached result from mechbench-api by its
        MechbenchPath. Returns the parsed JSON payload."""
        with ApiClient(cfg) as api:
            raw = api.fetch_object(path)
        return json.loads(raw)

    @mcp.tool()
    def list_experiments() -> list[dict[str, Any]]:
        """List the caller's queued / running / completed jobs."""
        with ApiClient(cfg) as api:
            return api.list_jobs()

    return mcp


def run_stdio(config: Config | None = None) -> None:
    """Run the MCP server over stdio. Invoked by `mechbench-runner mcp`."""
    server = build_server(config)
    server.run(transport="stdio")
