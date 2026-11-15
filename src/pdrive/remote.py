"""Remote node model, the Backend protocol and remote path helpers."""
from __future__ import annotations

import posixpath
from dataclasses import dataclass
from typing import Callable, Iterable, Optional, Protocol

ProgressFn = Callable[[int], None]


class PdriveError(Exception):
    """An error reported to the user as ``pdrive: <message>`` with a non-zero exit status."""

    exit_code = 1


class UsageError(PdriveError):
    exit_code = 2


class RemoteError(PdriveError):
    """An error returned by the Proton side. ``code`` is a short machine-readable class."""

    def __init__(self, code: str, message: str):
        super().__init__(explain(code, message))
        self.code = code

    @property
    def retryable(self) -> bool:
        return self.code in ("network", "rate_limited")


NOT_OWNER_HINT = ("a share link may only rename/delete items created in the same session; "
                  "use the owner's Proton account (drive.proton.me) for this")


def explain(code: str, message: str) -> str:
    """Add a hint to errors whose Proton wording doesn't say what to do."""
    if code == "not_owner":
        return f"{message.rstrip('.')} ({NOT_OWNER_HINT})"
    return message


@dataclass(frozen=True)
class Node:
    uid: str
    name: str
    type: str  # "file" | "folder"
    size: Optional[int] = None
    mtime: Optional[str] = None  # ISO-8601, UTC
    media_type: Optional[str] = None

    @property
    def is_dir(self) -> bool:
        return self.type == "folder"

    @classmethod
    def from_json(cls, d: dict) -> "Node":
        return cls(uid=d["uid"], name=d.get("name") or "", type=d["type"], size=d.get("size"),
                   mtime=d.get("mtime"), media_type=d.get("media_type"))


@dataclass(frozen=True)
class DeleteResult:
    uid: str
    ok: bool
    error: Optional[str] = None


class Backend(Protocol):
    """Single, uid-based operations against one authenticated share."""

    def auth(self, url: str, password: str) -> Node: ...
    def set_rate_limit(self, bytes_per_sec: int) -> None: ...
    def children(self, uid: str) -> list[Node]: ...
    def mkdir(self, parent_uid: str, name: str) -> Node: ...
    def rename(self, uid: str, name: str) -> Node: ...
    def delete(self, uids: Iterable[str]) -> list[DeleteResult]: ...
    def download(self, uid: str, local_path: str, progress: Optional[ProgressFn] = None) -> int: ...
    def upload(self, parent_uid: str, name: str, local_path: str, *, size: int, mtime: Optional[str],
               media_type: str, replace_uid: Optional[str] = None,
               progress: Optional[ProgressFn] = None) -> Node: ...
    def close(self) -> None: ...


def normpath(path: str) -> str:
    """Normalise a remote path to an absolute form. Remote paths are always relative to the share root,
    so ``Movies/A`` and ``/Movies/A/`` are the same folder."""
    p = posixpath.normpath("/" + (path or "").strip())
    if p.startswith("//"):
        p = "/" + p.lstrip("/")
    return p


def split(path: str) -> tuple[str, str]:
    """Split a normalised path into (parent, name). The root's name is ''."""
    p = normpath(path)
    if p == "/":
        return "/", ""
    parent, name = posixpath.split(p)
    return parent or "/", name


def join(parent: str, name: str) -> str:
    return normpath(posixpath.join(normpath(parent), name))


def parts(path: str) -> list[str]:
    return [x for x in normpath(path).split("/") if x]
