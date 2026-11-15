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
