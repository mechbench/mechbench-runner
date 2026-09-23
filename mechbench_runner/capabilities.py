"""What is on one surface and not the others, and why (task 000661).

The nouns' verbs (`verbs/`) are on the command line and MCP alike by
construction, and each names its API route. Everything else a surface
has is listed here with its reason, and `tests/test_parity.py` fails on
a command, a tool or a lifecycle gap that is in neither place.

`table()` renders the matrix `docs/CAPABILITIES.md` carries between its
markers, and `docs_page()` the docs site's page; `scripts/capabilities.py`
writes both, and the parity check fails when either is stale.
"""

from __future__ import annotations

from .verbs import LIFECYCLE, NOUNS

#: Bare commands that are a noun's verb under a shorter name: the
#: commonest, kept from before the nouns, with the arguments they had.
ALIASES: dict[str, str] = {
    "run": "run launch (`mechbench run PROTOCOL`); with no PROTOCOL, the runner loop",
    "runs": "run list",
    "label": "run update",
    "watch": "run watch (several runs at once)",
    "result": "run result (`JOB/NODE`, or found by `--protocol --bind`)",
    "cancel": "run cancel (several runs at once)",
    "delete": "`<noun> delete`, the noun read from the id's prefix or a path",
    "history": "`<noun> history`, by kind and id",
}

#: What this machine's runner does for itself: the command line only,
#: because MCP and the API are the platform's and these are the machine's.
MACHINE = (
    "this machine's runner, not the platform: an agent reaches the "
    "platform, and the person at the machine runs its service"
)
CLI_ONLY: dict[str, str] = {
    name: MACHINE
    for name in (
        "login",
        "logout",
        "whoami",
        "doctor",
        "models",
        "budget",
        "update",
        "supervise",
        "install-service",
        "uninstall-service",
        "service-status",
        "status",
        "pause",
        "resume",
        "restart",
        "smoke",
    )
}
CLI_ONLY["mcp"] = "starts the MCP server itself"

MCP_ONLY: dict[str, str] = {
    "run_protocol": (
        "runs a built-in kind in-process on the machine serving MCP; "
        "the queued, recorded way to run is `run launch` on every surface"
    ),
}

#: What the API does that the command line and MCP do not, and why.
API_ONLY: dict[str, str] = {
    "kinds (`GET/PUT /kinds/:path`)": "registered by compute releases, not by agents",
    "object lineage and inventory (`~lineage`, `~inventory`)": (
        "read through `bench.lineage` and the UI; `object list` and `object read` "
        "are the agent's discovery"
    ),
    "checkpoint files (`PUT /objects/:path` bytes)": (
        "written and read by the runner's own jobs"
    ),
    "a protocol's changelog, dependencies and citations": (
        "the composer's publish review; `protocol versions` and `protocol history` "
        "are the agent's record"
    ),
    "article versions, restore, delta, media, comments": (
        "the collaborative editor's; markdown writes and article versions for "
        "agents are epic 000525"
    ),
    "dataset upload (`POST /datasets`, multipart)": (
        "`object write` then `dataset create` names the stored object as one"
    ),
    "project transfer, members and audit": "an owner's administration, in the UI",
    "runners (`GET /runners`, `PATCH`, `DELETE`, commands)": (
        "the machines page; this machine's own are its command-line-only commands"
    ),
    "spend total": (
        "no total exists on any surface yet; spend is per run (`run list`, `run read`)"
    ),
}


def surface_names(noun: str, verb: str) -> tuple[str, str]:
    """`(cli, mcp)`: `mechbench protocol push`, `protocol(verb="push")`."""
    return f"mechbench {noun} {verb}", f'{noun}(verb="{verb}")'


def table() -> str:
    """Every noun's verbs on each surface, and its lifecycle gaps with why."""
    out = ["| Noun | Verb | API | MCP | CLI |", "|---|---|---|---|---|"]
    for n in NOUNS:
        for v in n.verbs:
            cli, mcp = surface_names(n.name, v.name)
            args = ", ".join(a.name + ("" if a.required else "?") for a in v.args)
            out.append(
                f"| {n.name} | **{v.name}**({args}) | `{v.api}` | `{mcp}` | `{cli}` |"
            )
        for verb in LIFECYCLE:
            if verb in n.absent:
                out.append(f"| {n.name} | {verb} | — | — | — ({n.absent[verb]}) |")
    return "\n".join(out)


def one_surface() -> str:
    """The commands, tools and routes on one surface only, with why."""
    lines = ["**Command line only.**", ""]
    by_reason: dict[str, list[str]] = {}
    for k, why in CLI_ONLY.items():
        by_reason.setdefault(why, []).append(f"`mechbench {k}`")
    lines += [f"- {', '.join(names)}: {why}." for why, names in by_reason.items()]
    lines += [
        "",
        "**Shorter names for a noun's verb** (the command line keeps them):",
        "",
    ]
    lines += [f"- `mechbench {k}`: {v}." for k, v in ALIASES.items()]
    lines += ["", "**MCP only.**", ""]
    lines += [f"- `{k}`: {why}." for k, why in MCP_ONLY.items()]
    lines += ["", "**API only.**", ""]
    lines += [f"- {k}: {why}." for k, why in API_ONLY.items()]
    return "\n".join(lines)


BEGIN = "<!-- verbs:begin (scripts/capabilities.py writes this) -->"
END = "<!-- verbs:end -->"


def generated() -> str:
    return "\n\n".join([BEGIN, table(), one_surface(), END])


def splice(doc: str) -> str:
    """`doc` with its generated part replaced by the current one."""
    head, sep, rest = doc.partition(BEGIN)
    if not sep:
        raise ValueError("no verbs:begin marker")
    _, sep, tail = rest.partition(END)
    if not sep:
        raise ValueError("no verbs:end marker")
    return head + generated() + tail


SUMMARY = (
    "Every noun an agent works with (objects, protocols, runs, articles, "
    "datasets, projects) and its verbs, spelled on the command line, over MCP "
    "and on the API, with what each surface leaves out and why."
)

PAGE_HEAD = (
    "---\ntitle: Verbs on every surface\nsummary: "
    + SUMMARY
    + "\nsection: Start here\norder: 13\n---\n"
    + """
# Verbs on every surface

<!-- Generated from mechbench-runner's verb registry
(mechbench_runner/verbs/) by its scripts/capabilities.py. Edit the
registry, not this page. -->

Every thing an agent works with is a noun, and every noun has the same
lifecycle: **list** (with `search`, `limit` and `offset`), **read**,
**create**, **update**, **delete** and **history**, on the API, over MCP
and on the command line. One verb is designed once and spelled on each:

- **Command line:** `mechbench <noun> <verb>`, the arguments as flags
  (`--label-contains`) or positionals.
- **MCP:** one tool per noun, `<noun>(verb, args)`, the arguments by the
  same names: `protocol(verb="push", args={"file": "draws.json",
  "into": "benji/lab"})`.
- **API:** the resource and its action, `POST /protocols/push`.

**Reads are summaries** unless asked for in full (`--full`, `full: true`,
`?view=full`): a protocol without its graph, a run without its job's
spec, an article without its body, an object as its header (kind, size,
hash, item count). **Listings** answer `{items, next}` on the command
line and MCP, where `next` is the offset of the next page; the API sends
it as `X-Next-Offset`.

**Deletion is permanent**, and nothing restores what is deleted. `delete`
answers what it would do and deletes nothing unless told `yes`
(`--yes`); what it refuses, and the articles that cite what it would
take, are on [Deleting](/deleting/). What was deleted keeps its
history.

MCP has a tool per noun rather than one per verb because every tool's
schema sits in an agent's context on every turn: these six, and the
in-process `run_protocol`, cost about 7.8 KB; the same verbs as a tool
each, with typed parameters, about 27.8 KB.

"""
)


def docs_page() -> str:
    return (
        PAGE_HEAD
        + "## The verbs\n\n"
        + table()
        + "\n\n## On one surface only\n\n"
        + one_surface()
        + "\n"
    )
