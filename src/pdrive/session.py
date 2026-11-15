"""LazyBackend: log in to the share only when a request actually needs Proton.

With a warm cache, `pdrive ls` and most MCP calls are answered from SQLite without starting a bridge
or logging in (which costs ~2 s). The first call that does need Proton starts the bridge(s) and logs in.
`close()` drops the connection, and the next call reconnects, which the MCP server uses after the
cache has gone cold.
"""
from __future__ import annotations

import threading
from typing import Any, Callable, Iterable, Optional

from .remote import Backend, DeleteResult, Node, ProgressFn


class LazyBackend:
    def __init__(self, factory: Callable[[], Backend], url: str, password: Callable[[], str]):
        self._factory = factory
        self._url = url
        self._password = password  # resolved only when a login is really needed
        self._lock = threading.Lock()
        self._real: Optional[Backend] = None
        self.root: Optional[Node] = None
        self.logins = 0

    @property
    def connected(self) -> bool:
        with self._lock:
            return self._real is not None

    def connect(self) -> Node:
        """Log in now (if not already connected) and return the share root."""
        self._backend()
        assert self.root is not None
        return self.root

    def _backend(self) -> Backend:
        with self._lock:
            if self._real is None:
                b = self._factory()
                try:
                    self.root = b.auth(self._url, self._password())
                except BaseException:
                    b.close()
                    raise
                self._real = b
                self.logins += 1
            return self._real

    def detach(self) -> Optional[Backend]:
        """Disconnect without closing: return the live backend (or None) for the caller to close later,
        e.g. outside its own locks. The next call logs in again."""
        with self._lock:
            real, self._real = self._real, None
            return real

    def close(self) -> None:
        real = self.detach()
        if real is not None:
            real.close()

    # -- Backend --------------------------------------------------------------
    def auth(self, url: str, password: str) -> Node:  # pragma: no cover - the CLI uses connect()
        return self.connect()

    def set_rate_limit(self, bytes_per_sec: int) -> None:
        self._backend().set_rate_limit(bytes_per_sec)

    def children(self, uid: str) -> list[Node]:
        return self._backend().children(uid)

    def mkdir(self, parent_uid: str, name: str) -> Node:
        return self._backend().mkdir(parent_uid, name)

    def rename(self, uid: str, name: str) -> Node:
        return self._backend().rename(uid, name)

    def delete(self, uids: Iterable[str]) -> list[DeleteResult]:
        return self._backend().delete(uids)

    def download(self, uid: str, local_path: str, progress: Optional[ProgressFn] = None) -> int:
        return self._backend().download(uid, local_path, progress)

    def upload(self, parent_uid: str, name: str, local_path: str, **kw: Any) -> Node:
        return self._backend().upload(parent_uid, name, local_path, **kw)
