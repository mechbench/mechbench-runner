from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import re
import subprocess
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

from . import install as install_mod
from . import paths

SCHEMA = 1
RUNNER_DIST = "mechbench"
COMPUTE_DIST = "mechbench-compute"
OURS = (RUNNER_DIST, COMPUTE_DIST)
KEY_FILE = Path(__file__).with_name("release_key.pub")
INSTALL_TIMEOUT_SECONDS = 900.0

_HEX64 = re.compile(r"[0-9a-f]{64}")
_NAME = re.compile(r"[A-Za-z0-9]([A-Za-z0-9._-]*[A-Za-z0-9])?")


class ManifestError(Exception):
    pass


@dataclass(frozen=True)
class Wheel:
    url: str
    sha256: str

    @property
    def filename(self) -> str:
        return urlparse(self.url).path.rsplit("/", 1)[-1]


@dataclass(frozen=True)
class Locked:
    name: str
    version: str
    sha256: tuple[str, ...]
    marker: str | None = None


@dataclass(frozen=True)
class Manifest:
    runner_version: str
    runner_wheel: Wheel
    compute_version: str
    compute_wheel: Wheel
    locked: tuple[Locked, ...]
    allow_downgrade: bool
    published_at: str
    body: Mapping[str, Any] = field(repr=False, compare=False, default_factory=dict)

    @property
    def target(self) -> tuple[str, str]:
        return self.runner_version, self.compute_version


def manifest_url(api_base_url: str) -> str:
    return f"{api_base_url.rstrip('/')}/releases/manifest"


def canonical(body: Mapping[str, Any]) -> bytes:
    unsigned = {k: v for k, v in body.items() if k != "signature"}
    return json.dumps(unsigned, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")


def public_key(path: Path | None = None) -> Any:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    from cryptography.hazmat.primitives.serialization import load_pem_public_key

    try:
        pem = (path or KEY_FILE).read_bytes()
    except OSError:
        return None
    if b"BEGIN PUBLIC KEY" not in pem:
        return None
    try:
        key = load_pem_public_key(pem)
    except ValueError:
        return None
    return key if isinstance(key, Ed25519PublicKey) else None


def verify(body: Any, key: Any) -> Manifest:
    from cryptography.exceptions import InvalidSignature

    if key is None:
        raise ManifestError("this build carries no release key, so no manifest "
                            "can be trusted")
    if not isinstance(body, Mapping):
        raise ManifestError("the manifest is not a JSON object")
    sig = body.get("signature")
    if not isinstance(sig, str) or not sig:
        raise ManifestError("the manifest is not signed")
    try:
        raw = base64.b64decode(sig, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ManifestError("the manifest's signature is not base64") from exc
    try:
        key.verify(raw, canonical(body))
    except InvalidSignature as exc:
        raise ManifestError("the manifest's signature does not verify against "
                            "the release key") from exc
    return _parse(body)


def _parse(body: Mapping[str, Any]) -> Manifest:
    if body.get("schema") != SCHEMA:
        raise ManifestError(f"the manifest is schema {body.get('schema')!r}; "
                            f"this runner reads schema {SCHEMA}")
    runner_v, runner_w = _dist(body, "runner", RUNNER_DIST)
    compute_v, compute_w = _dist(body, "compute", COMPUTE_DIST)
    locked_raw = body.get("locked")
    if not isinstance(locked_raw, list):
        raise ManifestError("the manifest's locked is not a list")
    locked = tuple(_locked(e) for e in locked_raw)
    for e in locked:
        if _norm(e.name) in OURS:
            raise ManifestError(f"the manifest locks {e.name} outside its own entry")
    allow = body.get("allow_downgrade", False)
    if not isinstance(allow, bool):
        raise ManifestError("the manifest's allow_downgrade is not a boolean")
    published = body.get("published_at")
    if not isinstance(published, str) or not published:
        raise ManifestError("the manifest has no published_at")
    return Manifest(runner_v, runner_w, compute_v, compute_w, locked, allow,
                    published, dict(body))


def _strict_version(text: Any, what: str) -> str:
    from packaging.version import InvalidVersion, Version

    if not isinstance(text, str):
        raise ManifestError(f"{what} has no version")
    try:
        v = Version(text)
    except InvalidVersion as exc:
        raise ManifestError(f"{what}'s version {text!r} is not a version") from exc
    if v.local is not None or str(v) != text:
        raise ManifestError(f"{what}'s version {text!r} is not a strict version")
    return text


def _sha(text: Any, what: str) -> str:
    if not isinstance(text, str) or not _HEX64.fullmatch(text):
        raise ManifestError(f"{what} has no sha256")
    return text


def _dist(body: Mapping[str, Any], key: str, dist: str) -> tuple[str, Wheel]:
    entry = body.get(key)
    if not isinstance(entry, Mapping):
        raise ManifestError(f"the manifest names no {key}")
    version = _strict_version(entry.get("version"), dist)
    wheel = entry.get("wheel")
    if not isinstance(wheel, Mapping):
        raise ManifestError(f"the manifest names no wheel for {dist}")
    url = wheel.get("url")
    if not isinstance(url, str) or urlparse(url).scheme != "https":
        raise ManifestError(f"{dist}'s wheel url is not https")
    w = Wheel(url, _sha(wheel.get("sha256"), f"{dist}'s wheel"))
    stem = dist.replace("-", "_")
    name = w.filename
    if not (name.startswith(f"{stem}-{version}-") and name.endswith(".whl")
            and _NAME.fullmatch(name[:-4].replace("-", "_"))):
        raise ManifestError(f"{dist}'s wheel url names {w.filename!r}, not a "
                            f"{dist} {version} wheel")
    return version, w


def _locked(entry: Any) -> Locked:
    from packaging.markers import InvalidMarker, Marker

    if not isinstance(entry, Mapping):
        raise ManifestError("a locked entry is not an object")
    name = entry.get("name")
    if not isinstance(name, str) or not _NAME.fullmatch(name):
        raise ManifestError(f"a locked entry names {name!r}, not a distribution")
    version = _strict_version(entry.get("version"), name)
    hashes = entry.get("sha256")
    if not isinstance(hashes, list) or not hashes:
        raise ManifestError(f"{name} {version} is locked with no sha256")
    marker = entry.get("marker")
    if marker is not None:
        if not isinstance(marker, str) or any(c in marker for c in "\r\n\\#"):
            raise ManifestError(f"{name}'s marker is not a marker")
        try:
            Marker(marker)
        except InvalidMarker as exc:
            raise ManifestError(f"{name}'s marker is not a marker ({exc})") from exc
    return Locked(name, version, tuple(_sha(h, f"{name} {version}") for h in hashes),
                  marker)


def requirements(m: Manifest, wheels: Mapping[str, Path]) -> str:
    lines: list[str] = []
    for e in sorted(m.locked, key=lambda e: _norm(e.name)):
        head = f"{e.name}=={e.version}" + (f" ; {e.marker}" if e.marker else "")
        hashes = [f"    --hash=sha256:{h}" for h in e.sha256]
        lines.append(" \\\n".join([head, *hashes]))
    ours = ((RUNNER_DIST, m.runner_wheel), (COMPUTE_DIST, m.compute_wheel))
    for dist, wheel in ours:
        lines.append(f"{dist} @ {wheels[dist].resolve().as_uri()} \\\n"
                     f"    --hash=sha256:{wheel.sha256}")
    return "\n".join(lines) + "\n"


def plan(m: Manifest, have: Mapping[str, str | None], *,
         requested: str | None = None) -> tuple[bool, str]:
    from packaging.version import InvalidVersion, Version

    if requested and requested != m.runner_version:
        return False, (f"the update asked for {RUNNER_DIST} {requested}; the release "
                       f"manifest names {m.runner_version}, so nothing is installed")
    change = False
    for dist, want in zip(OURS, m.target, strict=True):
        got = have.get(dist)
        if not got or got == "(absent)":
            change = True
            continue
        try:
            older = Version(want) < Version(got.split("+", 1)[0])
        except InvalidVersion:
            older = False
        if older and not m.allow_downgrade:
            return False, (f"the release manifest names {dist} {want}, older than "
                           f"the installed {got}, without allow_downgrade; "
                           f"nothing is installed")
        change = change or want != got
    return change, ""


def release_dir() -> Path:
    d = paths.mechbench_dir() / "release"
    d.mkdir(mode=0o700, parents=True, exist_ok=True)
    return d


def _state_path() -> Path:
    return release_dir() / "state.json"


def _read_state() -> dict[str, Any]:
    try:
        raw = json.loads(_state_path().read_text())
    except (OSError, ValueError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _write_state(state: Mapping[str, Any]) -> None:
    tmp = _state_path().with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True))
    os.replace(tmp, _state_path())


def _get_bytes(url: str) -> bytes:
    res = httpx.get(url, timeout=httpx.Timeout(120.0), follow_redirects=True)
    res.raise_for_status()
    return res.content


def fetch_json(url: str) -> Any:
    res = httpx.get(url, timeout=httpx.Timeout(15.0), follow_redirects=True)
    res.raise_for_status()
    return res.json()


def download(dist: str, wheel: Wheel, *,
             get_bytes: Callable[[str], bytes] = _get_bytes) -> Path:
    target = release_dir() / "wheels" / wheel.sha256[:16] / wheel.filename
    if target.is_file() and _sha256(target.read_bytes()) == wheel.sha256:
        return target
    try:
        data = get_bytes(wheel.url)
    except httpx.HTTPError as exc:
        raise ManifestError(f"could not fetch {dist}'s wheel ({exc})") from exc
    got = _sha256(data)
    if got != wheel.sha256:
        raise ManifestError(f"{dist}'s wheel arrived as sha256:{got}, not the "
                            f"manifest's sha256:{wheel.sha256}; refusing it")
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = target.with_suffix(".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, target)
    return target


Run = Callable[..., subprocess.CompletedProcess[str]]


def _install_cmd(python: str, req: Path) -> list[str]:
    uv = install_mod.find_executable("uv")
    if uv is not None:
        return [uv, "pip", "install", "--python", python, "--require-hashes",
                "-r", str(req)]
    return [python, "-m", "pip", "install", "--require-hashes", "-r", str(req)]


def _run_install(cmd: list[str], run: Run, say: Callable[[str], None]) -> None:
    say(" ".join(cmd))
    try:
        proc = run(cmd, timeout=INSTALL_TIMEOUT_SECONDS)
    except (OSError, subprocess.SubprocessError) as exc:
        raise ManifestError(f"the installer did not run to the end ({exc})") from exc
    if proc.returncode != 0:
        tail = ((proc.stderr or "") + (proc.stdout or "")).strip()
        lines = [ln for ln in tail.splitlines() if ln.strip()]
        raise ManifestError(" ".join(lines[-3:])[:400]
                            or f"the installer exited {proc.returncode}")


def install(python: str, m: Manifest, *,
            get_bytes: Callable[[str], bytes] = _get_bytes,
            run: Run | None = None,
            say: Callable[[str], None] = print) -> Path:
    wheels = {dist: download(dist, wheel, get_bytes=get_bytes)
              for dist, wheel in ((RUNNER_DIST, m.runner_wheel),
                                  (COMPUTE_DIST, m.compute_wheel))}
    text = requirements(m, wheels)
    req = release_dir() / f"{_sha256(text.encode())[:16]}.requirements.txt"
    req.write_text(text)
    _run_install(_install_cmd(python, req), run or install_mod._run, say)  # noqa: SLF001
    state = _read_state()
    if state.get("current") and state.get("current") != str(req):
        state["previous"] = state["current"]
    state["current"] = str(req)
    _write_state(state)
    return req


def restore_previous(python: str, fallback: tuple[str | None, str | None], *,
                     run: Run | None = None,
                     say: Callable[[str], None] = print) -> bool:
    state = _read_state()
    prev = state.get("previous")
    run = run or install_mod._run  # noqa: SLF001
    try:
        if isinstance(prev, str) and Path(prev).is_file():
            _run_install(_install_cmd(python, Path(prev)), run, say)
            _write_state({"current": prev})
            return True
        pins = [f"{d}=={v}" for d, v in zip(OURS, fallback, strict=True)
                if v and v != "(absent)"]
        if not pins:
            say("no earlier hash-locked install is recorded and no earlier "
                "version is known; nothing restored")
            return False
        say("no earlier hash-locked install is recorded; reinstalling the "
            f"earlier versions by pin: {' '.join(pins)}")
        uv = install_mod.find_executable("uv")
        cmd = ([uv, "pip", "install", "--python", python, *pins] if uv
               else [python, "-m", "pip", "install", *pins])
        _run_install(cmd, run, say)
        state.pop("current", None)
        _write_state(state)
        return True
    except ManifestError as exc:
        say(f"could not restore the earlier versions ({exc})")
        return False


@dataclass
class Upgrade:
    ok: bool
    changed: bool
    message: str
    previous: tuple[str | None, str | None] = (None, None)
    target: tuple[str, str] | None = None


def upgrade(python: str, url: str, *, requested: str | None = None,
            fetch: Callable[[str], Any] = fetch_json,
            get_bytes: Callable[[str], bytes] = _get_bytes,
            run: Run | None = None,
            key: Any = None,
            skip: tuple[str, str] | None = None,
            say: Callable[[str], None] = print) -> Upgrade:
    try:
        body = fetch(url)
    except Exception as exc:  # noqa: BLE001
        return Upgrade(False, False, f"could not read the release manifest ({exc})")
    try:
        m = verify(body, key if key is not None else public_key())
    except ManifestError as exc:
        return Upgrade(False, False, f"release manifest refused: {exc}")
    have = install_mod.installed_versions()
    previous = (have.get(RUNNER_DIST), have.get(COMPUTE_DIST))
    if skip is not None and m.target == skip:
        return Upgrade(True, False, "", previous, m.target)
    go, why = plan(m, have, requested=requested)
    if not go:
        return Upgrade(not why, False, why, previous, m.target)
    kept = extras_before()
    try:
        install(python or sys.executable, m, get_bytes=get_bytes, run=run, say=say)
    except ManifestError as exc:
        return Upgrade(False, False,
                       f"upgrade to {RUNNER_DIST} {m.runner_version}, {COMPUTE_DIST} "
                       f"{m.compute_version} failed ({exc})", previous, m.target)
    broken = extras_broken(kept)
    if broken:
        here = python or sys.executable
        lost = "; ".join(f"the {extra} extra needs {', '.join(problems)}"
                         for extra, problems in broken.items())
        restored = restore_previous(here, previous, run=run, say=say)
        fix = " && ".join(
            f"uv pip install --python {here} "
            f"'{COMPUTE_DIST}[{extra}]=={m.compute_version}'" for extra in broken)
        back = "is restored" if restored else "could not be restored"
        return Upgrade(False, False,
                       f"upgrade to {RUNNER_DIST} {m.runner_version}, {COMPUTE_DIST} "
                       f"{m.compute_version} would drop what this machine installed "
                       f"beside it ({lost}); the install before it {back}. To take "
                       f"it with the extra: {fix}", previous, m.target)
    return Upgrade(True, True,
                   f"upgraded {RUNNER_DIST} {previous[0]} -> {m.runner_version}, "
                   f"{COMPUTE_DIST} {previous[1]} -> {m.compute_version} from the "
                   f"release manifest of {m.published_at}", previous, m.target)


def extras_before() -> dict[str, list[str]]:
    return install_mod.unmet_extras()


def extras_broken(before: Mapping[str, list[str]]) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for extra, was in before.items():
        now = [p for p in install_mod.unmet(COMPUTE_DIST, extra) if p not in was]
        if now:
            out[extra] = now
    return out


def _norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
