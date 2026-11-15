"""Drive: path-based view of a share on top of a uid-based Backend, with a per-run listing cache."""
from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Iterable, Iterator, Optional

from . import remote
from .remote import Backend, Node, PdriveError


class NotFound(PdriveError):
    def __init__(self, path: str):
        super().__init__(f"{path}: No such file or directory")
        self.path = path


class NotADirectory(PdriveError):
    def __init__(self, path: str):
        super().__init__(f"{path}: Not a directory")
        self.path = path


LIST_JOBS = 16  # concurrent folder listings (~0.5 s round trip each); 16 measured best, 24 no faster


class Drive:
    def __init__(self, backend: Backend, root: Node, list_jobs: int = LIST_JOBS):
        self.backend = backend
        self.root = root
        self.list_jobs = list_jobs
        self._children: dict[str, list[Node]] = {}
        self._changes: dict[str, int] = {}  # per-folder mutation count, to detect changes mid-listing
        # Guards the cache and folder creation. Never held across a listing call, so parallel workers
        # can list different folders at the same time.
        self._lock = threading.RLock()

    # -- listing --------------------------------------------------------------
    def listdir(self, node: Node) -> list[Node]:
        if not node.is_dir:
            raise NotADirectory(node.name)
        with self._lock:
            cached = self._children.get(node.uid)
        while cached is None:
            with self._lock:
                seen = self._changes.get(node.uid, 0)
            kids = sorted(self.backend.children(node.uid), key=lambda n: n.name)
            with self._lock:
                if node.uid in self._children:
                    cached = self._children[node.uid]
                elif self._changes.get(node.uid, 0) == seen:  # else: changed while listing; list again
                    cached = self._children[node.uid] = kids
        return list(cached)

    def prefetch(self, folders: Iterable[Node], recursive: bool = True) -> None:
        """List folders concurrently (breadth first when ``recursive``) so later walks hit the cache."""
        level = [f for f in folders if f.is_dir]
        with ThreadPoolExecutor(max_workers=self.list_jobs) as ex:
            while level:
                listings = list(ex.map(self.listdir, level))  # cached folders return immediately
                if not recursive:
                    return
                level = [k for kids in listings for k in kids if k.is_dir]

    def lookup_many(self, paths: Iterable[str]) -> None:
        """Warm the cache for many paths at once, listing each depth level in parallel."""
        by_depth: dict[int, set[str]] = {}
        for p in paths:
            ps = remote.parts(p)
            for i in range(len(ps)):
                by_depth.setdefault(i, set()).add("/" + "/".join(ps[:i]))
        for depth in sorted(by_depth):
            nodes = [n for n in (self._try_lookup(p) for p in by_depth[depth]) if n is not None and n.is_dir]
            self.prefetch(nodes, recursive=False)

    def _try_lookup(self, path: str) -> Optional[Node]:
        try:
            return self.lookup(path)
        except NotADirectory:
            return None

    def child(self, folder: Node, name: str) -> Optional[Node]:
        for n in self.listdir(folder):
            if n.name == name:
                return n
        return None

    def lookup(self, path: str) -> Optional[Node]:
        node = self.root
        walked = "/"
        for part in remote.parts(path):
            if not node.is_dir:
                raise NotADirectory(walked)
            nxt = self.child(node, part)
            if nxt is None:
                return None
            node, walked = nxt, remote.join(walked, part)
        return node

    def stat(self, path: str) -> Node:
        node = self.lookup(path)
        if node is None:
            raise NotFound(remote.normpath(path))
        return node

    def walk(self, node: Node, path: str) -> Iterator[tuple[str, Node]]:
        """Yield (path, node) for every descendant of a folder, depth first, parents before children.
        The whole tree is listed up front, in parallel."""
        self.prefetch([node])
        yield from self._walk(node, path)

    def _walk(self, node: Node, path: str) -> Iterator[tuple[str, Node]]:
        for kid in self.listdir(node):
            kid_path = remote.join(path, kid.name)
            yield kid_path, kid
            if kid.is_dir:
                yield from self._walk(kid, kid_path)

    def tree_size(self, node: Node) -> int:
        if not node.is_dir:
            return node.size or 0
        return sum(n.size or 0 for _, n in self.walk(node, "/") if not n.is_dir)

    # -- mutation (keeps the cache coherent) -----------------------------------
    def _add(self, parent: Node, node: Node) -> None:
        with self._lock:
            self._changes[parent.uid] = self._changes.get(parent.uid, 0) + 1
            if parent.uid in self._children:
                kids = [n for n in self._children[parent.uid] if n.name != node.name] + [node]
                self._children[parent.uid] = sorted(kids, key=lambda n: n.name)

    def _remove(self, parent_uid: str, uid: str) -> None:
        with self._lock:
            self._changes[parent_uid] = self._changes.get(parent_uid, 0) + 1
            if parent_uid in self._children:
                self._children[parent_uid] = [n for n in self._children[parent_uid] if n.uid != uid]
            self._children.pop(uid, None)

    def mkdir(self, path: str, parents: bool = False) -> Node:
        """Create a folder. With ``parents``, create missing ancestors and accept an existing folder."""
        with self._lock:
            parent_path, name = remote.split(path)
            if not name:
                if parents:
                    return self.root
                raise PdriveError("/: File exists")
            parent = self.lookup(parent_path)
            if parent is None:
                if not parents:
                    raise NotFound(parent_path)
                parent = self.mkdir(parent_path, parents=True)
            if not parent.is_dir:
                raise NotADirectory(parent_path)
            existing = self.child(parent, name)
            if existing is not None:
                if parents and existing.is_dir:
                    return existing
                raise PdriveError(f"{remote.normpath(path)}: File exists")
            node = self.backend.mkdir(parent.uid, name)
            self._add(parent, node)
            self._children[node.uid] = []
            return node

    def rename(self, path: str, new_name: str) -> Node:
        if "/" in new_name or new_name in ("", ".", ".."):
            raise PdriveError(f"invalid name: {new_name!r} (rename only changes the name, not the folder)")
        parent_path, _ = remote.split(path)
        node = self.stat(path)
        if node.uid == self.root.uid:
            raise PdriveError("cannot rename the share root")
        parent = self.stat(parent_path)
        if self.child(parent, new_name) is not None:
            raise PdriveError(f"{remote.join(parent_path, new_name)}: File exists")
        renamed = self.backend.rename(node.uid, new_name)
        self._remove(parent.uid, node.uid)
        self._add(parent, renamed)
        return renamed

    def delete(self, items: list[tuple[str, Node]]) -> list[tuple[str, Optional[str]]]:
        """Delete nodes permanently. Returns (path, error-or-None) per item."""
        if not items:
            return []
        results = {r.uid: r for r in self.backend.delete([n.uid for _, n in items])}
        out = []
        for path, node in items:
            r = results.get(node.uid)
            err = None if (r and r.ok) else ((r.error if r else None) or "not deleted")
            if err is None:
                parent = self.lookup(remote.split(path)[0])
                if parent is not None:
                    self._remove(parent.uid, node.uid)
            out.append((path, err))
        return out

    def record_upload(self, parent: Node, node: Node) -> None:
        self._add(parent, node)
