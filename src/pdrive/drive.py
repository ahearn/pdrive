"""Drive: path-based view of a share on top of a uid-based Backend, with a per-run listing cache."""
from __future__ import annotations

import threading
from typing import Iterator, Optional

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


class Drive:
    def __init__(self, backend: Backend, root: Node):
        self.backend = backend
        self.root = root
        self._children: dict[str, list[Node]] = {}
        self._lock = threading.RLock()  # transfers create folders from worker threads

    # -- listing --------------------------------------------------------------
    def listdir(self, node: Node) -> list[Node]:
        if not node.is_dir:
            raise NotADirectory(node.name)
        with self._lock:
            if node.uid not in self._children:
                kids = self.backend.children(node.uid)
                self._children[node.uid] = sorted(kids, key=lambda n: n.name)
            return list(self._children[node.uid])

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
        """Yield (path, node) for every descendant of a folder, depth first, parents before children."""
        for kid in self.listdir(node):
            kid_path = remote.join(path, kid.name)
            yield kid_path, kid
            if kid.is_dir:
                yield from self.walk(kid, kid_path)

    def tree_size(self, node: Node) -> int:
        if not node.is_dir:
            return node.size or 0
        return sum(n.size or 0 for _, n in self.walk(node, "/") if not n.is_dir)

    # -- mutation (keeps the cache coherent) -----------------------------------
    def _add(self, parent: Node, node: Node) -> None:
        with self._lock:
            if parent.uid in self._children:
                kids = [n for n in self._children[parent.uid] if n.name != node.name] + [node]
                self._children[parent.uid] = sorted(kids, key=lambda n: n.name)

    def _remove(self, parent_uid: str, uid: str) -> None:
        with self._lock:
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
