from __future__ import annotations

from typing import Any

LONG_LIST = 32
KEEP = 8


def shorten_numbers(value: Any) -> Any:
    if isinstance(value, list):
        if len(value) >= LONG_LIST and all(
                isinstance(x, (int, float)) and not isinstance(x, bool) for x in value):
            return {"first": value[:KEEP], "count": len(value),
                    "shortened": "pass full: true for every value"}
        return [shorten_numbers(x) for x in value]
    if isinstance(value, dict):
        return {k: shorten_numbers(v) for k, v in value.items()}
    return value


def summarize_push(answer: Any) -> Any:
    if not isinstance(answer, dict) or not isinstance(answer.get("protocol"), dict):
        return answer
    protocol = answer["protocol"]
    out = {k: v for k, v in protocol.items()
           if v is None or isinstance(v, (str, int, float, bool))}
    graph = protocol.get("graph")
    if isinstance(graph, dict):
        out["graph"] = {"nodes": len(graph.get("nodes") or []),
                        "edges": len(graph.get("edges") or [])}
    signature = protocol.get("signature")
    if isinstance(signature, dict):
        out["signature"] = {part: [x.get("name") for x in signature.get(part) or []
                                   if isinstance(x, dict)]
                            for part in ("params", "inputs", "outputs")}
    return {**answer, "protocol": out, "shortened": "pass full: true for the whole protocol"}
