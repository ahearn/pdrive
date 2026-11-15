import threading
import time

import pytest

from pdrive.bridge import BridgePool
from pdrive.remote import DeleteResult, Node, RemoteError


class FakeBridge:
    def __init__(self, *, auth_event=None, auth_raises=None, children_gate=None):
        self.calls: list[tuple] = []
        self.closed = False
        self._auth_event = auth_event
        self._auth_raises = auth_raises
        self.children_gate = children_gate

    def auth(self, url: str, password: str) -> Node:
        self.calls.append(("auth", url, password))
        if self._auth_event is not None:
            assert self._auth_event.wait(timeout=10)
        if self._auth_raises is not None:
            raise self._auth_raises
        return Node("root", "Share", "folder")

    def set_rate_limit(self, bytes_per_sec: int) -> None:
        self.calls.append(("set_rate_limit", bytes_per_sec))

    def children(self, uid: str) -> list[Node]:
        self.calls.append(("children", uid))
        if self.children_gate is not None:
            assert self.children_gate.wait(timeout=10)
        return [Node("c1", "c1", "file")]

    def mkdir(self, parent_uid: str, name: str) -> Node:
        self.calls.append(("mkdir", parent_uid, name))
        return Node("new", name, "folder")

    def rename(self, uid: str, name: str) -> Node:
        self.calls.append(("rename", uid, name))
        return Node(uid, name, "file")

    def delete(self, uids) -> list[DeleteResult]:
        self.calls.append(("delete", list(uids)))
        return [DeleteResult(u, True) for u in uids]

    def download(self, uid: str, local_path: str, progress=None) -> int:
        self.calls.append(("download", uid, local_path))
        return 42

    def upload(self, parent_uid: str, name: str, local_path: str, *, size, mtime, media_type,
               replace_uid=None, progress=None) -> Node:
        self.calls.append(("upload", parent_uid, name, local_path))
        return Node("u1", name, "file")

    def close(self) -> None:
        self.calls.append(("close",))
        self.closed = True


def _make_spawn(instances, *, auth_event=None, auth_raises=None):
    def spawn():
        b = FakeBridge(auth_event=auth_event, auth_raises=auth_raises)
        instances.append(b)
        return b
    return spawn


def _poll_until(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def _hold_primary_busy(pool, instances):
    """Park one children() call on the primary so a least-busy pick would choose another bridge.
    Returns (event to release it, thread)."""
    hold = threading.Event()
    for b in instances:
        b.children_gate = None
    pool.primary.children_gate = hold
    t = threading.Thread(target=lambda: pool.children("root"))
    t.start()
    assert _poll_until(lambda: any(c[0] == "children" for c in pool.primary.calls[-1:]), timeout=5.0)
    return hold, t


# --------------------------------------------------------------------------
# 1. Single call uses one bridge
# --------------------------------------------------------------------------
def test_single_call_uses_one_bridge():
    instances: list[FakeBridge] = []
    pool = BridgePool(spawn=_make_spawn(instances), max_bridges=4, busy=5)
    pool.auth("http://x", "pw")
    for _ in range(3):
        pool.children("root")
    assert len(instances) == 1
    assert pool.size == 1


# --------------------------------------------------------------------------
# 2. Pool grows under load up to max_bridges
# --------------------------------------------------------------------------
def test_grows_under_load_up_to_max():
    instances: list[FakeBridge] = []
    gate = threading.Event()
    spawn_count = [0]

    def spawn():
        spawn_count[0] += 1
        b = FakeBridge(children_gate=gate)  # every bridge stays busy until the gate opens
        instances.append(b)
        return b

    pool = BridgePool(spawn=spawn, max_bridges=3, busy=1)
    pool.auth("http://x", "pw")
    instances[0].children_gate = gate

    threads = []
    for _ in range(6):
        t = threading.Thread(target=lambda: pool.children("root"))
        t.start()
        threads.append(t)

    # Poll until pool reaches 3 bridges (grow threads added them)
    assert _poll_until(lambda: pool.size >= 3, timeout=5.0), "pool never reached size 3"

    # Release the gate so children() calls complete
    gate.set()

    for t in threads:
        t.join(timeout=5)
        assert not t.is_alive()

    assert pool.size == 3
    assert len(instances) == 3

    for b in instances:
        auth_calls = [c for c in b.calls if c[0] == "auth"]
        assert len(auth_calls) == 1
        assert auth_calls[0][1] == "http://x"
        assert auth_calls[0][2] == "pw"


# --------------------------------------------------------------------------
# 3. Changes (mkdir, rename, delete) always go to the primary bridge
# --------------------------------------------------------------------------
def test_changes_go_to_primary():
    instances: list[FakeBridge] = []
    gate = threading.Event()

    def spawn():
        b = FakeBridge(children_gate=gate)  # every bridge stays busy until the gate opens
        instances.append(b)
        return b

    pool = BridgePool(spawn=spawn, max_bridges=3, busy=1)
    pool.auth("http://x", "pw")
    instances[0].children_gate = gate

    # Grow the pool to 3 via load
    threads = []
    for _ in range(6):
        t = threading.Thread(target=lambda: pool.children("root"))
        t.start()
        threads.append(t)
    assert _poll_until(lambda: pool.size >= 3, timeout=5.0)
    gate.set()
    for t in threads:
        t.join(timeout=5)
        assert not t.is_alive()

    assert pool.size == 3
    primary = pool.primary

    # Now issue changes while the primary is the busiest bridge – they must still land on it
    hold, holder = _hold_primary_busy(pool, instances)
    pool.mkdir("root", "folder1")
    pool.rename("c1", "renamed")
    pool.delete(["c1", "c2"])
    hold.set()
    holder.join(timeout=5)

    mkdir_calls = [c for c in primary.calls if c[0] == "mkdir"]
    rename_calls = [c for c in primary.calls if c[0] == "rename"]
    delete_calls = [c for c in primary.calls if c[0] == "delete"]
    assert len(mkdir_calls) == 1
    assert len(rename_calls) == 1
    assert len(delete_calls) == 1

    # None of the other bridges should have received change ops
    for b in instances[1:]:
        ops = [c[0] for c in b.calls]
        assert "mkdir" not in ops
        assert "rename" not in ops
        assert "delete" not in ops


# --------------------------------------------------------------------------
# 4. Rate-limit pins uploads to primary; without it uploads may spread
# --------------------------------------------------------------------------
def test_rate_limit_pins_uploads_to_primary():
    instances: list[FakeBridge] = []
    gate = threading.Event()

    def spawn():
        b = FakeBridge(children_gate=gate)  # every bridge stays busy until the gate opens
        instances.append(b)
        return b

    pool = BridgePool(spawn=spawn, max_bridges=3, busy=1)
    pool.auth("http://x", "pw")
    instances[0].children_gate = gate

    # Grow pool to 3
    threads = []
    for _ in range(6):
        t = threading.Thread(target=lambda: pool.children("root"))
        t.start()
        threads.append(t)
    assert _poll_until(lambda: pool.size >= 3, timeout=5.0)
    gate.set()
    for t in threads:
        t.join(timeout=5)
        assert not t.is_alive()

    assert pool.size == 3

    # Set rate limit – uploads must go to primary, even while it is the busiest bridge
    pool.set_rate_limit(1000)
    hold, holder = _hold_primary_busy(pool, instances)

    upload_threads = []
    for i in range(4):
        t = threading.Thread(
            target=lambda: pool.upload("root", f"f{i}", "/tmp/f{i}",
                                       size=10, mtime=None, media_type="text/plain")
        )
        t.start()
        upload_threads.append(t)
    for t in upload_threads:
        t.join(timeout=5)
        assert not t.is_alive()

    hold.set()
    holder.join(timeout=5)
    primary = pool.primary
    upload_calls_on_primary = [c for c in primary.calls if c[0] == "upload"]
    assert len(upload_calls_on_primary) == 4
    for b in instances[1:]:
        assert not any(c[0] == "upload" for c in b.calls)


# --------------------------------------------------------------------------
# 5. A failed extra bridge is silently ignored; primary keeps working
# --------------------------------------------------------------------------
def test_failed_extra_bridge_is_ignored():
    instances: list[FakeBridge] = []
    spawn_count = [0]

    def spawn():
        spawn_count[0] += 1
        if spawn_count[0] > 1:
            raise RuntimeError("spawn boom")
        b = FakeBridge()
        instances.append(b)
        return b

    pool = BridgePool(spawn=spawn, max_bridges=3, busy=1)
    pool.auth("http://x", "pw")

    # Generate load that would trigger _grow; the extra bridges will fail
    gate = threading.Event()
    instances[0].children_gate = gate
    threads = []
    for _ in range(6):
        t = threading.Thread(target=lambda: pool.children("root"))
        t.start()
        threads.append(t)

    # Give _grow threads a moment to fail
    time.sleep(0.05)
    gate.set()
    for t in threads:
        t.join(timeout=5)
        assert not t.is_alive()

    # Pool still works on primary
    result = pool.children("root")
    assert isinstance(result, list)
    assert pool.size == 1


# --------------------------------------------------------------------------
# 6. close() closes every bridge including one still starting
# --------------------------------------------------------------------------
def test_close_closes_all():
    instances: list[FakeBridge] = []
    unblock_auth = threading.Event()
    spawn_count = [0]

    def spawn():
        spawn_count[0] += 1
        if spawn_count[0] > 1:
            b = FakeBridge(auth_event=unblock_auth)
        else:
            b = FakeBridge()
        instances.append(b)
        return b

    pool = BridgePool(spawn=spawn, max_bridges=2, busy=1)
    pool.auth("http://x", "pw")

    # Make primary busy so _acquire triggers _grow
    gate = threading.Event()
    instances[0].children_gate = gate

    # Two calls: the second finds the primary busy, which starts an extra bridge.
    callers = [threading.Thread(target=lambda: pool.children("root")) for _ in range(2)]
    for c in callers:
        c.start()

    # Wait until _grow has spawned bridge 1 (now blocking in auth)
    assert _poll_until(lambda: spawn_count[0] >= 2, timeout=5.0)

    # Unblock auth shortly after close() starts blocking on the grower thread
    def _unblock_later():
        time.sleep(0.05)
        unblock_auth.set()

    unblocker = threading.Thread(target=_unblock_later)
    unblocker.start()

    gate.set()  # let the children() calls finish
    for c in callers:
        c.join(timeout=5)

    pool.close()
    unblocker.join(timeout=5)

    assert len(instances) == 2
    for b in instances:
        assert b.closed, f"bridge was not closed: {b.calls}"
        assert ("close",) in b.calls


# --------------------------------------------------------------------------
# 7. Auth failure on the first bridge raises and closes the bridge
# --------------------------------------------------------------------------
def test_auth_failure_closes_bridge():
    instances: list[FakeBridge] = []
    err = RemoteError("auth", "bad")

    def spawn():
        b = FakeBridge(auth_raises=err)
        instances.append(b)
        return b

    pool = BridgePool(spawn=spawn)
    with pytest.raises(RemoteError) as exc_info:
        pool.auth("http://x", "pw")
    assert exc_info.value.code == "auth"

    assert len(instances) == 1
    assert instances[0].closed
    assert ("close",) in instances[0].calls
