from __future__ import annotations

import itertools
import os
import shutil
from dataclasses import dataclass, field
from typing import Optional

import pytest

from pdrive.drive import Drive
from pdrive.remote import DeleteResult, Node, RemoteError

PASSWORD = "s3cret"
URL = "https://drive.proton.me/urls/TEST#key"


@dataclass
class FakeEntry:
    uid: str
    name: str
    type: str
    parent: Optional[str]
    data: bytes = b""
    mtime: str = "2026-01-02T03:04:05.000Z"
    revisions: int = 1

    def node(self) -> Node:
        return Node(self.uid, self.name, self.type, None if self.type == "folder" else len(self.data),
                    self.mtime, None)


@dataclass
class FakeBackend:
    """In-memory share. ``failures`` maps an op name to a list of RemoteErrors raised on successive calls."""

    entries: dict = field(default_factory=dict)
    calls: list = field(default_factory=list)
    failures: dict = field(default_factory=dict)
    rate: int = 0
    closed: bool = False

    def __post_init__(self):
        self._ids = itertools.count(1)
        self.entries["root"] = FakeEntry("root", "Share", "folder", None)

    # helpers for tests
    def add(self, path: str, data: Optional[bytes] = None) -> FakeEntry:
        parent = "root"
        parts = [p for p in path.split("/") if p]
        for i, part in enumerate(parts):
            last = i == len(parts) - 1
            kid = next((e for e in self.entries.values() if e.parent == parent and e.name == part), None)
            if kid is None:
                kind = "file" if (last and data is not None) else "folder"
                kid = FakeEntry(f"u{next(self._ids)}", part, kind, parent, data or b"")
                self.entries[kid.uid] = kid
            parent = kid.uid
        return self.entries[parent]

    def find(self, path: str) -> Optional[FakeEntry]:
        cur = self.entries["root"]
        for part in [p for p in path.split("/") if p]:
            cur = next((e for e in self.entries.values() if e.parent == cur.uid and e.name == part), None)
            if cur is None:
                return None
        return cur

    def _maybe_fail(self, op: str):
        queue = self.failures.get(op)
        if queue:
            raise queue.pop(0)

    # Backend protocol
    def auth(self, url, password):
        self.calls.append(("auth",))
        if password != PASSWORD:
            raise RemoteError("auth", "authentication failed: wrong password")
        return self.entries["root"].node()

    def set_rate_limit(self, bps):
        self.rate = bps

    def children(self, uid):
        self.calls.append(("children", uid))
        return [e.node() for e in self.entries.values() if e.parent == uid]

    def mkdir(self, parent_uid, name):
        self.calls.append(("mkdir", parent_uid, name))
        if any(e.parent == parent_uid and e.name == name for e in self.entries.values()):
            raise RemoteError("exists", "already exists")
        e = FakeEntry(f"u{next(self._ids)}", name, "folder", parent_uid)
        self.entries[e.uid] = e
        return e.node()

    def rename(self, uid, name):
        self.calls.append(("rename", uid, name))
        self.entries[uid].name = name
        return self.entries[uid].node()

    def delete(self, uids):
        uids = list(uids)
        self.calls.append(("delete", uids))
        out = []
        for uid in uids:
            if uid not in self.entries:
                out.append(DeleteResult(uid, False, "not found"))
                continue
            stack = [uid]
            while stack:
                u = stack.pop()
                stack += [e.uid for e in self.entries.values() if e.parent == u]
                self.entries.pop(u, None)
            out.append(DeleteResult(uid, True))
        return out

    def download(self, uid, local_path, progress=None):
        self.calls.append(("download", uid))
        self._maybe_fail("download")
        data = self.entries[uid].data
        with open(local_path, "wb") as f:
            f.write(data)
        if progress:
            progress(len(data))
        return len(data)

    def upload(self, parent_uid, name, local_path, *, size, mtime, media_type, replace_uid=None, progress=None):
        self.calls.append(("upload", parent_uid, name, replace_uid))
        self._maybe_fail("upload")
        with open(local_path, "rb") as f:
            data = f.read()
        assert len(data) == size
        if replace_uid:
            e = self.entries[replace_uid]
            e.data, e.revisions = data, e.revisions + 1
        else:
            if any(x.parent == parent_uid and x.name == name for x in self.entries.values()):
                raise RemoteError("exists", "already exists")
            e = FakeEntry(f"u{next(self._ids)}", name, "file", parent_uid, data)
            self.entries[e.uid] = e
        if progress:
            progress(size)
        return e.node()

    def close(self):
        self.closed = True


@pytest.fixture
def backend() -> FakeBackend:
    b = FakeBackend()
    b.add("Movies/A/Alien (1979).mkv", b"x" * 100)
    b.add("Movies/B/Blade Runner.mkv", b"y" * 250)
    b.add("Music/song.mp3", b"z" * 10)
    b.add("readme.txt", b"hello")
    b.add("Empty")
    return b


@pytest.fixture
def drive(backend) -> Drive:
    return Drive(backend, backend.auth(URL, PASSWORD))


@pytest.fixture
def tree(tmp_path):
    """A small local tree: src/{a.txt, sub/b.bin, sub/.DS_Store}."""
    src = tmp_path / "src"
    (src / "sub").mkdir(parents=True)
    (src / "a.txt").write_bytes(b"aaaa")
    (src / "sub" / "b.bin").write_bytes(b"b" * 20)
    (src / "sub" / ".DS_Store").write_bytes(b"junk")
    return src


@pytest.fixture
def no_sleep():
    return lambda s: None
