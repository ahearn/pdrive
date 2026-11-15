"""Formatting helpers and the transfer progress reporter (all progress/diagnostics go to stderr)."""
from __future__ import annotations

import sys
import threading
import time
from datetime import datetime
from typing import Optional, TextIO

from .remote import Node


def human(n: Optional[int]) -> str:
    """Bytes as ls -h / du -h style: 999, 1.0K, 12M, 3.4G (binary multiples)."""
    if n is None:
        return "-"
    v = float(n)
    for unit in ("", "K", "M", "G", "T", "P"):
        if v < 1024 or unit == "P":
            if unit == "":
                return str(int(v))
            return f"{v:.1f}{unit}" if v < 10 else f"{v:.0f}{unit}"
        v /= 1024
    return str(n)  # unreachable


def fmt_mtime(iso: Optional[str]) -> str:
    if not iso:
        return "                "
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone()
    except ValueError:
        return iso[:16]
    return dt.strftime("%Y-%m-%d %H:%M")


def long_line(node: Node, name: str, human_sizes: bool, size: Optional[int] = None) -> str:
    kind = "d" if node.is_dir else "-"
    s = node.size if size is None else size
    size_txt = human(s) if human_sizes else ("-" if s is None else str(s))
    return f"{kind} {size_txt:>{6 if human_sizes else 14}} {fmt_mtime(node.mtime)} {name}"


class Reporter:
    """Counts transfers and draws a one-line progress meter when stderr is a terminal."""

    def __init__(self, verb: str, total_files: int, total_bytes: int, *, verbose: bool = False,
                 quiet: bool = False, stream: TextIO = sys.stderr, tty: Optional[bool] = None,
                 clock=time.monotonic):
        self.verb = verb
        self.total_files = total_files
        self.total_bytes = total_bytes
        self.verbose = verbose
        self.quiet = quiet
        self.stream = stream
        self.tty = stream.isatty() if tty is None else tty
        self.clock = clock
        self.started = clock()
        self.done = self.skipped = self.failed = 0
        self.done_bytes = 0
        self._inflight: dict[int, int] = {}
        self._lock = threading.Lock()
        self._last_draw = 0.0
        self._drawn = False

    # called from worker threads
    def progress(self, task_id: int, nbytes: int) -> None:
        with self._lock:
            self._inflight[task_id] = nbytes
            self._draw()

    def finished(self, task_id: int, nbytes: int, line: str) -> None:
        with self._lock:
            self._inflight.pop(task_id, None)
            self.done += 1
            self.done_bytes += nbytes
            if self.verbose:
                self._say(line)
            self._draw(force=True)

    def skip(self, line: str) -> None:
        with self._lock:
            self.skipped += 1
            if self.verbose:
                self._say(line)

    def fail(self, task_id: Optional[int], line: str) -> None:
        with self._lock:
            if task_id is not None:
                self._inflight.pop(task_id, None)
            self.failed += 1
            self._say(f"pdrive: {line}")

    def note(self, line: str) -> None:
        with self._lock:
            self._say(line)

    def summary(self) -> str:
        secs = max(self.clock() - self.started, 1e-9)
        rate = self.done_bytes / secs
        return (f"{self.verb} {self.done} file(s), {human(self.done_bytes)}B in {secs:.0f}s "
                f"({human(int(rate))}B/s); skipped {self.skipped}, failed {self.failed}")

    def close(self) -> None:
        with self._lock:
            self._clear()
            if not self.quiet:
                self.stream.write(self.summary() + "\n")
                self.stream.flush()

    # internals (lock held)
    def _say(self, line: str) -> None:
        self._clear()
        self.stream.write(line + "\n")
        self.stream.flush()

    def _clear(self) -> None:
        if self._drawn:
            self.stream.write("\r\033[K")
            self._drawn = False

    def _draw(self, force: bool = False) -> None:
        if self.quiet or not self.tty:
            return
        now = self.clock()
        if not force and now - self._last_draw < 0.25:
            return
        self._last_draw = now
        moved = self.done_bytes + sum(self._inflight.values())
        secs = max(now - self.started, 1e-9)
        pct = f"{100 * moved / self.total_bytes:5.1f}%" if self.total_bytes else "  -  "
        line = (f"{self.verb} [{self.done + self.skipped + self.failed}/{self.total_files}] {pct} "
                f"{human(moved)}B/{human(self.total_bytes)}B {human(int(moved / secs))}B/s")
        self.stream.write("\r\033[K" + line)
        self.stream.flush()
        self._drawn = True
