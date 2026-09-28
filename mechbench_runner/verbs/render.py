from __future__ import annotations

import pathlib
from typing import Any

from .core import Arg, Ctx, VerbError

FORMATS = ("text", "json", "svg", "png")

LINKED_PATH = Arg(
    "path",
    "Its path (owner/project/…), or the link to its page on mechbench.ai, "
    "whose view (step, theme) is drawn unless named here.",
    required=True, positional=True,
)
LINKED_ID = Arg(
    "id",
    "Its id, or the link to its page on mechbench.ai, whose view (selected "
    "block, theme) is drawn unless named here.",
    required=True, positional=True,
)

FORMAT = Arg(
    "format",
    "text (a reading: the axes, the marks in order, what the renderer "
    "could not draw), json (the same reading as data), svg, or png.",
    choices=FORMATS,
)
THEME = Arg("theme", "light (the default) or dark.", choices=("light", "dark"))
WIDTH = Arg("width", "The drawing's width in pixels.", type="int")
FULL_READING = Arg("full", "Every mark in the reading, not a summary.", type="bool")
OUT = Arg("out", "Write the svg or png here.", flag="-o", local=True)
LINK = Arg(
    "link",
    "With png: keep the picture in the figure's project (renders/) and "
    "answer its path and a link that needs no credential for 15 minutes.",
    type="bool",
)


def render_answer(ctx: Ctx, route: str, query: dict[str, Any], a: dict) -> Any:
    fmt = a.get("format") or "text"
    link = bool(a.get("link"))
    q = {k: v for k, v in {**query, "format": fmt, "theme": a.get("theme"),
                           "width": a.get("width"),
                           "full": True if a.get("full") else None,
                           "link": True if link else None}.items()
         if v is not None}
    body, _headers = ctx.api("GET", route, query=q)
    if fmt == "json" or link:
        return body
    if fmt == "text":
        return {"text": _text(body)}
    out = a.get("out")
    if fmt == "png" and not out:
        raise VerbError("a png is written to a file: pass --out (-o)")
    if out:
        data = body if isinstance(body, bytes) else _text(body).encode()
        pathlib.Path(out).write_bytes(data)
        return {"written": out, "bytes": len(data), "format": fmt}
    return {"text": _text(body)}


def _text(body: Any) -> str:
    if isinstance(body, bytes):
        return body.decode()
    return str(body)
