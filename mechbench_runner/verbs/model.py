from __future__ import annotations

from .core import Arg, Noun, Verb

MODEL = Noun(
    "model",
    "A model compute could load, read from Hugging Face before anything runs.",
    (
        Verb(
            "model",
            "check",
            "Whether compute loads a Hugging Face repo, at what support level, its "
            "size, and its shape: layers, width, heads, key/value heads and which "
            "layers attend globally, read from its config.json as compute's loader "
            "reads it.",
            "GET /models/check",
            (Arg("repo", "The repo, owner/name.", required=True, positional=True),),
            lambda ctx, a: ctx.get("/models/check", repo=a["repo"]),
            "read",
            effect="read",
        ),
    ),
    absent={
        "list": "a model is Hugging Face's, not the platform's; the catalog of the ones "
                "verified here is GET /models/catalog",
        "read": "check reads it, by its repo",
        "create": "a model is published to Hugging Face, not made here",
        "update": "a model is Hugging Face's, not the platform's",
        "delete": "a model is Hugging Face's, not the platform's",
        "history": "a model is Hugging Face's; its revisions are its repo's commits",
    },
)
