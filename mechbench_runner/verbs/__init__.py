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
