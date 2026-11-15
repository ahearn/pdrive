"""Tests for pdrive.cache.CacheStore."""
import concurrent.futures
import os
import stat
from pathlib import Path

import pytest

from pdrive.cache import CacheStore, share_key
from pdrive.remote import Node


@pytest.fixture()
def now():
    return [1000.0]


@pytest.fixture()
def store(tmp_path, now):
    cache = CacheStore(
        tmp_path / "c" / "cache.sqlite",
        "https://x/urls/T#k",
        ttl_s=1800,
        clock=lambda: now[0],
    )
    yield cache
    cache.close()


# ---------------------------------------------------------------------------
# Cold cache basics
# ---------------------------------------------------------------------------


def test_cold_cache_get_listing_returns_none(store):
    assert store.get_listing("any") is None


def test_cold_cache_get_root_returns_none(store):
    assert store.get_root() is None


def test_begin_use_cold_returns_false(store):
    assert store.begin_use() is False


def test_begin_use_warm_returns_true(store):
    store.begin_use()  # cold -> False
    assert store.begin_use() is True  # warm within TTL


def test_begin_use_sets_last_used(store, now):
    store.begin_use()
    assert store.last_used() == 1000.0


# ---------------------------------------------------------------------------
# put_listing / get_listing round trip
# ---------------------------------------------------------------------------


def test_put_get_listing_round_trip(store):
    store.begin_use()
    kids = [
        Node("u2", "beta.txt", "file", 10),
        Node("u1", "alpha.txt", "file", 5),
    ]
    store.put_listing("root_uid", kids, path="/", fetched_at=1050.0)
    result = store.get_listing("root_uid")
    assert result is not None
    children, fetched_at = result
    assert fetched_at == 1050.0
    assert [n.name for n in children] == ["alpha.txt", "beta.txt"]


def test_empty_folder_returns_empty_list_not_none(store):
    store.begin_use()
    store.put_listing("empty_uid", [], path="/empty", fetched_at=1000.0)
    result = store.get_listing("empty_uid")
    assert result is not None
    assert result == ([], 1000.0)


def test_put_listing_keeps_existing_path_when_new_is_none(store):
    store.put_listing("f1", [Node("c1", "a", "file", 1)], path="/orig", fetched_at=1000.0)
    store.put_listing("f1", [Node("c2", "b", "file", 2)], path=None, fetched_at=1100.0)
    folders = store.folders()
    paths = {uid: p for uid, p, _ in folders}
    assert paths["f1"] == "/orig"


# ---------------------------------------------------------------------------
# Expiry
# ---------------------------------------------------------------------------


def test_expiry_get_listing_returns_none(store, now):
    store.begin_use()
    store.put_listing("f1", [Node("c1", "a", "file", 1)], path="/", fetched_at=1000.0)
    now[0] = 1000.0 + 1801.0  # past TTL
    assert store.get_listing("f1") is None


def test_begin_use_after_expiry_deletes_rows(store, now):
    store.begin_use()
    store.put_listing("f1", [Node("c1", "a", "file", 1)], path="/", fetched_at=1000.0)
    assert store.stats()["listings"] == 1
    now[0] = 1000.0 + 1801.0
    assert store.begin_use() is False
    assert store.stats()["listings"] == 0


# ---------------------------------------------------------------------------
# put_listing drops child folder -> deletes cached subtree
# ---------------------------------------------------------------------------


def test_put_listing_drops_child_folder_deletes_subtree(store):
    # Level 0: root_uid contains folder d1
    store.put_listing("root_uid", [Node("d1", "Sub", "folder")], path="/", fetched_at=1000.0)
    # Level 1: d1 contains folder d2
    store.put_listing("d1", [Node("d2", "Sub2", "folder")], path="/Sub", fetched_at=1000.0)
    # Level 2: d2 contains a file
    store.put_listing("d2", [Node("f2", "deep.txt", "file", 99)], path="/Sub/Sub2", fetched_at=1000.0)

    # Now replace root listing without d1
    store.put_listing("root_uid", [Node("x1", "other", "file", 1)], path="/", fetched_at=2000.0)

    assert store.get_listing("d1") is None
    assert store.get_listing("d2") is None


# ---------------------------------------------------------------------------
# add_child
# ---------------------------------------------------------------------------


def test_add_child_requires_cached_parent(store):
    store.add_child("missing_uid", Node("c1", "a", "file", 1))
    # Should be a no-op, no exception
    assert store.get_listing("missing_uid") is None


def test_add_child_replaces_same_name(store):
    store.begin_use()
    store.put_listing("p1", [Node("c1", "same.txt", "file", 1)], path="/", fetched_at=1000.0)
    store.add_child("p1", Node("c2", "same.txt", "file", 99))
    children, _ = store.get_listing("p1")
    assert len(children) == 1
    assert children[0].uid == "c2"
    assert children[0].size == 99


def test_add_child_inserts_new(store):
    store.begin_use()
    store.put_listing("p1", [Node("c1", "a", "file", 1)], path="/", fetched_at=1000.0)
    store.add_child("p1", Node("c2", "b", "file", 2))
    children, _ = store.get_listing("p1")
    assert len(children) == 2


# ---------------------------------------------------------------------------
# remove_child
# ---------------------------------------------------------------------------


def test_remove_child_removes_node(store):
    store.begin_use()
    store.put_listing("p1", [Node("c1", "a", "file", 1)], path="/", fetched_at=1000.0)
    store.remove_child("p1", "c1")
    children, _ = store.get_listing("p1")
    assert len(children) == 0


def test_remove_child_removes_subtree(store):
    store.put_listing("p1", [Node("d1", "Sub", "folder")], path="/", fetched_at=1000.0)
    store.put_listing("d1", [Node("c1", "file.txt", "file", 5)], path="/Sub", fetched_at=1000.0)
    store.remove_child("p1", "d1")
    assert store.get_listing("d1") is None


# ---------------------------------------------------------------------------
# set_root / get_root
# ---------------------------------------------------------------------------


def test_set_get_root_round_trip(store):
    root = Node("root_uid", "MyDrive", "folder")
    store.begin_use()
    store.set_root(root)
    got = store.get_root()
    assert got is not None
    assert got.uid == "root_uid"
    assert got.name == "MyDrive"


def test_get_root_none_when_cold(store):
    store.set_root(Node("r", "R", "folder"))
    # No begin_use called, cache is cold
    assert store.get_root() is None


# ---------------------------------------------------------------------------
# recent_activity
# ---------------------------------------------------------------------------


def test_recent_activity_newest_first(store, now):
    store.begin_use("/a")
    now[0] = 2000.0
    store.begin_use("/b")
    now[0] = 3000.0
    store.begin_use("/c")
    recent = store.recent_activity(limit=10)
    paths = [p for p, _ in recent]
    assert paths[0] == "/c"
    assert paths[1] == "/b"
    assert paths[2] == "/a"


def test_touch_updates_used_at(store, now):
    store.begin_use("/x")
    now[0] = 2500.0
    store.touch("/x")
    recent = store.recent_activity(limit=5)
    assert recent[0][0] == "/x"
    assert recent[0][1] == 2500.0


# ---------------------------------------------------------------------------
# flush
# ---------------------------------------------------------------------------


def test_flush_returns_count_and_empties(store):
    store.begin_use()
    store.put_listing("f1", [Node("c1", "a", "file", 1)], path="/", fetched_at=1000.0)
    store.put_listing("f2", [Node("c2", "b", "file", 2)], path="/b", fetched_at=1000.0)
    count = store.flush()
    assert count == 2
    assert store.stats()["listings"] == 0
    assert store.stats()["nodes"] == 0


def test_flush_other_shares_untouched(tmp_path, now):
    db = tmp_path / "shared" / "cache.sqlite"
    s1 = CacheStore(db, "https://x/urls/share1", ttl_s=1800, clock=lambda: now[0])
    s2 = CacheStore(db, "https://x/urls/share2", ttl_s=1800, clock=lambda: now[0])
    s1.begin_use()
    s2.begin_use()
    s1.put_listing("f1", [Node("c1", "a", "file", 1)], path="/", fetched_at=1000.0)
    s2.put_listing("f2", [Node("c2", "b", "file", 2)], path="/", fetched_at=1000.0)
    s1.flush()
    assert s2.stats()["listings"] == 1
    s1.close()
    s2.close()


# ---------------------------------------------------------------------------
# Two CacheStore objects on same file see each other's writes
# ---------------------------------------------------------------------------


def test_two_stores_shared_file(tmp_path, now):
    db = tmp_path / "shared" / "cache.sqlite"
    s1 = CacheStore(db, "https://x/urls/T#k", ttl_s=1800, clock=lambda: now[0])
    s2 = CacheStore(db, "https://x/urls/T#k", ttl_s=1800, clock=lambda: now[0])
    s1.begin_use()
    s1.put_listing("f1", [Node("c1", "a", "file", 1)], path="/", fetched_at=1000.0)
    result = s2.get_listing("f1")
    assert result is not None
    children, _ = result
    assert children[0].name == "a"
    s1.close()
    s2.close()


# ---------------------------------------------------------------------------
# File and directory permissions, URL not in DB bytes
# ---------------------------------------------------------------------------


def test_permissions(tmp_path):
    db_dir = tmp_path / "secure" / "cache.sqlite"
    store = CacheStore(
        db_dir,
        "https://x/urls/SUPERSECRET#key",
        ttl_s=1800,
        clock=lambda: 1000.0,
    )
    store.begin_use()
    store.close()

    # Directory permissions 0o700
    dir_path = db_dir.parent
    dir_mode = stat.S_IMODE(os.stat(dir_path).st_mode)
    assert dir_mode == 0o700

    # File permissions 0o600
    file_mode = stat.S_IMODE(os.stat(db_dir).st_mode)
    assert file_mode == 0o600


def test_url_not_in_database_bytes(tmp_path):
    db_path = tmp_path / "p" / "cache.sqlite"
    secret_url = "https://x/urls/T#k"
    store = CacheStore(
        db_path,
        secret_url,
        ttl_s=1800,
        clock=lambda: 1000.0,
    )
    store.begin_use()
    store.put_listing("f1", [Node("c1", "a", "file", 1)], path="/", fetched_at=1000.0)
    store.set_root(Node("r", "R", "folder"))
    store.close()

    raw = db_path.read_bytes()
    # Also check WAL if present
    wal_path = db_path.with_suffix(".sqlite-wal")
    shm_path = db_path.with_suffix(".sqlite-shm")
    if wal_path.exists():
        raw += wal_path.read_bytes()
    if shm_path.exists():
        raw += shm_path.read_bytes()

    assert secret_url.encode() not in raw


# ---------------------------------------------------------------------------
# Concurrent put_listing from 8 threads
# ---------------------------------------------------------------------------


def test_concurrent_put_listing(tmp_path):
    db = tmp_path / "conc" / "cache.sqlite"
    store = CacheStore(
        db,
        "https://x/urls/T#k",
        ttl_s=1800,
        clock=lambda: 1000.0,
    )
    store.begin_use()

    def do_put(i):
        kids = [Node(f"uid_{i}_{j}", f"file_{j}.txt", "file", j) for j in range(10)]
        store.put_listing(f"folder_{i}", kids, path=f"/f{i}", fetched_at=1000.0)

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(do_put, i) for i in range(8)]
        for f in concurrent.futures.as_completed(futures):
            f.result()  # re-raise if exception

    # All 8 listings should be present
    assert store.stats()["listings"] == 8
    store.close()


# ---------------------------------------------------------------------------
# share_key helper
# ---------------------------------------------------------------------------


def test_share_key_is_sha256_hex():
    import hashlib
    key = share_key("https://x/urls/T#k")
    expected = hashlib.sha256(b"https://x/urls/T#k").hexdigest()
    assert key == expected


# ---------------------------------------------------------------------------
# is_warm
# ---------------------------------------------------------------------------


def test_is_warm_false_when_cold(store):
    assert store.is_warm() is False


def test_is_warm_true_after_begin_use(store):
    store.begin_use()
    assert store.is_warm() is True


def test_forget_listing_drops_subtree_but_keeps_entry_in_parent(store):
    store.begin_use()
    store.put_listing("root", [Node("p1", "Movies", "folder")], path="/")
    store.put_listing("p1", [Node("c1", "Sub", "folder"), Node("f1", "a.mkv", "file", 3)], path="/Movies")
    store.put_listing("c1", [Node("f2", "b.mkv", "file", 4)], path="/Movies/Sub")
    store.forget_listing("p1")
    assert store.get_listing("p1") is None
    assert store.get_listing("c1") is None
    kids, _ = store.get_listing("root")
    assert [k.uid for k in kids] == ["p1"]
    assert {f[0] for f in store.folders()} == {"root"}
