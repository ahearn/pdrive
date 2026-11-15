"""Drive: path-based view of a share on top of a uid-based Backend, with a per-run listing cache."""
from __future__ import annotations

import heapq
import itertools
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING, Callable, Iterable, Iterator, Optional

from . import remote
from .remote import Backend, Node, PdriveError, RemoteError

if TYPE_CHECKING:
    from .cache import CacheStore


class NotFound(PdriveError):
    def __init__(self, path: str):
        super().__init__(f"{path}: No such file or directory")
        self.path = path


class NotADirectory(PdriveError):
    def __init__(self, path: str):
        super().__init__(f"{path}: Not a directory")
        self.path = path


LIST_JOBS = 16  # concurrent folder listings (~0.5 s round trip each); 16 measured best, 24 no faster


class _Prefetcher:
    """Lists a folder tree in the background with ``jobs`` workers.

    Workers always take the pending folder that comes first in depth-first output order, so the
    subtrees a depth-first consumer (du, ls -R, a recursive get) needs first are finished first. The
    consumer can print as it goes instead of waiting for the whole tree."""

    def __init__(self, drive: "Drive", root: Node, jobs: int, path: Optional[str] = None):
        self.drive = drive
        self.heap: list[tuple[tuple[int, ...], int, Node, Optional[str]]] = [((), 0, root, path)]
        self.seq = itertools.count(1)
        self.cv = threading.Condition()
        self.active = 0
        self.stopped = False
        self.threads = [threading.Thread(target=self._work, name="pdrive-prefetch", daemon=True)
                        for _ in range(jobs)]
        for t in self.threads:
            t.start()

    def stop(self) -> None:
        with self.cv:
            self.stopped = True
            self.cv.notify_all()

    def _work(self) -> None:
        while True:
            with self.cv:
                while not self.heap and self.active and not self.stopped:
                    self.cv.wait()
                if self.stopped or not self.heap:  # stopped, or the whole tree is listed
                    self.cv.notify_all()
                    return
                key, _, node, path = heapq.heappop(self.heap)
                self.active += 1
            try:
                kids = self.drive.listdir(node, path)
            except Exception:  # the consumer lists this folder itself and reports the error
                kids = []
            with self.cv:
                for i, kid in enumerate(kids):
                    if kid.is_dir:
                        kid_path = remote.join(path, kid.name) if path is not None else None
                        heapq.heappush(self.heap, (key + (i,), next(self.seq), kid, kid_path))
                self.active -= 1
                self.cv.notify_all()


class Drive:
    def __init__(self, backend: Backend, root: Node, list_jobs: int = LIST_JOBS,
                 store: Optional["CacheStore"] = None):
        self.backend = backend
        self.root = root
        self.store = store  # persistent listing cache shared with other pdrive processes, or None
        self.cache_hits = 0
        self.list_jobs = list_jobs
        self.on_list: Optional[Callable[[int], None]] = None  # called with the running count of listings
        self.listed = 0
        self._children: dict[str, list[Node]] = {}
        self._changes: dict[str, int] = {}  # per-folder mutation count, to detect changes mid-listing
        self._inflight: dict[str, threading.Event] = {}
        self._prefetchers: list[_Prefetcher] = []
        # Guards the cache. Never held across a network call, so parallel workers can list different
        # folders at the same time.
        self._lock = threading.RLock()
        self._mkdir_lock = threading.RLock()  # serialises folder creation (check-then-create)

    # -- listing --------------------------------------------------------------
    def listdir(self, node: Node, path: Optional[str] = None) -> list[Node]:
        """Children of a folder, sorted by name. Each folder is listed at most once at a time; callers
        that ask for a folder already being listed wait for that listing. ``path`` (when known) is
        stored with the cached listing so the background refresher can focus on recent paths."""
        if not node.is_dir:
            raise NotADirectory(node.name)
        while True:
            with self._lock:
                cached = self._children.get(node.uid)
                if cached is not None:
                    return list(cached)
                event = self._inflight.get(node.uid)
                owner = event is None
                if owner:
                    event = self._inflight[node.uid] = threading.Event()
                    seen = self._changes.get(node.uid, 0)
            if not owner:
                event.wait()
                continue  # now cached, or the other lister failed and this caller tries itself
            try:
                hit = self.store.get_listing(node.uid) if self.store else None
                if hit is not None:
                    with self._lock:
                        self._children.setdefault(node.uid, hit[0])
                        self.cache_hits += 1
                    continue
                kids = sorted(self.backend.children(node.uid), key=lambda n: n.name)
                if self.store:
                    self.store.put_listing(node.uid, kids, path)
                with self._lock:
                    # Changed while listing (e.g. an upload finished)? Then loop and list again.
                    if node.uid not in self._children and self._changes.get(node.uid, 0) == seen:
                        self._children[node.uid] = kids
                    self.listed += 1
                    count = self.listed
                if self.on_list:
                    self.on_list(count)
            finally:
                with self._lock:
                    self._inflight.pop(node.uid, None)
                event.set()

    def start_prefetch(self, folder: Node, path: Optional[str] = None) -> None:
        """List ``folder``'s whole tree in the background (see _Prefetcher). Returns immediately."""
        if folder.is_dir:
            self._prefetchers.append(_Prefetcher(self, folder, self.list_jobs, path))

    def stop(self) -> None:
        """Stop background listing (call before closing the backend)."""
        for p in self._prefetchers:
            p.stop()

    def list_many(self, folders: Iterable[Node]) -> None:
        """List several folders concurrently and wait for all of them."""
        folders = [f for f in folders if f.is_dir]
        if len(folders) <= 1:
            for f in folders:
                self.listdir(f)
            return
        with ThreadPoolExecutor(max_workers=self.list_jobs) as ex:
            list(ex.map(self.listdir, folders))

    def lookup_many(self, paths: Iterable[str]) -> None:
        """Warm the cache for many paths at once, listing each depth level in parallel."""
        by_depth: dict[int, set[str]] = {}
        for p in paths:
            ps = remote.parts(p)
            for i in range(len(ps)):
                by_depth.setdefault(i, set()).add("/" + "/".join(ps[:i]))
        try:  # only warms the cache: a failure here resurfaces, per file, when the work runs
            for depth in sorted(by_depth):
                nodes = [n for n in (self._try_lookup(p) for p in by_depth[depth]) if n is not None and n.is_dir]
                self.list_many(nodes)
        except PdriveError:
            pass

    def _try_lookup(self, path: str) -> Optional[Node]:
        try:
            return self.lookup(path)
        except NotADirectory:
            return None

    def child(self, folder: Node, name: str) -> Optional[Node]:
        return self._child_at(folder, None, name)

    def _child_at(self, folder: Node, folder_path: Optional[str], name: str) -> Optional[Node]:
        for n in self.listdir(folder, folder_path):
            if n.name == name:
                return n
        return None

    def lookup(self, path: str) -> Optional[Node]:
        node = self.root
        walked = "/"
        for part in remote.parts(path):
            if not node.is_dir:
                raise NotADirectory(walked)
            nxt = self._child_at(node, walked, part)
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
        The tree is listed in parallel in the background, so results start arriving right away."""
        self.start_prefetch(node, remote.normpath(path))
        yield from self._walk(node, path)

    def _walk(self, node: Node, path: str) -> Iterator[tuple[str, Node]]:
        for kid in self.listdir(node, remote.normpath(path)):
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
        if self.store:
            self.store.add_child(parent.uid, node)
        with self._lock:
            self._changes[parent.uid] = self._changes.get(parent.uid, 0) + 1
            if parent.uid in self._children:
                kids = [n for n in self._children[parent.uid] if n.name != node.name] + [node]
                self._children[parent.uid] = sorted(kids, key=lambda n: n.name)

    def _remove(self, parent_uid: str, uid: str) -> None:
        if self.store:
            self.store.remove_child(parent_uid, uid)
        with self._lock:
            self._changes[parent_uid] = self._changes.get(parent_uid, 0) + 1
            if parent_uid in self._children:
                self._children[parent_uid] = [n for n in self._children[parent_uid] if n.uid != uid]
            self._children.pop(uid, None)

    def mkdir(self, path: str, parents: bool = False) -> Node:
        """Create a folder. With ``parents``, create missing ancestors and accept an existing folder."""
        with self._mkdir_lock:
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
            try:
                node = self.backend.mkdir(parent.uid, name)
            except RemoteError as e:
                if not (parents and e.code == "exists"):
                    raise
                # Another process created it meanwhile: re-list the parent and use that folder.
                self.forget(parent.uid)
                existing = self.child(parent, name)
                if existing is None or not existing.is_dir:
                    raise
                return existing
            self._add(parent, node)
            with self._lock:
                self._children[node.uid] = []
            if self.store:
                self.store.put_listing(node.uid, [], remote.normpath(path))
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

    def forget(self, folder_uid: str) -> None:
        """Drop one folder's listing from both caches, so the next use lists it from Proton."""
        with self._lock:
            self._children.pop(folder_uid, None)
            self._changes[folder_uid] = self._changes.get(folder_uid, 0) + 1
        if self.store:
            self.store.forget_listing(folder_uid)

    def refresh(self, folder_uid: str) -> list[Node]:
        """List a folder from Proton even if cached, and store the result (background refresher)."""
        kids = sorted(self.backend.children(folder_uid), key=lambda n: n.name)
        if self.store:
            self.store.put_listing(folder_uid, kids)
        with self._lock:
            self._children[folder_uid] = kids
        return kids

    def flush_cache(self) -> int:
        """Forget every cached listing, in memory and on disk. Returns how many were on disk."""
        with self._lock:
            self._children.clear()
        return self.store.flush() if self.store else 0

    def record_upload(self, parent: Node, node: Node) -> None:
        self._add(parent, node)
