"""Persistent SQLite cache of Proton Drive folder listings for the pdrive project."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import time
from collections import deque
from pathlib import Path
from typing import Callable, Mapping, Optional

from .remote import Node


def default_cache_path(env: Mapping[str, str] = os.environ) -> Path:
    """Return the default cache database path from environment variables."""
    if env.get("PDRIVE_CACHE_DIR"):
        base = Path(env["PDRIVE_CACHE_DIR"])
    elif env.get("XDG_CACHE_HOME"):
        base = Path(env["XDG_CACHE_HOME"]) / "pdrive"
    else:
        base = Path.home() / ".cache" / "pdrive"
    return base / "cache.sqlite"


def share_key(share_url: str) -> str:
    """Return sha256 hex digest of the share URL (URL secret never stored)."""
    return hashlib.sha256(share_url.encode("utf-8")).hexdigest()


class CacheStore:
    """Thread-safe persistent SQLite cache for a single share's folder tree."""

    def __init__(
        self,
        path: Path,
        share_url: str,
        ttl_s: float,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._clock = clock
        self.ttl_s = ttl_s
        self._share = share_key(share_url)
        self._path = path
        self._lock = threading.RLock()

        # Ensure directory exists with restrictive permissions.
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(path.parent, 0o700)

        # Create the database file with mode 0o600 before sqlite opens it.
        if not path.exists():
            fd = os.open(str(path), os.O_CREAT | os.O_RDWR, 0o600)
            os.close(fd)
        else:
            os.chmod(str(path), 0o600)

        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        with self._lock:
            cur = self._conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA busy_timeout=5000")
            cur.execute("PRAGMA foreign_keys=OFF")
            cur.executescript(
                """
                CREATE TABLE IF NOT EXISTS meta (
                    share TEXT, key TEXT, value TEXT,
                    PRIMARY KEY (share, key)
                );
                CREATE TABLE IF NOT EXISTS listings (
                    share TEXT, folder_uid TEXT, path TEXT, fetched_at REAL,
                    PRIMARY KEY (share, folder_uid)
                );
                CREATE TABLE IF NOT EXISTS nodes (
                    share TEXT, parent_uid TEXT, uid TEXT, name TEXT,
                    type TEXT, size INTEGER, mtime TEXT, media_type TEXT,
                    PRIMARY KEY (share, parent_uid, uid)
                );
                CREATE TABLE IF NOT EXISTS activity (
                    share TEXT, path TEXT, used_at REAL,
                    PRIMARY KEY (share, path)
                );
                """
            )
            self._conn.commit()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _now(self) -> float:
        return self._clock()

    def _get_meta(self, key: str) -> Optional[str]:
        row = self._conn.execute(
            "SELECT value FROM meta WHERE share=? AND key=?",
            (self._share, key),
        ).fetchone()
        return row[0] if row else None

    def _set_meta(self, key: str, value: str) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO meta (share, key, value) VALUES (?,?,?)",
            (self._share, key, value),
        )

    def _delete_subtree(self, cur: sqlite3.Cursor, root_uid: str, keep_root: bool = False) -> None:
        """Delete the cached listings of root_uid and every folder below it. Unless keep_root, also
        remove root_uid's own entry from its parent's listing."""
        collected: list[str] = [root_uid]
        queue: deque[str] = deque([root_uid])
        while queue:
            uid = queue.popleft()
            rows = cur.execute(
                "SELECT uid FROM nodes WHERE share=? AND parent_uid=? AND type='folder'",
                (self._share, uid),
            ).fetchall()
            for (child_uid,) in rows:
                collected.append(child_uid)
                queue.append(child_uid)
        if not collected:
            return
        placeholders = ",".join("?" * len(collected))
        cur.execute(
            f"DELETE FROM listings WHERE share=? AND folder_uid IN ({placeholders})",
            (self._share, *collected),
        )
        cur.execute(
            f"DELETE FROM nodes WHERE share=? AND parent_uid IN ({placeholders})",
            (self._share, *collected),
        )
        if keep_root:
            return
        # Remove the root node itself from its parent's listing.
        cur.execute(
            "DELETE FROM nodes WHERE share=? AND uid=?",
            (self._share, root_uid),
        )

    def _node_from_row(self, row: sqlite3.Row) -> Node:
        return Node(
            uid=row[2],
            name=row[3],
            type=row[4],
            size=row[5],
            mtime=row[6],
            media_type=row[7],
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def begin_use(self, path: Optional[str] = None) -> bool:
        """Start a user command; purge cache if cold. Return True if was warm."""
        now = self._now()
        with self._lock:
            cur = self._conn.cursor()
            last_used = self._get_meta("last_used")
            was_warm = last_used is not None and (now - float(last_used)) < self.ttl_s

            if not was_warm:
                # Cache expired — wipe all rows for this share.
                cur.execute("DELETE FROM meta WHERE share=?", (self._share,))
                cur.execute("DELETE FROM listings WHERE share=?", (self._share,))
                cur.execute("DELETE FROM nodes WHERE share=?", (self._share,))
                cur.execute("DELETE FROM activity WHERE share=?", (self._share,))

            self._set_meta("last_used", str(now))

            if path is not None:
                cur.execute(
                    "INSERT OR REPLACE INTO activity (share, path, used_at) VALUES (?,?,?)",
                    (self._share, path, now),
                )

            self._conn.commit()
            return was_warm

    def touch(self, path: str) -> None:
        """Record activity and refresh last_used without expiry check."""
        now = self._now()
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO activity (share, path, used_at) VALUES (?,?,?)",
                (self._share, path, now),
            )
            self._set_meta("last_used", str(now))
            self._conn.commit()

    def is_warm(self) -> bool:
        """Return True if cache is within the TTL window."""
        with self._lock:
            last = self._get_meta("last_used")
            if last is None:
                return False
            return (self._now() - float(last)) < self.ttl_s

    def last_used(self) -> Optional[float]:
        """Return the last-used timestamp or None if never used."""
        with self._lock:
            val = self._get_meta("last_used")
            return float(val) if val is not None else None

    def get_root(self) -> Optional[Node]:
        """Return cached root node if warm, else None."""
        with self._lock:
            if not self.is_warm():
                return None
            val = self._get_meta("root")
            if val is None:
                return None
            return Node(**json.loads(val))

    def set_root(self, node: Node) -> None:
        """Persist the root node as JSON in meta."""
        with self._lock:
            self._set_meta("root", json.dumps({
                "uid": node.uid,
                "name": node.name,
                "type": node.type,
                "size": node.size,
                "mtime": node.mtime,
                "media_type": node.media_type,
            }))
            self._conn.commit()

    def get_listing(self, folder_uid: str) -> Optional[tuple[list[Node], float]]:
        """Return (children sorted by name, fetched_at) if warm and cached."""
        with self._lock:
            if not self.is_warm():
                return None
            row = self._conn.execute(
                "SELECT fetched_at FROM listings WHERE share=? AND folder_uid=?",
                (self._share, folder_uid),
            ).fetchone()
            if row is None:
                return None
            fetched_at = row[0]
            rows = self._conn.execute(
                "SELECT share, parent_uid, uid, name, type, size, mtime, media_type "
                "FROM nodes WHERE share=? AND parent_uid=? ORDER BY name",
                (self._share, folder_uid),
            ).fetchall()
            children = [self._node_from_row(r) for r in rows]
            return (children, fetched_at)

    def put_listing(
        self,
        folder_uid: str,
        kids: list[Node],
        path: Optional[str] = None,
        fetched_at: Optional[float] = None,
    ) -> None:
        """Replace a folder's cached listing; purge orphaned subtrees."""
        now = self._now()
        if fetched_at is None:
            fetched_at = now
        new_uids = {k.uid for k in kids}

        with self._lock:
            cur = self._conn.cursor()

            # Identify old child folders that are no longer present.
            old_folder_rows = cur.execute(
                "SELECT uid FROM nodes WHERE share=? AND parent_uid=? AND type='folder'",
                (self._share, folder_uid),
            ).fetchall()
            for (old_uid,) in old_folder_rows:
                if old_uid not in new_uids:
                    self._delete_subtree(cur, old_uid)

            # Remove old children rows for this folder.
            cur.execute(
                "DELETE FROM nodes WHERE share=? AND parent_uid=?",
                (self._share, folder_uid),
            )

            # Insert new children.
            for k in kids:
                cur.execute(
                    "INSERT OR REPLACE INTO nodes "
                    "(share, parent_uid, uid, name, type, size, mtime, media_type) "
                    "VALUES (?,?,?,?,?,?,?,?)",
                    (self._share, folder_uid, k.uid, k.name, k.type,
                     k.size, k.mtime, k.media_type),
                )

            # Upsert the listing row; keep existing path if new path is None.
            existing = cur.execute(
                "SELECT path FROM listings WHERE share=? AND folder_uid=?",
                (self._share, folder_uid),
            ).fetchone()
            final_path = path if path is not None else (existing[0] if existing else None)
            cur.execute(
                "INSERT OR REPLACE INTO listings (share, folder_uid, path, fetched_at) "
                "VALUES (?,?,?,?)",
                (self._share, folder_uid, final_path, fetched_at),
            )

            self._conn.commit()

    def add_child(self, parent_uid: str, node: Node) -> None:
        """Insert/replace child in parent's listing if parent is cached."""
        with self._lock:
            cur = self._conn.cursor()
            exists = cur.execute(
                "SELECT 1 FROM listings WHERE share=? AND folder_uid=?",
                (self._share, parent_uid),
            ).fetchone()
            if exists is None:
                return
            # Replace any existing child with the same name.
            cur.execute(
                "DELETE FROM nodes WHERE share=? AND parent_uid=? AND name=? AND uid!=?",
                (self._share, parent_uid, node.name, node.uid),
            )
            cur.execute(
                "INSERT OR REPLACE INTO nodes "
                "(share, parent_uid, uid, name, type, size, mtime, media_type) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (self._share, parent_uid, node.uid, node.name, node.type,
                 node.size, node.mtime, node.media_type),
            )
            self._conn.commit()

    def remove_child(self, parent_uid: str, uid: str) -> None:
        """Remove a child node and its cached subtree if it was a folder."""
        with self._lock:
            cur = self._conn.cursor()
            row = cur.execute(
                "SELECT type FROM nodes WHERE share=? AND parent_uid=? AND uid=?",
                (self._share, parent_uid, uid),
            ).fetchone()
            if row is None:
                return
            was_folder = row[0] == "folder"
            cur.execute(
                "DELETE FROM nodes WHERE share=? AND parent_uid=? AND uid=?",
                (self._share, parent_uid, uid),
            )
            if was_folder:
                self._delete_subtree(cur, uid)
            self._conn.commit()

    def forget_listing(self, folder_uid: str) -> None:
        """Drop the cached listing of one folder and everything cached below it (the folder's own
        entry in its parent stays). The next listdir fetches it from Proton again."""
        with self._lock:
            self._delete_subtree(self._conn.cursor(), folder_uid, keep_root=True)
            self._conn.commit()

    def folders(self) -> list[tuple[str, Optional[str], float]]:
        """Return (folder_uid, path, fetched_at) for every cached listing."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT folder_uid, path, fetched_at FROM listings WHERE share=?",
                (self._share,),
            ).fetchall()
            return [(r[0], r[1], r[2]) for r in rows]

    def recent_activity(self, limit: int = 20) -> list[tuple[str, float]]:
        """Return (path, used_at) entries, newest first."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT path, used_at FROM activity WHERE share=? "
                "ORDER BY used_at DESC LIMIT ?",
                (self._share, limit),
            ).fetchall()
            return [(r[0], r[1]) for r in rows]

    def flush(self) -> int:
        """Delete all cached rows for this share. Return listings removed count."""
        with self._lock:
            cur = self._conn.cursor()
            count = cur.execute(
                "SELECT COUNT(*) FROM listings WHERE share=?",
                (self._share,),
            ).fetchone()[0]
            cur.execute("DELETE FROM meta WHERE share=?", (self._share,))
            cur.execute("DELETE FROM listings WHERE share=?", (self._share,))
            cur.execute("DELETE FROM nodes WHERE share=?", (self._share,))
            cur.execute("DELETE FROM activity WHERE share=?", (self._share,))
            self._conn.commit()
            return count

    def stats(self) -> dict:
        """Return summary statistics of the current cache state."""
        with self._lock:
            listings = self._conn.execute(
                "SELECT COUNT(*) FROM listings WHERE share=?", (self._share,)
            ).fetchone()[0]
            nodes = self._conn.execute(
                "SELECT COUNT(*) FROM nodes WHERE share=?", (self._share,)
            ).fetchone()[0]
            return {
                "warm": self.is_warm(),
                "listings": listings,
                "nodes": nodes,
                "last_used": self.last_used(),
                "ttl_s": self.ttl_s,
                "path": str(self._path),
            }

    def close(self) -> None:
        """Close the underlying database connection."""
        with self._lock:
            self._conn.close()
