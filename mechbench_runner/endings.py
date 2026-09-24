from __future__ import annotations

from typing import Any

NOTICED = ("max_tokens", "empty", "filtered", "tool_call", "other")

_WORDS = {"max_tokens": "cut off at max_tokens", "empty": "empty",
          "filtered": "filtered by the provider",
          "tool_call": "ended on an unanswered tool call",
          "other": "ended for another reason (see metadata.call.stop_reason)"}


def _one(label: str | None, payload: Any) -> str | None:
    ended = payload.get("ended") if isinstance(payload, dict) else None
    if not isinstance(ended, dict):
        return None
    said = [f"{int(ended[k])} {_WORDS[k]}" for k in NOTICED if ended.get(k)]
    if not said:
        return None
    total = sum(int(v) for v in ended.values() if isinstance(v, (int, float)))
    where = f"{label}: " if label else ""
    return f"{where}of {total} items, " + ", ".join(said)


def ended_notes(payload: Any, node: str | None = None) -> list[str]:
    notes = [n for n in [_one(node, payload)] if n]
    if not isinstance(payload, dict):
        return notes
    seen: set[str] = set()
    for field in ("node_summaries", "outputs"):
        nodes = payload.get(field)
        if not isinstance(nodes, dict):
            continue
        for name, value in nodes.items():
            if name in seen:
                continue
            seen.add(name)
            note = _one(name, value)
            if note:
                notes.append(note)
    return notes


def with_notice(payload: Any, node: str | None = None) -> Any:
    notes = ended_notes(payload, node)
    if notes and isinstance(payload, dict):
        return {"ended_notice": notes, **payload}
    return payload
