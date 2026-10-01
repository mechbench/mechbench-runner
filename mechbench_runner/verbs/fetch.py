from __future__ import annotations

import hashlib
import io
import pathlib
import re
import tarfile
from collections.abc import Mapping
from typing import Any

from ..confine import PathRefusedError, under
from .core import Ctx, VerbError

SDIST_REF = re.compile(r"~hash/sha256:([0-9a-f]{64})")


def sdist_ref_of(version: Mapping[str, Any]) -> tuple[str, str]:
    manifest = version.get("manifest") if isinstance(version.get("manifest"), Mapping) else {}
    ref = str((manifest.get("package") or {}).get("sdist") or "")
    m = SDIST_REF.fullmatch(ref)
    if not m:
        raise VerbError(f"the version's package names no sdist by hash (got {ref or 'none'!r})")
    return ref, m.group(1)


def members_of(tar: tarfile.TarFile, dest: pathlib.Path) -> list[tuple[tarfile.TarInfo, pathlib.Path]]:
    base = dest.resolve()
    out = []
    for m in tar.getmembers():
        if not (m.isfile() or m.isdir()):
            raise VerbError(f"refused the sdist: {m.name!r} is a link or a device, not a file")
        try:
            path = under(dest, m.name, what="sdist member", ident=m.name)
        except PathRefusedError as e:
            if m.isdir() and pathlib.Path(dest, m.name).resolve() == base:
                continue
            raise VerbError(f"refused the sdist: {e}") from None
        out.append((m, path))
    return out


def unpack(data: bytes, dest: pathlib.Path) -> list[str]:
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
            members = members_of(tar, dest)
            dest.mkdir(parents=True, exist_ok=True)
            base = dest.resolve()
            written = []
            for m, path in members:
                if m.isdir():
                    path.mkdir(parents=True, exist_ok=True)
                    continue
                src = tar.extractfile(m)
                if src is None:
                    raise VerbError(f"refused the sdist: {m.name!r} has no content")
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(src.read())
                path.chmod(0o644)
                written.append(str(path.relative_to(base)))
    except tarfile.TarError as e:
        raise VerbError(f"the sdist is not a .tar.gz ({e})") from None
    return sorted(written)


def fetch(ctx: Ctx, route: str, to: str) -> dict[str, Any]:
    dest = pathlib.Path(to)
    if dest.is_symlink() or (dest.exists() and (not dest.is_dir() or any(dest.iterdir()))):
        raise VerbError(f"{dest} is not an empty directory")
    version = ctx.get(route)
    if not isinstance(version, Mapping):
        raise VerbError("the platform answered no version")
    ref, digest = sdist_ref_of(version)
    data = ctx.fetch_by_hash(ref)
    got = hashlib.sha256(data).hexdigest()
    if got != digest:
        raise VerbError(f"{ref} arrived as sha256:{got}; refused it and wrote nothing")
    files = unpack(data, dest)
    return {
        "address": version.get("address"),
        "version": version.get("version"),
        "pin": version.get("hash"),
        "sdist": ref,
        "sha256": digest,
        "bytes": len(data),
        "to": str(dest),
        "files": files,
    }
