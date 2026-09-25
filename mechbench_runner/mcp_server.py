from __future__ import annotations

from typing import Any, Literal

from mcp.server import MCPServer
from mechbench_compute.protocol import ProtocolExecutor, ProtocolSpec

from .config import Config
from .verbs import CONSENT, NOUNS, Ctx, Noun, Verb, VerbError, invoke, refusal


def build_tools(
    config: Config | None = None,
    executor: ProtocolExecutor | None = None,
) -> dict[str, Any]:
    cfg = config or Config.from_env()
    _executor = executor or ProtocolExecutor()

    def run_protocol(
        prompt: str,
        protocol_kind: str = "layer_ablation",
        model_id: str | None = None,
    ) -> dict[str, Any]:
        """Run an interpretability protocol in-process and return
        the structured result. `protocol_kind` defaults to
        layer_ablation. `model_id` falls back to
        MECHBENCH_WARM_MODEL_ID, and is required if that is unset."""
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
    lines = [noun.help, "verb and its args:"]
    for v in noun.verbs:
        sig = ", ".join(a.name + ("" if a.required else "?") for a in v.args)
        lines.append(f"{v.name}({sig}): {v.help}{consent(v)}")
    lines.append(
        "Reads are summaries unless full; lists answer {items, next}; "
        "delete is a dry run unless yes. [consent] marks a call to confirm "
        "with the person first: it spends, deletes or shows something "
        "to more people."
    )
    return "\n".join(lines)


def consent(v: Verb) -> str:
    return f" [consent: {v.effect_label()}]" if v.effect in CONSENT else ""


def noun_tool(ctx: Ctx, noun: Noun) -> Any:
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
    tool.__annotations__ = {
        "verb": Literal[verbs],
        "args": dict[str, Any] | None,
        "return": Any,
    }
    return tool


def build_server(
    config: Config | None = None,
    executor: ProtocolExecutor | None = None,
) -> MCPServer:
    server = MCPServer("mechbench")
    for fn in build_tools(config, executor).values():
        server.tool()(fn)
    return server


def run_stdio(config: Config | None = None) -> None:
    server = build_server(config)
    server.run(transport="stdio")


def _require_model(model_id: str | None, cfg: Config) -> str:
    resolved = model_id or cfg.warm_model_id
    if not resolved:
        raise ValueError(
            "model_id is required: pass one, or set MECHBENCH_WARM_MODEL_ID "
            "for this runner."
        )
    return resolved
