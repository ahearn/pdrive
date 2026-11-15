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
    """Prefer the pre-built bundle (``npm run build``; starts ~0.4 s faster). Fall back to running the
    TypeScript through tsx when the bundle is missing or older than bridge.ts."""
    if not (bridge_dir / "node_modules" / "@protontech" / "drive-sdk").is_dir():
        raise PdriveError(f"bridge dependencies missing; run: npm ci --prefix {bridge_dir}")
    cmd = [node, "--conditions=browser"]
    if dns == "cloudflare":
        cmd += ["--require", str(bridge_dir / "dns-cloudflare.cjs")]
    src, bundle = bridge_dir / "bridge.ts", bridge_dir / "dist" / "bridge.mjs"
    if bundle.exists() and bundle.stat().st_mtime >= src.stat().st_mtime:
        return cmd + [str(bundle)]
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
