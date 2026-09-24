from __future__ import annotations

import ast
import io
import re
import tokenize
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CODE_DIRS = ("mechbench", "mechbench_runner", "scripts", "tests")
SKIP_DIRS = {".git", ".venv", "dist", "build", "__pycache__", ".pytest_cache",
             ".ruff_cache"}
TEXT_SUFFIXES = {".py", ".md", ".toml", ".txt", ".json", ".yml", ".yaml", ".cfg"}
NOT_SCANNED_FOR_IDS = {"CHANGELOG.md", "uv.lock"}

DIRECTIVE = re.compile(
    r"#\s*(noqa\b|type:\s*ignore\b|pragma:|pylint:|ruff:|fmt:|mypy:|isort:|nosec\b"
    r"|external:\s*\S)"
)
TASK_ID = re.compile(r"(?<![0-9A-Za-z_])[0-9]{6}(?![0-9])")

READ_AT_RUN_TIME = {
    ("mechbench_runner/mcp_server.py", "build_tools.run_protocol"):
        "registered as an MCP tool; the server sends it as the description",
}


def _code_files() -> list[Path]:
    return sorted(p for d in CODE_DIRS for p in (ROOT / d).rglob("*.py")
                  if not SKIP_DIRS & set(p.parts))


def _text_files() -> list[Path]:
    out = []
    for p in ROOT.rglob("*"):
        rel = p.relative_to(ROOT)
        if (p.is_file() and p.suffix in TEXT_SUFFIXES
                and not SKIP_DIRS & set(rel.parts)
                and not any(part.endswith(".egg-info") for part in rel.parts)
                and p.name not in NOT_SCANNED_FOR_IDS):
            out.append(p)
    return sorted(out)


def _docstrings(tree: ast.AST):
    def walk(node, prefix):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                name = f"{prefix}{child.name}"
                if ast.get_docstring(child, clean=False) is not None:
                    yield name, child.body[0].lineno
                yield from walk(child, name + ".")
            else:
                yield from walk(child, prefix)

    if ast.get_docstring(tree, clean=False) is not None:
        yield "<module>", 1
    yield from walk(tree, "")


def test_only_directives_and_external_facts_are_comments():
    found = []
    for p in _code_files():
        src = p.read_text()
        for tok in tokenize.generate_tokens(io.StringIO(src).readline):
            if tok.type != tokenize.COMMENT:
                continue
            if tok.start[0] == 1 and tok.string.startswith("#!"):
                continue
            if not DIRECTIVE.match(tok.string) or (
                    "external:" not in tok.string
                    and re.search(r"\s(—|--)\s", tok.string)):
                found.append(f"{p.relative_to(ROOT)}:{tok.start[0]}: {tok.string}")
    assert not found, "comments that are not directives:\n" + "\n".join(found)


def test_only_docstrings_read_at_run_time_exist():
    found = []
    for p in _code_files():
        rel = str(p.relative_to(ROOT))
        for name, line in _docstrings(ast.parse(p.read_text())):
            if (rel, name) not in READ_AT_RUN_TIME:
                found.append(f"{rel}:{line}: {name}")
    assert not found, "docstrings nothing reads:\n" + "\n".join(found)


def test_the_kept_docstrings_are_still_read():
    from mechbench_runner.config import Config
    from mechbench_runner.mcp_server import build_tools

    cfg = Config(api_base_url="http://127.0.0.1:1", api_key="k",
                 poll_interval_seconds=1.0, warm_model_id=None, runner_id=None)
    tools = build_tools(cfg, executor=object())
    assert tools["run_protocol"].__doc__


def test_no_private_task_ids_outside_the_changelog():
    found = []
    for p in _text_files():
        for n, line in enumerate(p.read_text(errors="replace").splitlines(), 1):
            if TASK_ID.search(line):
                found.append(f"{p.relative_to(ROOT)}:{n}: {line.strip()}")
    assert not found, "six-digit task ids:\n" + "\n".join(found)
