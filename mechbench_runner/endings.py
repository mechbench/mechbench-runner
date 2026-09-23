"""The line a reader of a result must not miss: how many generated
items did not end naturally.

`text/generate` and `text/chat` count their items by ending in the
header's `ended` (`end`, `stop`, `max_tokens`, `tool_call`, `filtered`,
`empty`, `other`). A non-zero count of anything but `end` and `stop` is
said in words wherever a result is read — the command line and the MCP
tool — so a corpus cut off at `max_tokens` is noticed the day it runs.
A header without `ended` was stored before the count existed and says
nothing either way.
"""

from __future__ import annotations

from typing import Any

#: The endings that are not a natural finish, in the order they are said.
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
    """One line per generation node in `payload` whose items did not all
    end naturally: a node's own output, or a job result, read from its
    manifest's `node_summaries` (every executed node) and then its
    `outputs` for any node the summaries do not name."""
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
