"""MCP server exposing mechbench tools over stdio.

One tool per noun (task 000661): `object`, `protocol`, `run`, `article`,
`dataset`, `project`, each taking a `verb` and that verb's `args` by
name, from the registry in `verbs/` that also builds the command line
(`mechbench <noun> <verb>`). `protocol(verb="push", args={"file": …,
"into": …})` is `mechbench protocol push FILE --into …`.

Why a tool per noun and not per verb: every tool's schema sits in an
agent's context on every turn. Fifty-odd verbs as fifty-odd tools, each
with its typed parameters, would cost that context for good; six tools
whose descriptions list their verbs in one line each cost a fraction of
it, and an argument the verb does not take is refused with the verb's
own list, so the shape is learned from the answer. The numbers are in
docs/CAPABILITIES.md.

Reads answer summaries unless `full`; listings answer `{items, next}`.
A refusal from the API (a deletion something depends on, a name taken,
a missing thing) comes back as data, `{"error": {status, code, …}}`, so
the caller can read its code and what stands in the way.

And the older in-process one:

  run_protocol(prompt, protocol_kind?, model_id?)
      Runs the protocol *in-process* via mechbench-compute and returns
      the LayerAblationPayload dict. Does not round-trip through the
      mechbench-api job queue — the MCP caller wants the answer, and
      we are the compute target. (The job-runner subcommand is the
      queued path for UI-triggered jobs.)

Stdio transport only for v0. SSE / HTTP transports when remote
deploy earns its seat (deferred explicitly in task 000185).
"""

from __future__ import annotations

from typing import Any, Literal

from mcp.server import MCPServer
from mechbench_compute.protocol import ProtocolExecutor, ProtocolSpec

from .config import Config
from .verbs import NOUNS, Ctx, Noun, VerbError, invoke, refusal


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

    ctx = Ctx(cfg)
    tools: dict[str, Any] = {"run_protocol": run_protocol}
    for noun in NOUNS:
        tools[noun.name] = noun_tool(ctx, noun)
    return tools


def describe(noun: Noun) -> str:
    """A noun tool's description: what it is, then one line per verb,
    `verb(arg, arg?)`, `?` marking the optional ones."""
    lines = [noun.help, "verb and its args:"]
    for v in noun.verbs:
        sig = ", ".join(a.name + ("" if a.required else "?") for a in v.args)
        lines.append(f"{v.name}({sig}): {v.help}")
    lines.append("Reads are summaries unless full; lists answer {items, next}; "
                 "delete is a dry run unless yes.")
    return "\n".join(lines)


def noun_tool(ctx: Ctx, noun: Noun) -> Any:
    """The tool for one noun: `verb`, one of its verbs, and `args`."""

    def tool(verb: str, args: dict[str, Any] | None = None) -> Any:
        try:
            return invoke(ctx, noun.name, verb, args)
        except VerbError as e:
            raise ValueError(str(e)) from None
        except Exception as e:
            body = refusal(e)
            if body is None:
                raise
            return {"error": body}

    tool.__name__ = noun.name
    tool.__qualname__ = noun.name
    tool.__doc__ = describe(noun)
    verbs = tuple(v.name for v in noun.verbs)
    tool.__annotations__ = {"verb": Literal[verbs], "args": dict[str, Any] | None,
                            "return": Any}
    return tool


def build_server(
    config: Config | None = None,
    executor: ProtocolExecutor | None = None,
) -> MCPServer:
    """Construct the MCP server (mcp 2.x, task 000298). The tool
    names, signatures and docstrings are the contract an agent sees."""
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
