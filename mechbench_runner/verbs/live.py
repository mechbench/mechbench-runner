from __future__ import annotations

import json
import re
import uuid
from typing import Any

from .. import paths
from .core import LIMIT, OFFSET, SEARCH, Arg, Ctx, Noun, Verb, VerbError, paged

CURRENT_NAME = "live.json"
DURATION = re.compile(r"^(\d+)([smhd]?)$")
UNIT = {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}


def current_path() -> Any:
    return paths.mechbench_dir() / CURRENT_NAME


def current() -> str | None:
    try:
        got = json.loads(current_path().read_text()).get("current")
    except (OSError, ValueError, AttributeError):
        return None
    return got if isinstance(got, str) and got else None


def set_current(live_run_id: str | None) -> None:
    current_path().write_text(json.dumps({"current": live_run_id}) + "\n")


def seconds(text: Any) -> int:
    m = DURATION.match(str(text).strip())
    if m is None:
        raise VerbError(f"a duration is seconds or a number with s, m, h or d, not {text!r}")
    return int(m.group(1)) * UNIT[m.group(2)]


def which(a: dict) -> str:
    got = a.get("live_run") or current()
    if not got:
        raise VerbError("no live run named and none current on this machine: `mechbench live start --model …`")
    return str(got)


def value_of(text: Any, port: str = "PORT") -> Any:
    if not isinstance(text, str):
        return text
    if text == "":
        raise VerbError(f"--in {port}= is empty: for a name, quote it ('{port}=$NAME') "
                        "so the shell leaves the $ alone")
    if text.startswith("$"):
        return {"$name": text[1:]}
    if text[:1] in ("{", "[", '"'):
        try:
            return json.loads(text)
        except ValueError as e:
            raise VerbError(f"not valid JSON: {text[:60]!r} ({e})") from e
    return {"$ref": {"bench": text}}


def live_list(ctx: Ctx, a: dict) -> Any:
    got = ctx.get("/live-runs")
    rows = (got.get("liveRuns") if isinstance(got, dict) else got) or []
    want = str(a.get("search") or "").lower()
    if want:
        rows = [r for r in rows if any(want in str(r.get(k) or "").lower() for k in ("id", "label", "model"))]
    return paged(rows, a)


def live_read(ctx: Ctx, a: dict) -> Any:
    return ctx.get(f"/live-runs/{a.get('id') or which(a)}")


def live_start(ctx: Ctx, a: dict) -> Any:
    body: dict[str, Any] = {"model": a["model"]}
    if a.get("idle"):
        body["idleSeconds"] = seconds(a["idle"])
    if a.get("close_after"):
        body["closeAfterSeconds"] = seconds(a["close_after"])
    if a.get("label"):
        body["label"] = a["label"]
    got = ctx.api("POST", "/live-runs", body=body)[0]
    live_run = got.get("liveRun") or {}
    if live_run.get("id"):
        set_current(str(live_run["id"]))
    return got


def start_lines(answer: dict[str, Any]) -> list[str]:
    live_run = answer.get("liveRun") or {}
    lines = [str(live_run.get("id"))]
    if live_run.get("runnerId"):
        lines.append(f"  held by {live_run['runnerId']}, warming {live_run.get('model')}; current on this machine")
    else:
        lines.append(f"  no runner holds it yet: {answer.get('why') or 'none of your runners is connected'}")
    return lines


def live_try(ctx: Ctx, a: dict) -> Any:
    live_run_id = which(a)
    body: dict[str, Any] = {
        "op": a["op"],
        "inputs": {k: value_of(v, k) for k, v in (a.get("inputs") or {}).items()},
        "params": dict(a.get("params") or {}),
        "clientId": a.get("client_id") or uuid.uuid4().hex,
        "wait": int(a.get("wait") or 120),
    }
    if a.get("slot"):
        body["slot"] = a["slot"]
    if a.get("as"):
        body["as"] = a["as"]
    if a.get("baseline"):
        body["baseline"] = a["baseline"]
    if a.get("noise"):
        body["noise"] = a["noise"]
    if a.get("k") is not None:
        body["k"] = float(a["k"])
    return ctx.api("POST", f"/live-runs/{live_run_id}/tries", body=body)[0]


def live_names(ctx: Ctx, a: dict) -> Any:
    got = ctx.get(f"/live-runs/{which(a)}/names")
    names = (got.get("names") if isinstance(got, dict) else None) or {}
    rows = [{"name": k, **v} for k, v in sorted(names.items())]
    want = str(a.get("search") or "").lower()
    if want:
        rows = [r for r in rows if any(want in str(r.get(k) or "").lower() for k in ("name", "kind"))]
    return paged(rows, a)


def live_close(ctx: Ctx, a: dict) -> Any:
    live_run_id = which(a)
    got = ctx.api("POST", f"/live-runs/{live_run_id}/close")[0]
    if current() == live_run_id:
        set_current(None)
    return {"closed": live_run_id, **(got if isinstance(got, dict) else {})}


def try_lines(answer: dict[str, Any]) -> list[str]:
    if "lines" not in answer:
        return [f"t{answer.get('seq')} is still {answer.get('status') or 'queued'}: "
                f"`mechbench live read` shows it when it is done"]
    prov = answer.get("provenance") or {}
    timing = answer.get("timing") or {}
    head = f"t{answer.get('seq')}  {answer.get('kind')}  {timing.get('totalMs', '?')} ms  {prov.get('hash', '')[:19]}"
    where = answer.get("address") or "inline (--json prints it)"
    named = [f"  as {name}: {entry.get('path')}" for name, entry in (answer.get("names") or {}).items()]
    said = [f"  {line}" for line in answer.get("lines") or []]
    return [head, *said, f"  result: {where}", *named,
            *notable_lines(answer.get("notable"))]


def notable_lines(notable: Any) -> list[str]:
    if not isinstance(notable, dict) or not notable.get("line"):
        return []
    margin = [f"    margin: {c['line']}" for c in notable.get("caveats") or []
              if isinstance(c, dict) and c.get("line")]
    return [f"  {notable['line']}", *margin]


LIVE_RUN = Arg("live_run", "The live run (default: this machine's current one).", flag="--live-run")

LIVE = Noun(
    "live",
    "A live run: a model held warm on one of your runners, taking tries (any operation, "
    "answered in the response) until it is closed. `start` holds one and makes it this "
    "machine's current live run; `try` runs an operation on it, and with `as` binds the "
    "result to a name a later try reads as $NAME; `names` lists them; `close` ends it.",
    (
        Verb(
            "live", "list", "Your live runs, newest first.", "GET /live-runs",
            (SEARCH, LIMIT, OFFSET), live_list, "list",
            ("id", "form", "model", "status", "runnerId", "seq", "expiresAt"), effect="read",
        ),
        Verb(
            "live", "read", "A live run and its tries or events, each with its answer.",
            "GET /live-runs/:id",
            (Arg("id", "Its id (default: this machine's current live run).", positional=True),),
            live_read, effect="read",
        ),
        Verb(
            "live", "start",
            "Hold a model warm on one of your runners for tries, and make it this machine's "
            "current live run; prints its id.",
            "POST /live-runs",
            (
                Arg("model", "The model to hold, e.g. google/gemma-3n-E2B-it.", required=True),
                Arg("idle", "Release the model after this long with no try (15m, 600; default 10m)."),
                Arg("close_after", "Close it this long after the last try (default 24h).", flag="--close-after"),
                Arg("label", "What to call it."),
            ),
            live_start, effect="draft",
        ),
        Verb(
            "live", "try",
            "Run one operation on the live run's warm model and answer with its result, "
            "its lines, its notable line (whether it moved against its baseline, with "
            "any caveats) and where it is kept; nothing is a job or a run.",
            "POST /live-runs/:id/tries",
            (
                Arg("op", "The operation, e.g. logits/read-layers.", required=True, positional=True),
                Arg("inputs", "An input port, PORT=PATH (an object path, read by the Resolver), "
                              "PORT=$NAME (a name this live run bound; quote it from a shell) "
                              "or PORT=JSON.", type="pairs", flag="--in"),
                Arg("params", "A param, NAME=VALUE (VALUE read as JSON when it parses; a name "
                              "goes in a param as {\"$name\": NAME}).",
                    type="pairs", flag="--set"),
                Arg("as", "Bind the result to this name: one path segment, not t followed by "
                          "digits. `mechbench let NAME = OP …` is the same."),
                Arg("baseline",
                    "Read the result against this name ($_ is the last try that "
                    "succeeded; quote it from a shell). Default: the control the "
                    "result carries, else the first try of the same operation with "
                    "one param changed."),
                Arg("noise",
                    "The noise floor: a platform/noise collection's path, as "
                    "records/measure-noise writes it. A change has moved only past k "
                    "floors and the kind's threshold; default the runner's own floor, "
                    "when it has one."),
                Arg("k",
                    "How many floors a change must pass to have moved (default 1).",
                    type="float"),
                Arg("slot", "Replace a still-queued try in this slot."),
                Arg("wait", "Seconds to wait for the answer (at most 120).", type="int"),
                Arg("client_id", "Your own id for it: a resend is the same try.", flag="--client-id"),
                LIVE_RUN,
                Arg("as_json", "Print the whole answer, the result with it, as JSON.", type="bool",
                    flag="--json", local=True),
            ),
            live_try, effect="draft",
        ),
        Verb(
            "live", "names",
            "The names bound in a live run, each with the try whose result it names and its "
            "scratch path. Names die with the live run; `object copy` puts a value in a project.",
            "GET /live-runs/:id/names", (LIVE_RUN, SEARCH, LIMIT, OFFSET), live_names, "list",
            ("name", "seq", "kind", "path"), effect="read",
        ),
        Verb(
            "live", "close", "End the live run: its model is released and its scratch deleted.",
            "POST /live-runs/:id/close", (LIVE_RUN,), live_close, effect="delete",
        ),
    ),
    absent={
        "create": "`start` holds a model warm, which is what creating one is",
        "update": "a live run changes only by its tries",
        "delete": "`close` ends it and deletes its scratch; its tries are kept 30 days",
        "history": "its tries are its history: `live read`",
    },
)
