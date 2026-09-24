from __future__ import annotations

import re
import tomllib
from pathlib import Path

PROJECT = tomllib.loads(
    (Path(__file__).resolve().parent.parent / "pyproject.toml").read_text()
)["project"]


def _name(req: str) -> str:
    return re.split(r"[<>=!~\[; ]", req, maxsplit=1)[0]


def test_requires_python_is_capped_because_uv_managed_python_takes_the_newest():
    assert "<" in PROJECT["requires-python"]


def test_third_party_dependencies_are_capped_and_our_own_are_floor_only():
    for req in PROJECT["dependencies"]:
        name = _name(req)
        if name.startswith("mechbench-") or name == "certifi":
            assert "<" not in req, req
        else:
            assert "<" in req, req
