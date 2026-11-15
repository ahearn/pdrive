import pytest

from pdrive import remote
from pdrive.drive import Drive, NotADirectory, NotFound
from pdrive.remote import PdriveError


@pytest.mark.parametrize("raw,expected", [
    ("", "/"), ("/", "/"), ("Movies", "/Movies"), ("/Movies/", "/Movies"),
    ("Movies//A/./", "/Movies/A"), ("Movies/A/..", "/Movies"), ("//x", "/x"), ("../..", "/"),
])
def test_normpath(raw, expected):
    assert remote.normpath(raw) == expected


def test_split_join_parts():
    assert remote.split("/Movies/A") == ("/Movies", "A")
    assert remote.split("Movies") == ("/", "Movies")
    assert remote.split("/") == ("/", "")
    assert remote.join("/Movies", "A") == "/Movies/A"
    assert remote.parts("/a/b/") == ["a", "b"]


def test_lookup_and_stat(drive):
    assert drive.lookup("/").uid == "root"
    assert drive.lookup("Movies/A/Alien (1979).mkv").size == 100
    assert drive.lookup("Movies/nope") is None
    with pytest.raises(NotFound):
        drive.stat("/nope")
    with pytest.raises(NotADirectory):
        drive.lookup("readme.txt/inside")


def test_listing_is_cached(drive, backend):
    drive.listdir(drive.root)
    drive.listdir(drive.root)
    assert [c for c in backend.calls if c[0] == "children"] == [("children", "root")]


def test_listdir_sorted(drive):
    assert [n.name for n in drive.listdir(drive.root)] == ["Empty", "Movies", "Music", "readme.txt"]


def test_mkdir(drive, backend):
    drive.mkdir("/New")
    assert backend.find("New").type == "folder"
    with pytest.raises(PdriveError, match="File exists"):
        drive.mkdir("/New")
    with pytest.raises(NotFound):
        drive.mkdir("/x/y")


def test_mkdir_parents(drive, backend):
    drive.mkdir("/x/y/z", parents=True)
    assert backend.find("x/y/z").type == "folder"
    drive.mkdir("/x/y", parents=True)  # existing: fine
    assert drive.lookup("/x/y/z") is not None  # cache saw the new folders
    with pytest.raises(PdriveError, match="File exists"):
        drive.mkdir("/readme.txt", parents=True)


def test_rename(drive, backend):
    drive.rename("/readme.txt", "README.md")
    assert backend.find("README.md") is not None
    assert drive.lookup("/README.md") is not None and drive.lookup("/readme.txt") is None
    with pytest.raises(PdriveError, match="File exists"):
        drive.rename("/README.md", "Movies")
    with pytest.raises(PdriveError, match="invalid name"):
        drive.rename("/README.md", "Movies/x")
    with pytest.raises(PdriveError, match="root"):
        drive.rename("/", "x")


def test_delete_updates_cache(drive, backend):
    node = drive.stat("/Movies/A")
    assert drive.delete([("/Movies/A", node)]) == [("/Movies/A", None)]
    assert backend.find("Movies/A") is None
    assert drive.lookup("/Movies/A") is None


def test_walk_and_size(drive):
    paths = [p for p, _ in drive.walk(drive.stat("/Movies"), "/Movies")]
    assert paths == ["/Movies/A", "/Movies/A/Alien (1979).mkv", "/Movies/B", "/Movies/B/Blade Runner.mkv"]
    assert drive.tree_size(drive.stat("/Movies")) == 350
    assert drive.tree_size(drive.root) == 365


def test_prefetch_lists_folders_concurrently(backend):
    import threading
    import time
    for i in range(6):
        backend.add(f"Wide/d{i}/f.txt", b"x")
    active, peak, lock = [0], [0], threading.Lock()
    real = backend.children

    def slow_children(uid):
        with lock:
            active[0] += 1
            peak[0] = max(peak[0], active[0])
        time.sleep(0.05)
        with lock:
            active[0] -= 1
        return real(uid)

    backend.children = slow_children
    d = Drive(backend, backend.auth("u", "s3cret"), list_jobs=8)
    files = [p for p, n in d.walk(d.stat("/Wide"), "/Wide") if not n.is_dir]
    assert len(files) == 6
    assert peak[0] >= 4  # the six d* folders were listed side by side


def test_lookup_many_warms_cache(drive, backend):
    drive.lookup_many(["/Movies/A/x.mkv", "/Music/y.mp3"])
    before = len(backend.calls)
    assert drive.lookup("/Movies/A/Alien (1979).mkv") is not None
    assert len(backend.calls) == before


def test_listdir_refetches_when_folder_changes_mid_listing(backend):
    from pdrive.drive import Drive

    root = backend.auth("u", "s3cret")
    drive = Drive(backend, root)

    movies_node = backend.find("Movies").node()

    original_children = backend.children
    target_uid = movies_node.uid
    call_count = [0]

    def patched_children(uid):
        result = original_children(uid)
        if uid == target_uid:
            call_count[0] += 1
            if call_count[0] == 1:
                # Simulate another worker finishing an upload while listing is in-flight:
                new_entry = backend.add("Movies/race_file.txt", b"late")
                drive.record_upload(movies_node, new_entry.node())
        return result

    backend.children = patched_children

    result = drive.listdir(movies_node)

    names = [n.name for n in result]
    assert "race_file.txt" in names
    children_calls_for_folder = [c for c in backend.calls if c[0] == "children" and c[1] == target_uid]
    assert len(children_calls_for_folder) == 2
