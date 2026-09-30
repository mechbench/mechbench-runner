from __future__ import annotations

import hashlib
import re
import shutil
from pathlib import Path

ID_RULE = re.compile(r"[A-Za-z0-9_-]{1,64}")
MARKER = ".mechbench-owned"


class PathRefusedError(ValueError):
    pass


def check_id(what: str, ident: object) -> str:
    if not isinstance(ident, str) or not ID_RULE.fullmatch(ident):
        raise PathRefusedError(
            f"refused {what} {ident!r}: an id is 1 to 64 characters of "
            f"A-Z, a-z, 0-9, '_' and '-'"
        )
    return ident


def hashed(ident: str) -> str:
    return hashlib.sha256(ident.encode()).hexdigest()[:32]


def under(root: Path, *parts: str, what: str, ident: object) -> Path:
    base = root.resolve()
    for part in parts:
        if (not isinstance(part, str) or not part or "\0" in part
                or Path(part).is_absolute()):
            raise PathRefusedError(
                f"refused {what} {ident!r}: {part!r} is not a relative path")
    path = base.joinpath(*parts).resolve()
    if base not in path.parents:
        raise PathRefusedError(f"refused {what} {ident!r}: {path} is outside {base}")
    return path


def owned_dir(root: Path, what: str, ident: object) -> Path:
    return under(root, hashed(check_id(what, ident)), what=what, ident=ident)


def owner_of(path: Path) -> str | None:
    marker = path / MARKER
    if path.is_symlink() or not marker.is_file():
        return None
    ident = marker.read_text().strip()
    return ident if ID_RULE.fullmatch(ident) and path.name == hashed(ident) else None


def make_owned(path: Path, ident: str, *, adopt: bool = False) -> Path:
    marker = path / MARKER
    if path.is_symlink():
        raise PathRefusedError(f"refused {ident!r}: {path} is a symlink")
    if marker.is_file():
        if marker.read_text().strip() != ident:
            raise PathRefusedError(f"refused {ident!r}: {path} belongs to another id")
        return path
    if path.exists() and not adopt:
        raise PathRefusedError(f"refused {ident!r}: {path} exists and the runner "
                               f"did not create it")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    marker.write_text(ident)
    return path


def remove_owned(owner: Path, *parts: str) -> None:
    if not owner.exists() and not owner.is_symlink():
        return
    if owner_of(owner) is None:
        raise PathRefusedError(
            f"refused to delete {owner}: the runner did not create it")
    raw = owner.joinpath(*parts)
    if raw.is_symlink():
        raise PathRefusedError(f"refused to delete {raw}: it is a symlink")
    target = (under(owner, *parts, what="spool path", ident=owner.name)
              if parts else owner)
    if target.is_dir():
        shutil.rmtree(target)
    elif target.exists():
        target.unlink()
