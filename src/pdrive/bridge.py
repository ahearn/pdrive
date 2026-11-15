"""BridgeBackend: talks JSON lines to the Node helper (bridge_js/bridge.ts) that wraps the Proton Drive SDK."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

from .remote import DeleteResult, Node, PdriveError, ProgressFn, RemoteError, explain

BRIDGE_DIR = Path(__file__).parent / "bridge_js"
FNM_DEFAULT_NODE = Path.home() / ".local/share/fnm/aliases/default/bin/node"


def find_node(configured: Optional[str] = None) -> str:
    """Locate a Node >= 22 binary: config value, then PATH, then fnm's default alias (absent under systemd)."""
    for cand in (configured, shutil.which("node"), str(FNM_DEFAULT_NODE)):
        if cand and os.access(cand, os.X_OK):
            return cand
    raise PdriveError("node not found; install Node.js or set it with: pdrive config set node /path/to/node")


def bridge_command(node: str, bridge_dir: Path = BRIDGE_DIR, dns: str = "system") -> list[str]:
    """Prefer the pre-built bundle (made by the installer, or ``npm run build``). It contains the SDK,
    so it needs no node_modules and starts ~0.4 s faster. Fall back to running bridge.ts through tsx
    when the bundle is missing, or is older than bridge.ts during development."""
    cmd = [node, "--conditions=browser"]
    if dns == "cloudflare":
        cmd += ["--require", str(bridge_dir / "dns-cloudflare.cjs")]
    src, bundle = bridge_dir / "bridge.ts", bridge_dir / "dist" / "bridge.mjs"
    if bundle.exists() and (not src.exists() or bundle.stat().st_mtime >= src.stat().st_mtime):
        return cmd + [str(bundle)]
    if not (bridge_dir / "node_modules" / "@protontech" / "drive-sdk").is_dir():
        raise PdriveError(f"bridge not built; run: npm ci --prefix {bridge_dir} && npm run build --prefix {bridge_dir}")
    return cmd + ["--import", "tsx", str(src)]


class _Call:
    def __init__(self, on_event: Optional[Callable[[dict], None]]):
        self.done = threading.Event()
        self.on_event = on_event
        self.response: Optional[dict] = None


class BridgeBackend:
    """Backend over a long-lived bridge process. Thread-safe: calls from several threads run concurrently
    in the bridge and are matched to responses by id."""

    def __init__(self, cmd: list[str], cwd: Optional[Path] = None, env: Optional[dict] = None):
        self._proc = subprocess.Popen(cmd, cwd=str(cwd or BRIDGE_DIR), env=env, stdin=subprocess.PIPE,
                                      stdout=subprocess.PIPE, text=True, encoding="utf-8", bufsize=1)
        self._lock = threading.Lock()
        self._calls: dict[int, _Call] = {}
        self._next_id = 0
        self._reader = threading.Thread(target=self._read_loop, name="pdrive-bridge-reader", daemon=True)
        self._reader.start()

    # -- plumbing ---------------------------------------------------------
    def _read_loop(self) -> None:
        assert self._proc.stdout is not None
        for line in self._proc.stdout:
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue  # stray output; the protocol is strictly one JSON object per line
            call = self._calls.get(msg.get("id"))
            if call is None:
                continue
            if "event" in msg:
                if call.on_event:
                    call.on_event(msg)
            else:
                call.response = msg
                call.done.set()
        # Process ended: fail everything still waiting.
        with self._lock:
            for call in self._calls.values():
                if not call.done.is_set():
                    call.response = {"ok": False, "error": {"code": "bridge", "message": "bridge process exited"}}
                    call.done.set()

    def call(self, op: str, on_event: Optional[Callable[[dict], None]] = None, **params: Any) -> Any:
        with self._lock:
            if self._proc.poll() is not None:
                raise RemoteError("bridge", "bridge process exited")
            self._next_id += 1
            cid = self._next_id
            call = self._calls[cid] = _Call(on_event)
            assert self._proc.stdin is not None
            try:
                self._proc.stdin.write(json.dumps({"id": cid, "op": op, **params}) + "\n")
                self._proc.stdin.flush()
            except BrokenPipeError:
                del self._calls[cid]
                raise RemoteError("bridge", "bridge process exited") from None
        call.done.wait()
        with self._lock:
            del self._calls[cid]
        resp = call.response or {}
        if not resp.get("ok"):
            err = resp.get("error") or {}
            raise RemoteError(err.get("code", "error"), err.get("message", "unknown bridge error"))
        return resp.get("result")

    @staticmethod
    def _progress(progress: Optional[ProgressFn]) -> Optional[Callable[[dict], None]]:
        if progress is None:
            return None
        return lambda ev: progress(int(ev.get("bytes", 0))) if ev.get("event") == "progress" else None

    # -- Backend ------------------------------------------------------------
    def auth(self, url: str, password: str) -> Node:
        return Node.from_json(self.call("auth", url=url, password=password)["root"])

    def set_rate_limit(self, bytes_per_sec: int) -> None:
        self.call("configure", rate_bps=int(bytes_per_sec))

    def children(self, uid: str) -> list[Node]:
        return [Node.from_json(d) for d in self.call("children", uid=uid)]

    def mkdir(self, parent_uid: str, name: str) -> Node:
        return Node.from_json(self.call("mkdir", parent=parent_uid, name=name))

    def rename(self, uid: str, name: str) -> Node:
        return Node.from_json(self.call("rename", uid=uid, name=name))

    def delete(self, uids: Iterable[str]) -> list[DeleteResult]:
        out = self.call("delete", uids=list(uids))
        return [DeleteResult(r["uid"], r["ok"], None if r["ok"] else
                             explain((r.get("error") or {}).get("code", "error"), (r.get("error") or {}).get("message", "")))
                for r in out]

    def download(self, uid: str, local_path: str, progress: Optional[ProgressFn] = None) -> int:
        return int(self.call("download", self._progress(progress), uid=uid, path=local_path)["bytes"])

    def upload(self, parent_uid: str, name: str, local_path: str, *, size: int, mtime: Optional[str],
               media_type: str, replace_uid: Optional[str] = None,
               progress: Optional[ProgressFn] = None) -> Node:
        return Node.from_json(self.call("upload", self._progress(progress), parent=parent_uid, name=name,
                                        path=local_path, size=size, mtime=mtime, media_type=media_type,
                                        replace_uid=replace_uid))

    def close(self) -> None:
        if self._proc.stdin and not self._proc.stdin.closed:
            try:
                self._proc.stdin.close()
            except BrokenPipeError:
                pass
        try:
            self._proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self._proc.terminate()
            self._proc.wait(timeout=5)
        self._reader.join(timeout=2)


class BridgePool:
    """Backend over several bridge processes, each with its own Proton session.

    Listing is CPU-bound: the SDK decrypts every entry's key and name (~18 ms each), and Node does that
    on one core. Big walks and transfers therefore go to the least-busy of up to ``max_bridges``
    processes. Extra bridges start lazily, in the background, once calls queue up (``busy`` calls in
    flight on every bridge), so short commands like ``ls`` run a single process.

    Changes (mkdir, rename, delete) always use the first bridge. Uploads do too while a rate limit is
    set, because the limit is enforced per process.
    """

    def __init__(self, spawn: Callable[[], Any], max_bridges: int = 8, busy: int = 2):
        self._spawn = spawn
        self.max_bridges = max(1, max_bridges)
        self.busy = busy
        self._lock = threading.Lock()
        self._bridges: list[Any] = []
        self._load: dict[int, int] = {}
        self._starting = 0
        self._creds: Optional[tuple[str, str]] = None
        self._rate = 0
        self._closed = False
        self._growers: list[threading.Thread] = []

    @property
    def size(self) -> int:
        with self._lock:
            return len(self._bridges)

    @property
    def primary(self):
        return self._bridges[0]

    def auth(self, url: str, password: str) -> Node:
        b = self._spawn()
        try:
            root = b.auth(url, password)
        except Exception:
            b.close()
            raise
        with self._lock:
            self._creds = (url, password)
            self._bridges = [b]
            self._load = {id(b): 0}
        return root

    def _grow(self) -> None:
        b = None
        try:
            b = self._spawn()
            assert self._creds is not None
            b.auth(*self._creds)
        except Exception:  # an extra bridge is an optimisation; carry on with the ones we have
            if b is not None:
                b.close()
            b = None
        with self._lock:
            self._starting -= 1
            if b is not None and not self._closed:
                self._bridges.append(b)
                self._load[id(b)] = 0
                b = None
        if b is not None:  # pool closed while this bridge was starting
            b.close()

    def _acquire(self, spread: bool):
        with self._lock:
            if self._closed or not self._bridges:
                raise RemoteError("bridge", "not authenticated")
            b = min(self._bridges, key=lambda x: self._load[id(x)]) if spread else self._bridges[0]
            if (spread and self._load[id(b)] >= self.busy
                    and len(self._bridges) + self._starting < self.max_bridges):
                self._starting += 1
                t = threading.Thread(target=self._grow, name="pdrive-bridge-start", daemon=True)
                self._growers.append(t)
                t.start()
            self._load[id(b)] += 1
            return b

    def _use(self, fn: Callable[[Any], Any], spread: bool = True):
        b = self._acquire(spread)
        try:
            return fn(b)
        finally:
            with self._lock:
                self._load[id(b)] -= 1

    # -- Backend -------------------------------------------------------------
    def set_rate_limit(self, bytes_per_sec: int) -> None:
        self._rate = int(bytes_per_sec)
        self._use(lambda b: b.set_rate_limit(bytes_per_sec), spread=False)

    def children(self, uid: str) -> list[Node]:
        return self._use(lambda b: b.children(uid))

    def mkdir(self, parent_uid: str, name: str) -> Node:
        return self._use(lambda b: b.mkdir(parent_uid, name), spread=False)

    def rename(self, uid: str, name: str) -> Node:
        return self._use(lambda b: b.rename(uid, name), spread=False)

    def delete(self, uids: Iterable[str]) -> list[DeleteResult]:
        uids = list(uids)
        return self._use(lambda b: b.delete(uids), spread=False)

    def download(self, uid: str, local_path: str, progress: Optional[ProgressFn] = None) -> int:
        return self._use(lambda b: b.download(uid, local_path, progress))

    def upload(self, parent_uid: str, name: str, local_path: str, **kw) -> Node:
        return self._use(lambda b: b.upload(parent_uid, name, local_path, **kw), spread=not self._rate)

    def close(self) -> None:
        with self._lock:
            self._closed = True
            bridges, self._bridges = self._bridges, []
        closers = [threading.Thread(target=b.close) for b in bridges]
        for t in closers:
            t.start()
        for t in closers + self._growers:
            t.join(15)
