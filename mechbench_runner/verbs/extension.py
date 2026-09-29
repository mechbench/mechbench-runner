from __future__ import annotations

import hashlib
import json
import os
import pathlib
import re
import subprocess
import sys
import tempfile
import tomllib
from collections.abc import Mapping
from typing import Any
from urllib.parse import quote

from .. import install as install_mod
from .core import (
    LIMIT,
    OFFSET,
    SEARCH,
    WIDER,
    Arg,
    Ctx,
    Noun,
    Verb,
    VerbError,
    paged,
    unwrap,
)

GROUP = "mechbench.extensions"
NAME = re.compile(r"^[a-z][a-z0-9-]{0,62}$")
LEAF = re.compile(r"^[a-z][a-z0-9_]*$")
PLATFORM_FIELDS = ("state", "visibility", "party", "flags", "approved", "promoted", "conformance")
AUDIENCE = {
    "private": "only you",
    "org": "the owning org's members",
    "public": "everyone",
}
BUILD_TIMEOUT_SECONDS = 600.0
TEST_TIMEOUT_SECONDS = 1800.0


def compute_release() -> str:
    try:
        from mechbench_compute import __version__

        return __version__.split("+", 1)[0]
    except Exception:  # noqa: BLE001
        return "0.169.0"


def module_of(name: str) -> str:
    return name.replace("-", "_")


PYPROJECT = """\
[project]
name = "mechbench-ext-{name}"
version = "0.1.0"
description = "{owner}/{project}'s {name}: mechbench operations."
requires-python = ">=3.12"
dependencies = ["mechbench-compute>={compute}"]

[project.entry-points."mechbench.extensions"]
{name} = "{module}:MANIFEST"

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["{module}"]

[tool.hatch.build.targets.sdist]
include = ["{module}", "inputs", "README.md"]
"""

INIT = '''\
from mechbench_compute.api import Extension, Package

MANIFEST = Extension(
    name="{owner}/{project}",
    extension="{name}",
    version=1,
    module=__name__,
    package=Package("mechbench-ext-{name}"),
    min_compute="{compute}",
)
'''

OP_FILE = '''\
from mechbench_compute.api import In, Op, Output, P, collection, items_of

OP = Op(
    name="{op}",
    summary="Count the records it reads, as one record.",
    description="""\\
A starting point: replace the params, ports, output and `run` with your
operation's. `extension test` checks the declaration and runs the
example twice; `extension push --draft` makes it usable by you at once.
""",
    params=(P("label", "string", "The id of the record it writes.", "count"),),
    inputs=(In("records", "records/record", "The records to count.", many=True),),
    output=Output("records/record", collection=True, doc="One record: its id and the count."),
    example={{"label": "count"}},
    example_inputs={{"records": collection("records/record", [{{"id": "1"}}, {{"id": "2"}}])}},
)


def run(ctx, inputs, params):
    items = items_of(inputs["records"])
    return collection("records/record", [{{"id": params.get("label", "count"), "count": len(items)}}])
'''

README = """\
# {owner}/{project}/extensions/{name}

mechbench operations: `mechbench extension test .` checks them, `mechbench extension push . --draft` publishes a draft.
"""


def leaf_is_verb(leaf: str) -> bool:
    try:
        from mechbench_compute.conformance.names import is_verb_first
    except ImportError:
        return True
    return is_verb_first(leaf)


def scaffold(a: dict) -> dict[str, Any]:
    owner, _, project = str(a["scope"]).partition("/")
    if not owner or not project or "/" in project:
        raise VerbError("scope is <owner>/<project>")
    name = str(a["name"])
    if not NAME.match(name):
        raise VerbError(f"name {name!r}: lowercase letters, digits and hyphens, starting with a letter")
    family, _, leaf = str(a["op"]).partition("/")
    if not LEAF.match(family) or not LEAF.match(leaf) or "/" in leaf:
        raise VerbError("op is <family>/<leaf>: lowercase words")
    if not leaf_is_verb(leaf):
        raise VerbError(f"{leaf!r} does not start with a verb; an operation's leaf is one (count, not tally)")
    root = pathlib.Path(a.get("dir") or name)
    if root.exists() and any(root.iterdir()):
        raise VerbError(f"{root} is not empty")
    module = module_of(name)
    fill = {
        "owner": owner, "project": project, "name": name, "module": module,
        "compute": compute_release(), "op": f"{family}/{leaf}",
    }
    files = {
        "pyproject.toml": PYPROJECT.format(**fill),
        "README.md": README.format(**fill),
        f"{module}/__init__.py": INIT.format(**fill),
        f"{module}/ops/__init__.py": "",
        f"{module}/ops/{family}/__init__.py": "",
        f"{module}/ops/{family}/{leaf}.py": OP_FILE.format(**fill),
        f"{module}/kinds/__init__.py": "",
    }
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    return {
        "dir": str(root),
        "address": f"{owner}/{project}/extensions/{name}",
        "op": f"{owner}/{project}/ops/{family}/{leaf}",
        "files": sorted(files),
        "next": f"mechbench extension test {root}",
    }


def read_package(root: pathlib.Path) -> dict[str, Any]:
    pyproject = root / "pyproject.toml"
    if not pyproject.is_file():
        raise VerbError(f"{root} has no pyproject.toml")
    data = tomllib.loads(pyproject.read_text())
    points = ((data.get("project") or {}).get("entry-points") or {}).get(GROUP) or {}
    if len(points) != 1:
        raise VerbError(f"{pyproject} names {len(points)} entry points in {GROUP}; one names the manifest")
    ((point, target),) = points.items()
    return {"distribution": (data.get("project") or {}).get("name"), "point": point, "target": target}


def python_of(a: Mapping[str, Any]) -> str:
    return str(a.get("python") or sys.executable)


def env_for(root: pathlib.Path) -> dict[str, str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join([str(root.resolve()), *filter(None, [env.get("PYTHONPATH")])])
    return env


COMPILE = (
    "import importlib, json, sys\n"
    "mod, _, attr = sys.argv[1].partition(':')\n"
    "ext = getattr(importlib.import_module(mod), attr or 'MANIFEST')\n"
    "d = ext.to_dict()\n"
    "print(json.dumps({'manifest': d, 'needs_model': any(n.startswith(('model.', 'runtime.mlx')) "
    "for n in d.get('needs') or [])}))\n"
)


def compile_manifest(root: pathlib.Path, a: Mapping[str, Any]) -> dict[str, Any]:
    pkg = read_package(root)
    proc = subprocess.run(
        [python_of(a), "-c", COMPILE, pkg["target"]],
        capture_output=True, text=True, env=env_for(root), check=False, timeout=300,
    )
    if proc.returncode != 0:
        raise VerbError(f"its declarations do not compile: {tail(proc)}")
    return {**pkg, **json.loads(proc.stdout.strip().splitlines()[-1])}


def tail(proc: subprocess.CompletedProcess, lines: int = 15) -> str:
    return "\n".join(((proc.stderr or "") + (proc.stdout or "")).strip().splitlines()[-lines:])


def extension_test(ctx: Ctx, a: dict) -> Any:
    root = pathlib.Path(a["dir"])
    compiled = compile_manifest(root, a)
    cmd = [python_of(a), "-m", "mechbench_compute.conformance", compiled["target"]]
    inputs = root / "inputs"
    if inputs.is_dir():
        cmd += ["--inputs", str(inputs)]
    warm = getattr(ctx.config, "warm_model_id", None)
    model = bool(a.get("model")) or bool(compiled["needs_model"] and warm)
    if model:
        cmd.append("--model")
    proc = subprocess.run(
        cmd, capture_output=True, text=True, env=env_for(root), check=False,
        timeout=TEST_TIMEOUT_SECONDS,
    )
    if proc.returncode not in (0, 1):
        raise VerbError(f"the conformance run did not finish: {tail(proc)}")
    try:
        report = json.loads(proc.stdout)
    except ValueError:
        raise VerbError(f"the conformance run printed no report: {tail(proc)}") from None
    out = {"command": " ".join(cmd), **report}
    if compiled["needs_model"] and not model:
        out["note"] = "an op needs a model and none was offered: its example was skipped; pass model"
    return out


def build_sdist(root: pathlib.Path, out: pathlib.Path, a: Mapping[str, Any]) -> pathlib.Path:
    uv = install_mod.find_executable("uv")
    if uv:
        cmd = [uv, "build", "--sdist", "--out-dir", str(out), str(root)]
    else:
        cmd = [python_of(a), "-m", "build", "--sdist", "--outdir", str(out), str(root)]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=BUILD_TIMEOUT_SECONDS)
    if proc.returncode != 0:
        raise VerbError(f"the sdist did not build ({' '.join(cmd)}): {tail(proc)}")
    built = sorted(out.glob("*.tar.gz"))
    if len(built) != 1:
        raise VerbError(f"the build left {len(built)} sdists in {out}")
    return built[0]


def address_of(text: str) -> tuple[str, str, str, str]:
    base, at, ref = str(text).strip("/").partition("@")
    parts = base.split("/")
    if len(parts) == 4 and parts[2] == "extensions":
        parts = [parts[0], parts[1], parts[3]]
    if len(parts) != 3 or not all(parts):
        raise VerbError(f"{text!r} is not an extension's address: <owner>/<project>/extensions/<name>[@<n>]")
    return parts[0], parts[1], parts[2], (at + ref if at else "")


def version_route(text: str, needs_version: bool = False) -> str:
    owner, project, name, ref = address_of(text)
    if needs_version and not ref:
        raise VerbError(f"{text!r} names no version: add @<n>")
    return f"/extensions/{quote(owner)}/{quote(project)}/extensions/{quote(name)}{quote(ref, safe='@:')}"


def visibility_now(ctx: Ctx, owner: str, project: str, name: str) -> str | None:
    try:
        got = ctx.get(f"/extensions/{quote(owner)}/{quote(project)}/extensions/{quote(name)}")
    except Exception as e:  # noqa: BLE001
        if getattr(e, "status", None) == 404:
            return None
        raise
    manifest = got.get("manifest") if isinstance(got, Mapping) else None
    return str((manifest or {}).get("visibility") or "private")


def push_consent(visibility: str | None, draft: bool) -> str:
    vis = visibility or "private"
    who = AUDIENCE.get(vis, vis)
    if draft:
        return f"a draft: usable by you at once, shown to no one else (the extension is {vis})"
    if vis == "private":
        return "verification queued; verified, it stays private: shown to you only"
    return f"verification queued; verified, it is shown to {who} ({vis})"


def body_of(manifest: Mapping[str, Any], sdist_ref: str) -> dict[str, Any]:
    body = {k: v for k, v in manifest.items() if k not in PLATFORM_FIELDS}
    body["package"] = {**dict(manifest.get("package") or {}), "sdist": sdist_ref}
    prov = {k: v for k, v in dict(body.get("provenance") or {}).items() if k == "source"}
    body["provenance"] = prov
    return body


def local_hash(body: Mapping[str, Any]) -> str | None:
    try:
        from mechbench_compute.lexicon.extension import hash_extension
    except ImportError:
        return None
    return hash_extension(dict(body))


def extension_push(ctx: Ctx, a: dict) -> Any:
    root = pathlib.Path(a["dir"])
    compiled = compile_manifest(root, a)
    manifest = compiled["manifest"]
    owner, project, name = manifest["owner"], manifest["project"], manifest["name"]
    with tempfile.TemporaryDirectory(prefix="mechbench-sdist-") as tmp:
        sdist = build_sdist(root, pathlib.Path(tmp), a)
        data = sdist.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    path = f"{owner}/{project}/extensions/{name}/sdist/{digest[:16]}"
    stored = ctx.put_bytes(path, data, kind="sdist")
    got = str(stored.get("hash") or "")
    if got != f"sha256:{digest}":
        raise VerbError(f"{path} was stored as {got or 'no hash'}, not sha256:{digest}")
    before = visibility_now(ctx, owner, project, name)
    body = body_of(manifest, f"~hash/sha256:{digest}")
    answer = ctx.api("PUT", f"/extensions/{quote(owner)}/{quote(project)}/{quote(name)}", body=body)[0]
    mine = local_hash(body)
    address = str(answer.get("address") or f"{owner}/{project}/extensions/{name}")
    out: dict[str, Any] = {
        "address": address,
        "version": answer.get("version"),
        "hash": answer.get("hash"),
        "pin": f"{address}@{answer.get('hash')}",
        "state": answer.get("state"),
        "visibility": answer.get("visibility") or before or "private",
        "sdist": {"path": path, "ref": f"~hash/sha256:{digest}", "bytes": len(data), "file": sdist.name},
    }
    if mine and answer.get("hash") and mine != answer.get("hash"):
        out["warning"] = f"the platform pinned {answer.get('hash')}; this machine's compute computes {mine}"
    for k in ("idempotent", "supersedes"):
        if answer.get(k) is not None:
            out[k] = answer[k]
    draft = bool(a.get("draft"))
    out["consent"] = push_consent(out["visibility"], draft)
    if not draft:
        v = ctx.api("POST", f"{version_route(address)}@{answer.get('version')}/verify")[0]
        out["verify"] = {k: v.get(k) for k in ("state", "job", "waitingFor") if isinstance(v, Mapping)}
    return out


def extension_list(ctx: Ctx, a: dict) -> Any:
    data = ctx.get(
        "/extensions",
        owner=a.get("owner"), state=a.get("state"), reads=a.get("reads"),
        emits=a.get("emits"), q=a.get("search"),
    )
    items = unwrap(data, "extensions")
    return paged(items if isinstance(items, list) else [], a)


def extension_history(ctx: Ctx, a: dict) -> Any:
    got = ctx.get(version_route(str(a["address"]).partition("@")[0]))
    if not isinstance(got, Mapping):
        return got
    return {"address": got.get("address"), "versions": got.get("versions") or [], "usage": got.get("usage")}


ADDRESS = Arg(
    "address",
    "`<owner>/<project>/extensions/<name>`, optionally `@<n>` or `@sha256:<hex>`.",
    required=True,
    positional=True,
)
VERSIONED = Arg(
    "address",
    "`<owner>/<project>/extensions/<name>@<n>`: the version.",
    required=True,
    positional=True,
)
DIR = Arg("dir", "The package's directory (its pyproject.toml).", required=True, positional=True, local=True)
PYTHON = Arg(
    "python",
    "The interpreter whose environment has the package's dependencies (default: this one).",
    local=True,
)

EXTENSION = Noun(
    "extension",
    "An extension: one project's operations, kinds and marks with the package that "
    "runs them, pushed as versions and verified. There is no install verb: a "
    "machine installs what a claimed job needs, under its policy.",
    (
        Verb(
            "extension",
            "new",
            "Scaffold a package on this machine: pyproject.toml with the entry point, "
            "the manifest, one op (its declaration and run), an empty kinds/, a README. "
            "It passes `extension test` as written.",
            "",
            (
                Arg("scope", "<owner>/<project>.", required=True, positional=True),
                Arg("name", "The extension's name (lowercase, hyphens).", required=True),
                Arg("op", "Its first op, <family>/<leaf>.", required=True),
                Arg("dir", "Where to write it (default: ./<name>).", local=True),
            ),
            lambda _ctx, a: scaffold(a),
            effect="read",
            local=True,
        ),
        Verb(
            "extension",
            "test",
            "Check a package on this machine: compute's conformance over its "
            "declarations and each op's example, run twice; inputs from <dir>/inputs "
            "when there is one.",
            "",
            (
                DIR,
                Arg(
                    "model",
                    "A model is available here: run the ops that need one too "
                    "(on by itself when MECHBENCH_WARM_MODEL_ID is set).",
                    type="bool",
                ),
                PYTHON,
            ),
            extension_test,
            "read",
            effect="read",
            local=True,
        ),
        Verb(
            "extension",
            "push",
            "Build its sdist, store it by hash, compile its declarations and push "
            "them as a version; without draft, ask for it to be verified.",
            "PUT /extensions/:owner/:project/:name",
            (
                DIR,
                Arg("draft", "Push a draft, usable by you at once, and verify nothing.", type="bool"),
                PYTHON,
            ),
            extension_push,
            effect="outward",
            local=True,
        ),
        Verb(
            "extension",
            "verify",
            "Ask for a version to be verified: queues a job on your runner, and says "
            "what it waits for.",
            "POST /extensions/:owner/:project/extensions/:ref/verify",
            (VERSIONED,),
            lambda ctx, a: ctx.api("POST", version_route(a["address"], True) + "/verify")[0],
            effect="draft",
        ),
        Verb(
            "extension",
            "list",
            "Extensions you may see: your own at their newest version, others' at "
            "their newest verified.",
            "GET /extensions",
            (
                Arg("owner", "Only this handle's."),
                Arg("state", "Only versions in this state.",
                    choices=("draft", "pending", "verified", "withdrawn")),
                Arg("reads", "Only those with an op that takes this kind."),
                Arg("emits", "Only those with an op that gives this kind."),
                SEARCH,
                LIMIT,
                OFFSET,
            ),
            extension_list,
            "list",
            ("address", "version", "state", "visibility", "ops"),
            effect="read",
        ),
        Verb(
            "extension",
            "read",
            "One version (the newest you would use by default): its manifest, pin "
            "hash, the versions you may see, and what uses it.",
            "GET /extensions/:owner/:project/extensions/:ref",
            (ADDRESS,),
            lambda ctx, a: ctx.get(version_route(a["address"])),
            "read",
            effect="read",
        ),
        Verb(
            "extension",
            "history",
            "Its versions, with their state, hash and date.",
            "GET /extensions/:owner/:project/extensions/:ref",
            (ADDRESS,),
            extension_history,
            "read",
            effect="read",
        ),
        Verb(
            "extension",
            "withdraw",
            "Withdraw a version: no new pins, every result kept.",
            "POST /extensions/:owner/:project/extensions/:ref/withdraw",
            (VERSIONED, Arg("reason", "Why, for the audit log.", required=True)),
            lambda ctx, a: ctx.api(
                "POST", version_route(a["address"], True) + "/withdraw", body={"reason": a["reason"]}
            )[0],
            effect="delete",
        ),
        Verb(
            "extension",
            "visibility",
            "Change who sees the extension, at a verified version for public.",
            "PUT /extensions/:owner/:project/extensions/:ref/visibility",
            (
                VERSIONED,
                Arg("visibility", "Who sees it.", required=True, positional=True,
                    choices=("private", "org", "public")),
            ),
            lambda ctx, a: ctx.api(
                "PUT", version_route(a["address"], True) + "/visibility",
                body={"visibility": a["visibility"]},
            )[0],
            effect="outward",
            consent_when=WIDER,
        ),
    ),
    absent={
        "create": "push is its create: a package is pushed, by its manifest's name",
        "update": "a version never changes: push the next; visibility and withdraw "
                  "are the platform's fields",
        "delete": "a version is withdrawn, never deleted, so what ran on it stays readable",
    },
)
