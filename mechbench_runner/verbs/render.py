from __future__ import annotations

import pathlib
from typing import Any

from .core import Arg, Ctx, VerbError

FORMATS = ("text", "json", "svg", "png")

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


def render_answer(ctx: Ctx, route: str, query: dict[str, Any], a: dict) -> Any:
    fmt = a.get("format") or "text"
    q = {k: v for k, v in {**query, "format": fmt, "theme": a.get("theme"),
                           "width": a.get("width"),
                           "full": True if a.get("full") else None}.items()
         if v is not None}
    body, _headers = ctx.api("GET", route, query=q)
    if fmt == "json":
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
