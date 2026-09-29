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

_ABSENT = object()


@dataclass(frozen=True)
class Admit:
    ok: bool
    reason: str | None = None


def _refuse(reason: str) -> Admit:
    return Admit(False, reason)


def _entry_matches(entry: Mapping[str, Any], ext: Mapping[str, Any]) -> bool:
    fields = ("owner", "org", "extension")
    named = [entry.get(f, _ABSENT) for f in fields]
    if all(v is _ABSENT for v in named):
        return False
    owner, org, name = named
    if owner is not _ABSENT and owner != ext.get("owner", _ABSENT):
        return False
    if org is not _ABSENT and org != ext.get("org", _ABSENT):
        return False
    return not (name is not _ABSENT and name != ext.get("name", _ABSENT))


def policy_admits(policy: Mapping[str, Any], extension: Mapping[str, Any],
                  context: Mapping[str, Any]) -> Admit:
    ext = extension
    name = ext["name"]
    state = ext["state"]
    if state == "withdrawn":
        return _refuse(
            f"{name} is withdrawn, and a withdrawn version is never installed.")
    verified = state == "verified"
    rules = policy["extensions"]
    level = rules["install"]
    if level == "locked":
        return _refuse("The policy is locked: it installs nothing.")
    if level == "mine":
        if context["jobCreatorId"] != context["runnerOwnerId"]:
            return _refuse(
                "Under mine, a runner installs only for its owner's own jobs.")
        if not verified and ext["owner"] != context["jobCreatorId"]:
            return _refuse(
                f"Under mine, {name} must be verified or your own draft; "
                f"it is {state}.")
    if level == "verified" and not verified:
        return _refuse(f"Under verified, {name} must be verified; it is {state}.")
    if level == "allowlist":
        matches = [e for e in rules.get("allow") or [] if _entry_matches(e, ext)]
        if not matches:
            return _refuse(f"{name} is not on the policy's allowlist.")
        if not verified and not any(
                e.get("owner", _ABSENT) == ext["owner"] for e in matches):
            return _refuse(
                f"{name} is {state}, and an unverified extension installs only "
                f"when its owner is listed.")
    needs = ext.get("needs") or []
    network = next((n for n in needs if n.startswith("network:")), None)
    if network is not None and rules["network"] == "none":
        return _refuse(
            f"The policy allows no network, and {name} declares {network}.")
    return Admit(True)


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
        self.owner_id: str | None = None

    def load(self) -> HeldPolicy | None:
        try:
            raw = json.loads(self.path.read_text())
            name = raw.get("name")
            held = HeldPolicy(str(raw["id"]), int(raw["version"]), dict(raw["body"]),
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
        held = HeldPolicy(str(got["policyId"]), int(got["version"]), dict(got["body"]),
                          name if isinstance(name, str) else None)
        with self._lock:
            before = self.held
            self.held = held
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

    def owner(self, api: Any) -> str:
        if self.owner_id is None:
            me = api.whoami()
            account = me.get("account") or {}
            runner = me.get("runner") or {}
            self.owner_id = str(account.get("userId") or runner.get("userId"))
        return self.owner_id


def check_installs(api: Any, holder: PolicyHolder,
                   claim: Mapping[str, Any]) -> list[Admit]:
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
    installs = list(claim.get("install") or [])
    if not installs:
        return []
    if held is None:
        refusal = _refuse("This runner holds no policy, so it installs nothing.")
        _release(api, claim, [refusal])
        return [refusal]
    context: dict[str, Any] = {
        "runnerOwnerId": holder.owner(api),
        "jobCreatorId": claim.get("userId"),
    }
    if claim.get("orgId") is not None:
        context["jobOrgId"] = claim["orgId"]
    admits = [policy_admits(held.body, item, context) for item in installs]
    if not all(a.ok for a in admits):
        _release(api, claim, admits)
    return admits


def _release(api: Any, claim: Mapping[str, Any], admits: list[Admit]) -> None:
    job_id = str(claim.get("id"))
    text = (" ".join(a.reason for a in admits if not a.ok and a.reason)
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
