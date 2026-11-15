"""Planning and running uploads (put) and downloads (get).

Planning is pure path logic (cp(1) semantics) and is unit-tested against a fake backend; execution runs
the plan on a thread pool with retries for transient errors.
"""
from __future__ import annotations

import fnmatch
import mimetypes
import os
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Callable, Optional, Sequence

from . import remote
from .drive import Drive, NotADirectory, NotFound
from .output import Reporter
from .remote import Node, PdriveError, RemoteError

RETRY_DELAYS = (5.0, 30.0, 120.0)
PARTIAL_SUFFIX = ".pdrive-partial"


class Policy(Enum):
    """What to do when the destination file already exists."""
    DEFAULT = "default"  # skip if the size matches, otherwise report a conflict
    UPDATE = "update"    # skip if the size matches, otherwise overwrite / upload a new revision
    FORCE = "force"      # always overwrite / upload a new revision


class Action(Enum):
    TRANSFER = "transfer"
    REPLACE = "replace"
    SKIP = "skip"
    CONFLICT = "conflict"


def decide(src_size: int, existing_size: Optional[int], exists: bool, policy: Policy) -> Action:
    if not exists:
        return Action.TRANSFER
    if policy is Policy.FORCE:
        return Action.REPLACE
    if existing_size == src_size:
        return Action.SKIP
    return Action.REPLACE if policy is Policy.UPDATE else Action.CONFLICT


def excluded(name: str, patterns: Sequence[str]) -> bool:
    return any(fnmatch.fnmatch(name, p) for p in patterns)


def media_type(name: str) -> str:
    return mimetypes.guess_type(name, strict=False)[0] or "application/octet-stream"


def run_with_retry(fn: Callable[[], object], delays: Sequence[float] = RETRY_DELAYS,
                   sleep: Callable[[float], None] = time.sleep, on_retry: Optional[Callable[[str], None]] = None):
    for attempt in range(len(delays) + 1):
        try:
            return fn()
        except RemoteError as e:
            if not e.retryable or attempt == len(delays):
                raise
            if on_retry:
                on_retry(f"{e} (retrying in {delays[attempt]:.0f}s)")
            sleep(delays[attempt])


# ---------------------------------------------------------------------------- put
@dataclass
class Upload:
    local: str
    remote_dir: str  # remote folder path; created on demand
    name: str
    size: int
    mtime: str

    @property
    def remote_path(self) -> str:
        return remote.join(self.remote_dir, self.name)


@dataclass
class PutPlan:
    uploads: list[Upload] = field(default_factory=list)
    folders: list[str] = field(default_factory=list)  # remote folders to create, parents first
    errors: list[str] = field(default_factory=list)


def _local_upload(local: str, remote_dir: str, name: str) -> Upload:
    st = os.stat(local)
    mtime = datetime.fromtimestamp(st.st_mtime, timezone.utc).isoformat()
    return Upload(local, remote.normpath(remote_dir), name, st.st_size, mtime)


def plan_put(drive: Drive, sources: Sequence[str], dest: str, *, recursive: bool,
             exclude: Sequence[str] = ()) -> PutPlan:
    """cp-style: one source may be renamed to DEST; several sources need DEST to be an existing folder.
    A folder source goes *into* an existing DEST folder, unless it ends in "/" (then its contents do)."""
    plan = PutPlan()
    dest = remote.normpath(dest)
    dest_node = drive.lookup(dest)
    if dest_node is not None and not dest_node.is_dir and (len(sources) > 1 or any(os.path.isdir(s) for s in sources)):
        raise NotADirectory(dest)
    if len(sources) > 1 and dest_node is None:
        raise NotFound(dest)

    for src in sources:
        src_clean = src.rstrip("/") or "/"
        base = os.path.basename(src_clean)
        if not os.path.exists(src):
            plan.errors.append(f"{src}: No such file or directory")
        elif os.path.isdir(src):
            if not recursive:
                plan.errors.append(f"{src}: is a directory (use -r)")
                continue
            # rsync convention: "dir/" means the folder's contents, so re-running a put is idempotent.
            into = dest_node is not None and not src.endswith("/")
            top = remote.join(dest, base) if into else dest
            plan.folders.append(top)
            for dirpath, dirnames, filenames in os.walk(src):
                dirnames[:] = sorted(d for d in dirnames if not excluded(d, exclude))
                rel = os.path.relpath(dirpath, src)
                rdir = top if rel == "." else remote.join(top, rel.replace(os.sep, "/"))
                if rel != ".":
                    plan.folders.append(rdir)
                for fn in sorted(filenames):
                    if excluded(fn, exclude):
                        continue
                    path = os.path.join(dirpath, fn)
                    if os.path.isfile(path):
                        plan.uploads.append(_local_upload(path, rdir, fn))
        elif excluded(base, exclude):
            continue
        elif dest_node is not None and dest_node.is_dir:
            plan.uploads.append(_local_upload(src, dest, base))
        else:  # single file to a new (or existing file) name
            parent, name = remote.split(dest)
            plan.uploads.append(_local_upload(src, parent, name))
    return plan


def run_put(drive: Drive, plan: PutPlan, reporter: Reporter, *, policy: Policy = Policy.DEFAULT,
            jobs: int = 4, dry_run: bool = False, parents: bool = False,
            sleep: Callable[[float], None] = time.sleep) -> int:
    """Execute a put plan. Returns the number of failures (including planning errors and conflicts)."""
    for err in plan.errors:
        reporter.fail(None, err)
    # Skip checks need a listing of every destination folder; fetch them in parallel up front.
    drive.lookup_many(plan.folders + [u.remote_path for u in plan.uploads])
    if dry_run:
        for f in plan.folders:
            if drive.lookup(f) is None:
                reporter.note(f"would create {f}/")
    else:
        for f in plan.folders:
            try:
                drive.mkdir(f, parents=True)
            except PdriveError as e:
                reporter.fail(None, str(e))

    def one(task_id: int, up: Upload) -> None:
        try:
            parent = drive.lookup(up.remote_dir)
            if parent is None:
                if not parents and up.remote_dir not in plan.folders:
                    raise NotFound(up.remote_dir)
                parent = None if dry_run else drive.mkdir(up.remote_dir, parents=True)
            existing = drive.child(parent, up.name) if parent is not None else None
            if existing is not None and existing.is_dir:
                raise PdriveError(f"{up.remote_path}: is a folder")
            action = decide(up.size, existing.size if existing else None, existing is not None, policy)
            if action is Action.SKIP:
                reporter.skip(f"skip {up.local} (same size at {up.remote_path})")
                return
            if action is Action.CONFLICT:
                reporter.fail(task_id, f"{up.remote_path}: exists with a different size "
                                       f"(local {up.size}, remote {existing.size}); use -u or -f to replace")
                return
            verb = "replace" if action is Action.REPLACE else "upload"
            if dry_run:
                reporter.finished(task_id, up.size, f"would {verb} {up.local} -> {up.remote_path}")
                return
            assert parent is not None
            node = run_with_retry(
                lambda: drive.backend.upload(parent.uid, up.name, up.local, size=up.size, mtime=up.mtime,
                                             media_type=media_type(up.name),
                                             replace_uid=existing.uid if existing else None,
                                             progress=lambda b: reporter.progress(task_id, b)),
                sleep=sleep, on_retry=lambda m: reporter.note(f"pdrive: {up.remote_path}: {m}"))
            drive.record_upload(parent, node)  # type: ignore[arg-type]
            reporter.finished(task_id, up.size, f"{verb} {up.local} -> {up.remote_path}")
        except PdriveError as e:
            reporter.fail(task_id, f"{up.local}: {e}")

    _pool(one, plan.uploads, jobs)
    return reporter.failed


# ---------------------------------------------------------------------------- get
@dataclass
class Download:
    node: Node
    remote_path: str
    local: str


@dataclass
class GetPlan:
    downloads: list[Download] = field(default_factory=list)
    folders: list[str] = field(default_factory=list)  # local directories to create
    errors: list[str] = field(default_factory=list)


def plan_get(drive: Drive, sources: Sequence[str], dest: str, *, recursive: bool,
             exclude: Sequence[str] = ()) -> GetPlan:
    plan = GetPlan()
    dest_is_dir = os.path.isdir(dest)
    nodes: list[tuple[str, Optional[Node]]] = [(remote.normpath(s), drive.lookup(s)) for s in sources]
    sources_raw = {remote.normpath(s): s for s in sources}
    if (len(sources) > 1 or any(n is not None and n.is_dir for _, n in nodes)) and os.path.exists(dest) \
            and not dest_is_dir:
        raise PdriveError(f"{dest}: Not a directory")
    if len(sources) > 1 and not dest_is_dir:
        raise PdriveError(f"{dest}: No such directory")

    for path, node in nodes:
        if node is None:
            plan.errors.append(f"{path}: No such file or directory")
            continue
        name = node.name or "share"
        if node.is_dir:
            if not recursive:
                plan.errors.append(f"{path}: is a folder (use -r)")
                continue
            into = dest_is_dir and not sources_raw[path].endswith("/")
            top = os.path.join(dest, name) if into else dest
            plan.folders.append(top)
            pruned: list[str] = []
            for sub_path, sub in drive.walk(node, path):
                rel = sub_path[len(path):].lstrip("/") if path != "/" else sub_path.lstrip("/")
                if excluded(sub.name, exclude) or any(rel.startswith(p + "/") for p in pruned):
                    if sub.is_dir:
                        pruned.append(rel)
                    continue
                local = os.path.join(top, *rel.split("/"))
                if sub.is_dir:
                    plan.folders.append(local)
                else:
                    plan.downloads.append(Download(sub, sub_path, local))
        elif excluded(name, exclude):
            continue
        else:
            local = os.path.join(dest, name) if dest_is_dir else dest
            plan.downloads.append(Download(node, path, local))
    return plan


def run_get(drive: Drive, plan: GetPlan, reporter: Reporter, *, policy: Policy = Policy.DEFAULT,
            jobs: int = 4, dry_run: bool = False, sleep: Callable[[float], None] = time.sleep) -> int:
    for err in plan.errors:
        reporter.fail(None, err)
    for d in plan.folders:
        if dry_run:
            if not os.path.isdir(d):
                reporter.note(f"would create {d}/")
        else:
            try:
                os.makedirs(d, exist_ok=True)
            except OSError as e:
                reporter.fail(None, f"{d}: {e.strerror}")

    def one(task_id: int, dl: Download) -> None:
        try:
            exists = os.path.exists(dl.local)
            if exists and os.path.isdir(dl.local):
                raise PdriveError(f"{dl.local}: is a directory")
            local_size = os.path.getsize(dl.local) if exists else None
            action = decide(dl.node.size or 0, local_size, exists, policy)
            if action is Action.SKIP:
                reporter.skip(f"skip {dl.remote_path} (same size at {dl.local})")
                return
            if action is Action.CONFLICT:
                reporter.fail(task_id, f"{dl.local}: exists with a different size "
                                       f"(local {local_size}, remote {dl.node.size}); use -u or -f to overwrite")
                return
            if dry_run:
                reporter.finished(task_id, dl.node.size or 0, f"would download {dl.remote_path} -> {dl.local}")
                return
            if not os.path.isdir(os.path.dirname(dl.local) or "."):
                raise PdriveError(f"{os.path.dirname(dl.local)}: No such directory")
            tmp = dl.local + PARTIAL_SUFFIX
            try:
                n = run_with_retry(
                    lambda: drive.backend.download(dl.node.uid, tmp, progress=lambda b: reporter.progress(task_id, b)),
                    sleep=sleep, on_retry=lambda m: reporter.note(f"pdrive: {dl.remote_path}: {m}"))
                os.replace(tmp, dl.local)
            finally:
                if os.path.exists(tmp):
                    os.unlink(tmp)
            _set_mtime(dl.local, dl.node.mtime)
            reporter.finished(task_id, int(n), f"download {dl.remote_path} -> {dl.local}")  # type: ignore[arg-type]
        except PdriveError as e:
            reporter.fail(task_id, f"{dl.remote_path}: {e}")
        except OSError as e:
            reporter.fail(task_id, f"{dl.local}: {e.strerror or e}")

    _pool(one, plan.downloads, jobs)
    return reporter.failed


def _set_mtime(path: str, iso: Optional[str]) -> None:
    if not iso:
        return
    try:
        ts = datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()
        os.utime(path, (ts, ts))
    except (ValueError, OSError):
        pass


def _pool(fn: Callable, items: list, jobs: int) -> None:
    if jobs <= 1 or len(items) <= 1:
        for i, item in enumerate(items):
            fn(i, item)
        return
    with ThreadPoolExecutor(max_workers=jobs) as ex:
        for f in [ex.submit(fn, i, item) for i, item in enumerate(items)]:
            f.result()
