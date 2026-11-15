import io
import threading

from pdrive.drive import Drive
from pdrive.output import StatusLine


def test_concurrent_listdir_same_folder_lists_once(backend, drive):
    folder = backend.find("Movies").node()
    original_children = backend.children
    call_count = [0]
    all_started = threading.Event()
    n_threads = 5
    barrier = threading.Barrier(n_threads + 1, timeout=5)

    def wrapped_children(uid):
        if uid == folder.uid:
            call_count[0] += 1
            all_started.wait(timeout=5)
        return original_children(uid)

    backend.children = wrapped_children

    results = []
    lock = threading.Lock()

    def list_it():
        barrier.wait(timeout=5)
        kids = drive.listdir(folder)
        with lock:
            results.append([n.name for n in kids])

    threads = [threading.Thread(target=list_it) for _ in range(n_threads)]
    for t in threads:
        t.start()

    barrier.wait(timeout=5)
    all_started.set()

    for t in threads:
        t.join(timeout=10)
        assert not t.is_alive()

    assert call_count[0] == 1
    assert len(results) == n_threads
    assert all(r == results[0] for r in results)
    assert results[0] == sorted(["A", "B"])


def test_walk_streams_before_tree_is_listed(backend):
    for i in range(20):
        name = chr(ord("a") + i)
        backend.add(f"Big/{name}/f{i}", b"x")

    big_node = backend.find("Big").node()
    t_folder = backend.find("Big/t")

    original_children = backend.children
    block_event = threading.Event()

    timed_out = []

    def wrapped_children(uid):
        if uid == t_folder.uid and not block_event.wait(timeout=2):
            timed_out.append(uid)  # the walk waited for 't' before yielding anything
        return original_children(uid)

    backend.children = wrapped_children

    root = backend.auth("https://drive.proton.me/urls/TEST#key", "s3cret")
    drive = Drive(backend, root, list_jobs=4)

    try:
        walker = drive.walk(big_node, "/Big")

        first_path, first_node = next(walker)
        assert first_path == "/Big/a"

        block_event.set()

        all_items = [(first_path, first_node)]
        for path, node in walker:
            all_items.append((path, node))

        names_seen = set()
        for path, node in all_items:
            if not node.is_dir:
                names_seen.add(backend.entries[node.uid].name)

        expected_files = {f"f{i}" for i in range(20)}
        assert names_seen == expected_files
        assert len(all_items) == 40
        assert timed_out == [], "walk did not yield until the whole tree was listed"
    finally:
        drive.stop()


def test_status_line():
    # --- enabled=True with controllable clock ---
    buf = io.StringIO()
    t = [0.0]
    sl = StatusLine(buf, enabled=True, clock=lambda: t[0])

    # First update writes the line
    sl.update(5)
    assert buf.getvalue() == "\r\033[Klisting… 5 folders"

    # Second update within 0.2 s is throttled
    buf.truncate(0)
    buf.seek(0)
    t[0] = 0.1
    sl.update(6)
    assert buf.getvalue() == ""

    # Update after the 0.2 s window succeeds
    t[0] = 0.25
    sl.update(12)
    assert buf.getvalue() == "\r\033[Klisting… 12 folders"

    # clear() writes the escape sequence because something was shown
    buf.truncate(0)
    buf.seek(0)
    sl.clear()
    assert buf.getvalue() == "\r\033[K"

    # clear() again writes nothing (nothing was shown)
    buf.truncate(0)
    buf.seek(0)
    sl.clear()
    assert buf.getvalue() == ""

    # --- enabled=False: nothing is ever written ---
    buf2 = io.StringIO()
    sl2 = StatusLine(buf2, enabled=False, clock=lambda: 0.0)
    sl2.update(100)
    sl2.clear()
    assert buf2.getvalue() == ""
