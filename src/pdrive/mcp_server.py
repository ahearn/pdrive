"""MCP server: the share's read, search and transfer operations as tools for Claude.

`pdrive --mcp` serves streamable HTTP on 127.0.0.1 (port 14912 by default). Deliberately missing:
rm and rename. The server shares the SQLite listing cache with the CLI:

- The cache stays warm for ``cache_ttl`` minutes after the last request (CLI or MCP).
- While warm, a background thread re-lists one folder per ``refresh_interval`` seconds (default 60),
  choosing stale folders near the paths used recently (see refresh.pick_refresh).
- When the cache goes cold, the Proton sessions are closed. The next request logs in again.
"""
from __future__ import annotations

import fnmatch
import inspect
import io
import socket
import sys
import threading
import time
from contextlib import contextmanager
from typing import Any, Callable, Iterable, Iterator, Optional, TextIO

from . import remote
from .cache import CacheStore
from .drive import Drive
from .output import Reporter, human
from .refresh import pick_refresh
from .remote import Backend, Node, PdriveError
from .session import LazyBackend
from .transfer import Policy, plan_get, plan_put, run_get, run_put

HOST = "127.0.0.1"
MAX_ENTRIES = 500


def setup_text(port: int, share_name: str, ttl_min: float) -> str:
    url = f"http://{HOST}:{port}/mcp"
    return f"""\
pdrive MCP server for share {share_name!r} is listening on {url}
(cache stays warm {ttl_min:g} min after the last use; Ctrl-C stops the server)

Add it to Claude Code:
  claude mcp add --transport http pdrive {url}
  (add --scope user to make it available in every project; check with: claude mcp list)

Or put this in a project's .mcp.json:
  {{"mcpServers": {{"pdrive": {{"type": "http", "url": "{url}"}}}}}}

Tools: ls, stat, du, search, get, put, mkdir, flush_cache, cache_status (no rm or rename).
"""


class ServerState:
    """The share connection, cache and background refresher shared by all tool calls."""

    def __init__(self, store: CacheStore, backend: LazyBackend, list_jobs: int, refresh_interval: float,
                 clock: Callable[[], float] = time.time):
        self.store = store
        self.backend = backend
        self.list_jobs = list_jobs
        self.refresh_interval = refresh_interval
        self.clock = clock
        self.refreshed: list[str] = []  # uids re-listed by the refresher (for status and tests)
        self._calls_lock = threading.Lock()
        self._active = 0  # tool calls in progress; background upkeep stands aside while > 0
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def root(self) -> Node:
        root = self.store.get_root()
        if root is None:
            root = self.backend.connect()
            self.store.set_root(root)
        return root

    @contextmanager
    def drive(self, paths: Iterable[str] = ()) -> Iterator[Drive]:
        """A Drive for one tool call. Counts as a use: it keeps the cache warm."""
        with self._calls_lock:
            self._active += 1
        try:
            self.store.begin_use()
            for p in paths:
                self.store.touch(remote.normpath(p))
            d = Drive(self.backend, self.root(), list_jobs=self.list_jobs, store=self.store)
            try:
                yield d
            finally:
                d.stop()
        finally:
            with self._calls_lock:
                self._active -= 1

    @property
    def active_calls(self) -> int:
        with self._calls_lock:
            return self._active

    # -- background upkeep ------------------------------------------------------
    def tick(self) -> Optional[str]:
        """One round of upkeep. Cold cache: drop the Proton sessions. Warm: re-list the most useful
        stale folder. Returns the uid refreshed, if any. Never counts as a use.

        Upkeep must never hold up a real request, so it stands aside: it skips the round while any tool
        call is running, never logs in by itself (a login holds the session lock for ~2 s), and drops
        the sessions only when no call is using them (deciding under the same lock a call takes)."""
        with self._calls_lock:
            if self._active:
                return None
            detached = None if self.store.is_warm() else self.backend.detach()
        if detached is not None or not self.store.is_warm():
            if detached is not None:
                detached.close()  # outside the lock: a new call can already log in again
            return None
        if not self.backend.connected:
            return None
        uid = pick_refresh(self.store.folders(), self.store.recent_activity(), self.clock())
        root = self.store.get_root()
        if uid is None or root is None:
            return None
        Drive(self.backend, root, list_jobs=1, store=self.store).refresh(uid)
        self.refreshed.append(uid)
        return uid

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, name="pdrive-refresh", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(5)
        self.backend.close()
        self.store.close()

    def _loop(self) -> None:
        while not self._stop.wait(self.refresh_interval):
            try:
                self.tick()
            except Exception as e:  # keep the server alive; the next tick tries again
                print(f"pdrive: background refresh: {e}", file=sys.stderr)


# ------------------------------------------------------------------------------- tools
def _entry(path: str, n: Node) -> dict[str, Any]:
    return {"path": path, "name": n.name, "type": n.type, "size": n.size, "mtime": n.mtime}


def _policy(update: bool, force: bool) -> Policy:
    if update and force:
        raise PdriveError("update and force are mutually exclusive")
    return Policy.FORCE if force else Policy.UPDATE if update else Policy.DEFAULT


def _transfer_result(rep: Reporter, failed: int, log: io.StringIO, limit: int = 200) -> dict[str, Any]:
    lines = log.getvalue().splitlines()
    return {"ok": failed == 0, "transferred": rep.done, "bytes": rep.done_bytes, "skipped": rep.skipped,
            "failed": rep.failed, "summary": rep.summary(), "log": lines[:limit],
            "log_truncated": len(lines) > limit}


class Tools:
    """Tool implementations. Plain methods so they can be tested without an MCP transport."""

    def __init__(self, state: ServerState):
        self.s = state

    def ls(self, path: str = "/", recursive: bool = False, limit: int = MAX_ENTRIES) -> dict[str, Any]:
        with self.s.drive([path]) as d:
            p = remote.normpath(path)
            node = d.stat(p)
            if not node.is_dir:
                return {"path": p, "entries": [_entry(p, node)], "truncated": False}
            it = d.walk(node, p) if recursive else ((remote.join(p, k.name), k) for k in d.listdir(node, p))
            entries: list[dict[str, Any]] = []
            for sub_path, sub in it:
                if len(entries) >= limit:
                    return {"path": p, "entries": entries, "truncated": True}
                entries.append(_entry(sub_path, sub))
            return {"path": p, "entries": entries, "truncated": False}

    def stat(self, path: str) -> dict[str, Any]:
        with self.s.drive([path]) as d:
            p = remote.normpath(path)
            node = d.stat(p)
            return {**_entry(p, node), "uid": node.uid, "media_type": node.media_type}

    def du(self, path: str = "/", max_depth: int = 1) -> dict[str, Any]:
        if max_depth < 0:
            raise PdriveError("max_depth must be 0 or more")
        with self.s.drive([path]) as d:
            p = remote.normpath(path)
            node = d.stat(p)
            rows: list[dict[str, Any]] = []

            def walk(n: Node, q: str, depth: int) -> int:
                total = 0
                for k in d.listdir(n, q):
                    total += walk(k, remote.join(q, k.name), depth + 1) if k.is_dir else (k.size or 0)
                if 0 < depth <= max_depth:
                    rows.append({"path": q, "bytes": total, "human": human(total) + "B"})
                return total

            if node.is_dir:
                d.start_prefetch(node, p)
                total = walk(node, p, 0)
            else:
                total = node.size or 0
            return {"path": p, "bytes": total, "human": human(total) + "B",
                    "children": sorted(rows, key=lambda r: r["path"])[:MAX_ENTRIES]}

    def search(self, pattern: str, path: str = "/", type: Optional[str] = None,
               limit: int = 200) -> dict[str, Any]:
        if type not in (None, "file", "folder"):
            raise PdriveError("type must be 'file', 'folder' or omitted")
        pat = pattern if any(c in pattern for c in "*?[") else f"*{pattern}*"
        with self.s.drive([path]) as d:
            p = remote.normpath(path)
            matches: list[dict[str, Any]] = []
            for sub_path, sub in d.walk(d.stat(p), p):
                if (type is None or sub.type == type) and fnmatch.fnmatch(sub.name.lower(), pat.lower()):
                    matches.append(_entry(sub_path, sub))
                    if len(matches) >= limit:
                        return {"pattern": pat, "path": p, "matches": matches, "truncated": True}
            return {"pattern": pat, "path": p, "matches": matches, "truncated": False}

    def get(self, remote_paths: list[str], local_dir: str, recursive: bool = False, update: bool = False,
            force: bool = False, dry_run: bool = False) -> dict[str, Any]:
        policy = _policy(update, force)
        with self.s.drive(remote_paths) as d:
            plan = plan_get(d, remote_paths, local_dir, recursive=recursive)
            log = io.StringIO()
            rep = Reporter("would download" if dry_run else "downloaded", len(plan.downloads),
                           sum(x.node.size or 0 for x in plan.downloads), verbose=True, stream=log, tty=False)
            failed = run_get(d, plan, rep, policy=policy, jobs=4, dry_run=dry_run)
            return _transfer_result(rep, failed, log)

    def put(self, local_paths: list[str], remote_dir: str, recursive: bool = False, update: bool = False,
            force: bool = False, parents: bool = False, dry_run: bool = False) -> dict[str, Any]:
        policy = _policy(update, force)
        with self.s.drive([remote_dir]) as d:
            plan = plan_put(d, local_paths, remote_dir, recursive=recursive)
            log = io.StringIO()
            rep = Reporter("would upload" if dry_run else "uploaded", len(plan.uploads),
                           sum(u.size for u in plan.uploads), verbose=True, stream=log, tty=False)
            failed = run_put(d, plan, rep, policy=policy, jobs=4, dry_run=dry_run, parents=parents)
            return _transfer_result(rep, failed, log)

    def mkdir(self, path: str, parents: bool = True) -> dict[str, Any]:
        with self.s.drive([path]) as d:
            node = d.mkdir(path, parents=parents)
            return _entry(remote.normpath(path), node)

    def flush_cache(self) -> dict[str, Any]:
        return {"flushed_listings": self.s.store.flush()}

    def cache_status(self) -> dict[str, Any]:
        st = self.s.store.stats()
        last = st.get("last_used")
        return {**st, "idle_s": None if last is None else round(self.s.clock() - last),
                "connected_to_proton": self.s.backend.connected,
                "background_refreshes": len(self.s.refreshed)}


TOOL_DOCS = {
    "ls": "List a folder of the Proton Drive share (paths are relative to the share root, e.g. '/Movies/A'). "
          "recursive=True lists the whole subtree. At most `limit` entries; `truncated` says if more exist.",
    "stat": "Details of one file or folder: type, size (bytes), modified time, uid, media type.",
    "du": "Total size of a folder, with per-subfolder totals down to max_depth levels (0 = total only).",
    "search": "Find files/folders whose name matches a pattern under `path` (case-insensitive). Plain text "
              "matches anywhere in the name; use * and ? for globs. type: 'file', 'folder' or omit.",
    "get": "Download remote files (or folders with recursive=True) into local_dir on this machine. Existing "
           "local files of the same size are skipped; update=True overwrites differing ones, force=True all. "
           "dry_run=True shows what would happen.",
    "put": "Upload local files (or folders with recursive=True) into remote_dir. A trailing '/' on a local "
           "folder uploads its contents. Same-size remote files are skipped; update=True uploads a new "
           "revision when the size differs, force=True always. parents=True creates missing remote folders. "
           "dry_run=True shows what would happen.",
    "mkdir": "Create a remote folder (with parents=True, missing parents too and no error if it exists).",
    "flush_cache": "Forget all cached folder listings, so the next calls list fresh from Proton.",
    "cache_status": "Listing-cache statistics: warm or cold, entries, idle time, Proton connection.",
}


def build_server(tools: Tools):
    from mcp.server.mcpserver import MCPServer
    from mcp.server.mcpserver.exceptions import ToolError
    from mcp.types import ToolAnnotations

    server = MCPServer(name="pdrive", instructions=(
        "Tools for one Proton Drive share (the share link the user logged in to with pdrive). "
        "Paths are relative to the share root. Listings come from a cache that is refreshed in "
        "the background; call flush_cache if something looks out of date. Deleting and renaming are not "
        "available."), log_level="WARNING")
    read = ToolAnnotations(read_only_hint=True, open_world_hint=True)
    write = ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=True)
    local_write = ToolAnnotations(read_only_hint=False, destructive_hint=True, open_world_hint=True)

    def wrap(fn):
        def call(*a, **kw):
            try:
                return fn(*a, **kw)
            except PdriveError as e:
                raise ToolError(str(e)) from None
        call.__name__, call.__doc__ = fn.__name__, TOOL_DOCS[fn.__name__]
        call.__signature__ = inspect.signature(fn, eval_str=True)  # real types for the schema
        return call

    for name, ann in (("ls", read), ("stat", read), ("du", read), ("search", read), ("get", local_write),
                      ("put", write), ("mkdir", write), ("flush_cache", write), ("cache_status", read)):
        server.tool(name=name, description=TOOL_DOCS[name], annotations=ann)(wrap(getattr(tools, name)))
    return server


def transport_security():
    """Reject requests whose Host or Origin isn't this machine's loopback address. Binding to 127.0.0.1
    isn't enough by itself: a web page can reach it through DNS rebinding, and `put` can upload any local file."""
    from mcp.server.transport_security import TransportSecuritySettings
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=["127.0.0.1:*", "localhost:*"],
        allowed_origins=["http://127.0.0.1:*", "http://localhost:*"],
    )


def port_free(port: int, host: str = HOST) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind((host, port))
        except OSError:
            return False
    return True


def serve(state: ServerState, port: int, ttl_min: float, out: TextIO = sys.stderr) -> int:
    try:
        import mcp  # noqa: F401
    except ImportError:
        raise PdriveError("the MCP server needs the 'mcp' package: reinstall pdrive (it's a dependency)") from None
    if not port_free(port):
        raise PdriveError(f"port {port} on {HOST} is already in use (another pdrive --mcp?); "
                          f"pick another with --port")
    root = state.backend.connect()  # log in now: a bad password fails at startup, not on the first call
    state.store.set_root(root)
    server = build_server(Tools(state))
    out.write(setup_text(port, root.name, ttl_min))
    out.flush()
    state.start()
    try:
        server.run("streamable-http", host=HOST, port=port, transport_security=transport_security())
    except KeyboardInterrupt:
        pass
    finally:
        state.stop()
    return 0
