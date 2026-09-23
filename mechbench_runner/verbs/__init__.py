"""Every noun an agent works with, and its verbs, declared once (task 000661).

The command line (`mechbench <noun> <verb>`, built in `verbs_cli.py`) and
MCP (one tool per noun, the verb its argument, in `mcp_server.py`) are
both generated from `NOUNS`, so a verb is on both or on neither; the
parity check in `tests/test_parity.py` holds that, holds each verb's API
route to mechbench-api's source, and holds `docs/CAPABILITIES.md` to what
is declared here.

A verb is thin. What it does is the API's: a route, its query and its
body. The verbs for which the bench library already has the call (launch,
push, export, publish, copy, cancel, delete, history, result, emit)
go through it, so there is one client for each; the rest go through
`ApiClient.call`. Nothing here decides anything the server decides.

Reads are summaries unless `full` is asked for: the API's `view=summary`,
which leaves out a protocol's graph, a job's spec and an article's body.
Listings take `search`, `limit` and `offset`, and answer `{items, next}`,
where `next` is the offset of the following page (the API's
`X-Next-Offset`) or None on the last.

Deletion is permanent. `delete` says what it would do unless `yes` is
given, and refuses what the API refuses (a dependant, a running job, an
article citing it until `acknowledge_citations`).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .article import ARTICLE
from .core import (
    LIFECYCLE,
    Arg,
    Ctx,
    Noun,
    Verb,
    VerbError,
    invoke_on,
    refusal,
)
from .dataset import DATASET
from .object import OBJECT
from .project import PROJECT, project_id
from .protocol import PROTOCOL, split_version
from .run import RUN, job_of

NOUNS: tuple[Noun, ...] = (OBJECT, PROTOCOL, RUN, ARTICLE, DATASET, PROJECT)


def noun(name: str) -> Noun:
    for n in NOUNS:
        if n.name == name:
            return n
    raise KeyError(name)


def invoke(
    ctx: Ctx, noun_name: str, verb_name: str, args: Mapping[str, Any] | None
) -> Any:
    """Run one verb by its noun's and its own name, with its arguments by
    name: the MCP tools' door, and the command line's."""
    return invoke_on(ctx, noun(noun_name), verb_name, args)


__all__ = [
    "LIFECYCLE",
    "NOUNS",
    "Arg",
    "Ctx",
    "Noun",
    "Verb",
    "VerbError",
    "invoke",
    "job_of",
    "noun",
    "project_id",
    "refusal",
    "split_version",
]
