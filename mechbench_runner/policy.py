from __future__ import annotations

import json
import os
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import paths
from .api_client import ApiError

POLICY_MISMATCH = "POLICY_MISMATCH"

RETRY_SECONDS = 30.0

SERVE_SOURCES = ("own", "org", "members", "listed")
ADMIT_SOURCES = ("own", "org", "approved", "verified", "listed")
VETTED = ("checked", "verified")


@dataclass(frozen=True)
class PolicyOption:
    name: str
    description: str
    sources: tuple[str, ...]
    machines: tuple[str, ...]


SERVE_OPTIONS = (
    PolicyOption("me only", "The protocols you run, on any project, and nobody else's.",
                 ("own",), ("user",)),
    PolicyOption("me and my org", "The protocols you run, and every job on your orgs' "
                 "projects, whichever member ran it.", ("own", "org"), ("user",)),
    PolicyOption("the org", "Every job on the org's projects, whichever member ran it.",
                 ("own",), ("org",)),
    PolicyOption("the org and its members' own work", "Every job on the org's projects, "
                 "and the jobs its members run on their own projects.",
                 ("own", "members"), ("org",)),
    PolicyOption("nobody (paused)", "No jobs: the machine is paused.", (), ("user", "org")),
)

ADMIT_OPTIONS = (
    PolicyOption("mine", "Extensions in your own projects, in any state (on an org's "
                 "machine, the org's projects).", ("own",), ("user", "org")),
    PolicyOption("mine and my org's", "Extensions in your own projects and in your orgs' "
                 "projects, in any state: colleagues' work, read by nobody.",
                 ("own", "org"), ("user",)),
    PolicyOption("mine and my org's approved", "Extensions in your own projects, and the "
                 "versions your org's admins approved.", ("own", "approved"), ("user", "org")),
    PolicyOption("mechbench-verified", "Extensions in your own projects, and any version "
                 "the platform verified: a person and its reviewer read the code.",
                 ("own", "verified"), ("user", "org")),
    PolicyOption("nothing (locked)", "No extensions: core operations only.", (),
                 ("user", "org")),
)

SERVE_SOURCE_WORDS = {
    "own": "Jobs you run (on an org's machine, jobs on the org's projects).",
    "org": "Jobs on the projects of every org you belong to, whichever member ran them.",
    "members": "On an org's machine: jobs its members run on their own projects.",
    "listed": "Jobs on the projects, owners or orgs listed.",
}

ADMIT_SOURCE_WORDS = {
    "own": "Extensions in your own projects, in any state (on an org's machine, the "
           "org's projects).",
    "org": "Extensions in your orgs' projects, in any state.",
    "approved": "Versions an org you belong to approved (on an org's machine, that org).",
    "verified": "Versions the platform verified, whoever's.",
    "listed": "Extensions by the owners, orgs or names listed; a draft only when its "
              "owner is listed.",
}


def policy_option_of(options: tuple[PolicyOption, ...], sources: Any,
                     kind: str) -> PolicyOption | None:
    want = set(sources or ())
    for o in options:
        if kind in o.machines and len(o.sources) == len(want) and want == set(o.sources):
            return o
    return None


@dataclass(frozen=True)
class Decision:
    ok: bool
    reason: str | None = None


OK = Decision(True)


def _refuse(reason: str) -> Decision:
    return Decision(False, reason)


def _owner(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _same_owner(a: Mapping[str, Any], b: Mapping[str, Any]) -> bool:
    return a.get("kind") == b.get("kind") and a.get("id") == b.get("id")


def _list_words(words: list[str]) -> str:
    words = [str(w) for w in words]
    if len(words) < 2:
        return "".join(words)
    return f"{', '.join(words[:-1])} and {words[-1]}"


def _owner_words(o: Mapping[str, Any]) -> str:
    return f"{o.get('kind')} {o.get('id')}"


def _owner_matches(entry: Mapping[str, Any], owner: Mapping[str, Any]) -> bool:
    if entry.get("owner") is not None and not (
            owner.get("kind") == "user" and owner.get("id") == entry["owner"]):
        return False
    return not (entry.get("org") is not None and not (
        owner.get("kind") == "org" and owner.get("id") == entry["org"]))


def _names_nothing(entry: Mapping[str, Any], fields: tuple[str, ...]) -> bool:
    return all(entry.get(f) is None for f in fields)


def _serve_entry_matches(entry: Mapping[str, Any], job: Mapping[str, Any]) -> bool:
    if _names_nothing(entry, ("owner", "org", "project")):
        return False
    if not _owner_matches(entry, _owner(job.get("projectOwner"))):
        return False
    return entry.get("project") is None or entry["project"] == job.get("projectId")


def _admit_entry_matches(entry: Mapping[str, Any], ext: Mapping[str, Any]) -> bool:
    if _names_nothing(entry, ("owner", "org", "extension")):
        return False
    if not _owner_matches(entry, _owner(ext.get("projectOwner"))):
        return False
    return entry.get("extension") is None or entry["extension"] == ext.get("name")


def _serves(source: str, policy: Mapping[str, Any], job: Mapping[str, Any],
            runner: Mapping[str, Any]) -> bool:
    owner = _owner(runner.get("owner"))
    org_machine = owner.get("kind") == "org"
    project_owner = _owner(job.get("projectOwner"))
    org_ids = list(runner.get("orgIds") or [])
    if source == "own":
        if org_machine:
            return _same_owner(project_owner, owner)
        return job.get("creatorId") is not None and job.get("creatorId") == owner.get("id")
    if source == "org":
        if org_machine:
            return _same_owner(project_owner, owner)
        return project_owner.get("kind") == "org" and project_owner.get("id") in org_ids
    if source == "members":
        return (org_machine and project_owner.get("kind") == "user"
                and project_owner.get("id") == job.get("creatorId")
                and owner.get("id") in (job.get("creatorOrgIds") or []))
    if source == "listed":
        allow = (policy.get("jobs") or {}).get("allow") or []
        return any(_serve_entry_matches(e, job) for e in allow)
    return False


def policy_serves(policy: Mapping[str, Any], job: Mapping[str, Any],
                  runner: Mapping[str, Any]) -> Decision:
    sources = list((policy.get("jobs") or {}).get("serve") or [])
    if not sources:
        return _refuse("The policy serves nobody: this machine is paused.")
    if any(_serves(s, policy, job, runner) for s in sources):
        return OK
    return _refuse(
        f"The policy serves {_list_words(sources)}, and this job was run by user "
        f"{job.get('creatorId')} on a project "
        f"{_owner_words(_owner(job.get('projectOwner')))} owns.")


def _admits(source: str, policy: Mapping[str, Any], ext: Mapping[str, Any],
            runner: Mapping[str, Any]) -> bool:
    owner = _owner(runner.get("owner"))
    org_machine = owner.get("kind") == "org"
    project_owner = _owner(ext.get("projectOwner"))
    state = ext.get("state")
    if source == "own":
        return _same_owner(project_owner, owner)
    if source == "org":
        if org_machine:
            return _same_owner(project_owner, owner)
        return (project_owner.get("kind") == "org"
                and project_owner.get("id") in (runner.get("orgIds") or []))
    if source == "approved":
        if state not in VETTED:
            return False
        orgs = [owner.get("id")] if org_machine else list(runner.get("orgIds") or [])
        return any(o in orgs for o in ext.get("approvedBy") or [])
    if source == "verified":
        return state == "verified"
    if source == "listed":
        allow = (policy.get("extensions") or {}).get("allow") or []
        matches = [e for e in allow if _admit_entry_matches(e, ext)]
        if state in VETTED:
            return bool(matches)
        return any(e.get("owner") is not None or e.get("org") is not None for e in matches)
    return False


def policy_admits(policy: Mapping[str, Any], ext: Mapping[str, Any],
                  job: Mapping[str, Any], runner: Mapping[str, Any]) -> Decision:
    del job
    name = ext.get("name")
    state = ext.get("state")
    if state == "withdrawn":
        return _refuse(f"{name} is withdrawn, and a withdrawn version is never installed.")
    rules = policy.get("extensions") or {}
    sources = list(rules.get("admit") or [])
    if not sources:
        return _refuse("The policy admits nothing: core operations only.")
    first_party = ext.get("party") == "first" and state == "verified"
    if not first_party and not any(_admits(s, policy, ext, runner) for s in sources):
        listed_by_name_only = (
            "listed" in sources and state not in VETTED
            and any(_admit_entry_matches(e, ext) for e in rules.get("allow") or []))
        if listed_by_name_only:
            return _refuse(f"{name} is a {state} version, and listed admits one only "
                           f"when an entry names its owner.")
        approved_by = list(ext.get("approvedBy") or [])
        approvals = _list_words(approved_by) if approved_by else "no org"
        return _refuse(
            f"The policy admits {_list_words(sources)}, and {name} is a {state} version "
            f"in a project {_owner_words(_owner(ext.get('projectOwner')))} owns, "
            f"approved by {approvals}.")
    network = next((n for n in ext.get("needs") or [] if str(n).startswith("network:")), None)
    if network is not None and rules.get("network") == "none":
        return _refuse(f"The policy allows no network, and {name} declares {network}.")
    return OK


PERSONAL_POLICY: dict[str, Any] = {
    "jobs": {"serve": ["own"], "allow": []},
    "extensions": {"admit": ["own"], "allow": [], "network": "none"},
    "upgrades": {"compute": "auto"},
    "gc": {"unused_days": 30},
}

ORG_POLICY: dict[str, Any] = {
    **PERSONAL_POLICY,
    "extensions": {"admit": ["own", "approved"], "allow": [], "network": "none"},
}

ORG_POLICY_ID = "pol_org"


def default_policy_for(kind: str) -> dict[str, Any]:
    return json.loads(json.dumps(ORG_POLICY if kind == "org" else PERSONAL_POLICY))


OLD_INSTALL = {
    "locked": [],
    "mine": ["own"],
    "verified": ["own", "verified"],
    "allowlist": ["own", "listed"],
}


def is_current(body: Any) -> bool:
    if not isinstance(body, Mapping):
        return False
    jobs, ext = body.get("jobs"), body.get("extensions")
    return (isinstance(jobs, Mapping) and isinstance(jobs.get("serve"), list)
            and isinstance(ext, Mapping) and isinstance(ext.get("admit"), list))


def _allow_entry(entry: Any) -> dict[str, Any] | None:
    if not isinstance(entry, Mapping) or set(entry) - {"owner", "org", "extension"}:
        return None
    if not all(isinstance(v, str) and v for v in entry.values()):
        return None
    return dict(entry)


def _field(v: Any, k: str) -> Any:
    return v.get(k) if isinstance(v, Mapping) else None


def migrate_policy(old: Any) -> dict[str, Any]:
    if is_current(old):
        return dict(old)
    field = _field
    ext = field(old, "extensions")
    install = field(ext, "install")
    admit = OLD_INSTALL.get(install) if isinstance(install, str) else None
    allow_in = field(ext, "allow")
    allow = [e for e in (_allow_entry(x) for x in allow_in or []) if e is not None] \
        if isinstance(allow_in, list) else []
    network = field(ext, "network")
    compute = field(field(old, "upgrades"), "compute")
    days = field(field(old, "gc"), "unused_days")
    days_ok = isinstance(days, int) and not isinstance(days, bool) and 1 <= days <= 3650
    return {
        "jobs": {"serve": ["own"], "allow": []},
        "extensions": {
            "admit": list(admit if admit is not None else PERSONAL_POLICY["extensions"]["admit"]),
            "allow": allow[:200],
            "network": network if network in ("none", "declared") else "none",
        },
        "upgrades": {"compute": compute if compute in ("auto", "hold") else "auto"},
        "gc": {"unused_days": days if days_ok else 30},
    }


def job_of(claim: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "creatorId": claim.get("creatorId"),
        "projectId": claim.get("projectId"),
        "projectOwner": _owner(claim.get("projectOwner")),
        "creatorOrgIds": list(claim.get("creatorOrgIds") or []),
    }


def identity_of(me: Mapping[str, Any]) -> dict[str, Any]:
    runner = me.get("runner") if isinstance(me.get("runner"), Mapping) else {}
    account = me.get("account") if isinstance(me.get("account"), Mapping) else {}
    owner = _owner(me.get("owner") or runner.get("owner"))
    if not owner.get("kind") or not owner.get("id"):
        org_id = runner.get("scopeOrgId")
        if runner.get("scope") in ("org", "project") and org_id:
            owner = {"kind": "org", "id": str(org_id)}
        else:
            owner = {"kind": "user", "id": str(account.get("userId") or runner.get("userId"))}
    org_ids = me.get("orgIds") or runner.get("orgIds") or account.get("orgIds") or []
    return {"owner": owner, "orgIds": [str(o) for o in org_ids]}


@dataclass(frozen=True)
class HeldPolicy:
    id: str
    version: int
    body: dict[str, Any]
    name: str | None = None

    @property
    def label(self) -> str:
        if self.name and self.name != self.id:
            return f"{self.name} ({self.id}) v{self.version}"
        return f"{self.id} v{self.version}"

    def matches(self, ref: Mapping[str, Any]) -> bool:
        return ref.get("id") == self.id and ref.get("version") == self.version


def _ref_of(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    pid, ver = value.get("id"), value.get("version")
    if not isinstance(pid, str) or not isinstance(ver, int):
        return None
    return {"id": pid, "version": ver}


class PolicyHolder:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or paths.policy_path()
        self.held: HeldPolicy | None = None
        self._wanted: dict[str, Any] | None = None
        self._confirmed = False
        self._retry_at = 0.0
        self._lock = threading.Lock()
        self._identity: dict[str, Any] | None = None

    def load(self) -> HeldPolicy | None:
        try:
            raw = json.loads(self.path.read_text())
            name = raw.get("name")
            held = HeldPolicy(str(raw["id"]), int(raw["version"]),
                              migrate_policy(raw["body"]),
                              name if isinstance(name, str) else None)
        except (OSError, ValueError, KeyError, TypeError):
            return None
        with self._lock:
            self.held = held
        return held

    def _mirror(self, held: HeldPolicy) -> None:
        tmp = self.path.with_suffix(".json.tmp")
        try:
            mirrored: dict[str, Any] = {"id": held.id, "version": held.version,
                                        "body": held.body}
            if held.name is not None:
                mirrored["name"] = held.name
            tmp.write_text(json.dumps(mirrored, indent=2))
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)
        except OSError as exc:
            print(f"[runner] could not mirror the policy to {self.path} ({exc})")

    def note(self, ref: Any) -> bool:
        ref = _ref_of(ref)
        if ref is None:
            return False
        with self._lock:
            if self.held is not None and self.held.matches(ref):
                self._wanted = None
                return False
            if self._wanted != ref:
                self._retry_at = 0.0
            self._wanted = ref
            return True

    @property
    def stale(self) -> bool:
        with self._lock:
            return (self.held is None or not self._confirmed
                    or self._wanted is not None)

    def matches(self, ref: Any) -> bool:
        ref = _ref_of(ref)
        with self._lock:
            return ref is not None and self.held is not None and self.held.matches(ref)

    def refresh(self, api: Any) -> HeldPolicy:
        got = api.fetch_policy()
        name = got.get("name")
        held = HeldPolicy(str(got["policyId"]), int(got["version"]),
                          migrate_policy(got["body"]),
                          name if isinstance(name, str) else None)
        with self._lock:
            before = self.held
            self.held = held
            self._identity = None
            self._confirmed = True
            if self._wanted is not None and held.matches(self._wanted):
                self._wanted = None
        if before != held:
            self._mirror(held)
        if before is None or (before.id, before.version) != (held.id, held.version):
            print(f"[runner] policy {held.label} applied", flush=True)
        return held

    def start(self, api: Any) -> HeldPolicy | None:
        mirrored = self.load()
        try:
            return self.refresh(api)
        except Exception as exc:  # noqa: BLE001
            self._retry_at = time.monotonic() + RETRY_SECONDS
            if mirrored is not None:
                print(f"[runner] could not fetch the policy ({exc}); holding "
                      f"{mirrored.label} from the last run")
            else:
                print(f"[runner] could not fetch the policy ({exc}); "
                      "will retry before claiming")
            return mirrored

    def ensure_current(self, api: Any) -> bool:
        if not self.stale:
            return True
        if time.monotonic() < self._retry_at:
            return False
        self._retry_at = time.monotonic() + RETRY_SECONDS
        try:
            self.refresh(api)
        except ApiError as exc:
            if exc.status == 401:
                raise
            print(f"[runner] could not refresh the policy ({exc})")
            return False
        except Exception as exc:  # noqa: BLE001
            print(f"[runner] could not refresh the policy ({exc})")
            return False
        return not self.stale

    def identity(self, api: Any) -> dict[str, Any]:
        with self._lock:
            known = self._identity
        if known is not None:
            return known
        found = identity_of(api.whoami())
        with self._lock:
            self._identity = found
        return found


def runner_of(api: Any, holder: PolicyHolder, claim: Mapping[str, Any]) -> dict[str, Any]:
    carried = claim.get("runner")
    if isinstance(carried, Mapping) and isinstance(carried.get("owner"), Mapping):
        return identity_of({"owner": carried["owner"], "orgIds": carried.get("orgIds")})
    return holder.identity(api)


def check_claim(api: Any, holder: PolicyHolder,
                claim: Mapping[str, Any]) -> list[Decision]:
    ref = _ref_of(claim.get("policy"))
    if ref is not None and not holder.matches(ref):
        holder.note(ref)
        try:
            holder.refresh(api)
        except Exception as exc:  # noqa: BLE001
            print(f"[runner] could not refresh the policy ({exc})")
    held = holder.held
    if ref is not None and not holder.matches(ref):
        have = held.label if held else "no policy"
        refusal = _refuse(f"The claim was made under {ref['id']} v{ref['version']}; "
                          f"this runner holds {have}.")
        _release(api, claim, [refusal])
        return [refusal]
    if held is None:
        refusal = _refuse("This runner holds no policy, so it takes no job.")
        _release(api, claim, [refusal])
        return [refusal]
    try:
        runner = runner_of(api, holder, claim)
    except ApiError as exc:
        if exc.status == 401:
            raise
        return _unknown_owner(api, claim, exc)
    except Exception as exc:  # noqa: BLE001
        return _unknown_owner(api, claim, exc)
    decisions = evaluate(held.body, claim, runner)
    if not all(d.ok for d in decisions):
        _release(api, claim, decisions)
        try:
            holder.refresh(api)
        except Exception as exc:  # noqa: BLE001
            print(f"[runner] could not refresh the policy ({exc})")
    return decisions


def evaluate(policy: Mapping[str, Any], claim: Mapping[str, Any],
             runner: Mapping[str, Any]) -> list[Decision]:
    job = job_of(claim)
    served = policy_serves(policy, job, runner)
    if not served.ok:
        served = _refuse(f"This runner does not serve it: {served.reason}")
    installs = [i for i in claim.get("install") or [] if isinstance(i, Mapping)]
    return [served, *(policy_admits(policy, i, job, runner) for i in installs)]


def _unknown_owner(api: Any, claim: Mapping[str, Any], exc: Exception) -> list[Decision]:
    refusal = _refuse(f"This runner could not learn who owns it ({exc}).")
    _release(api, claim, [refusal])
    return [refusal]


def _release(api: Any, claim: Mapping[str, Any], decisions: list[Decision]) -> None:
    job_id = str(claim.get("id"))
    text = (" ".join(d.reason for d in decisions if not d.ok and d.reason)
            or "This runner's policy refuses the job.")
    print(f"[runner] job {job_id} released: {POLICY_MISMATCH}: {text}", flush=True)
    try:
        api.release_job(job_id, POLICY_MISMATCH, text)
    except ApiError as exc:
        if exc.status != 404:
            print(f"[runner] could not release job {job_id} ({exc})")
            return
        try:
            api.fail_job(job_id, f"{POLICY_MISMATCH}: {text}")
        except Exception as exc2:  # noqa: BLE001
            print(f"[runner] could not release job {job_id} ({exc2})")
    except Exception as exc:  # noqa: BLE001
        print(f"[runner] could not release job {job_id} ({exc})")
