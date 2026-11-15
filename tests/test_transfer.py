import io
import os

import pytest

from pdrive.drive import NotADirectory, NotFound
from pdrive.output import Reporter
from pdrive.remote import PdriveError, RemoteError
from pdrive.transfer import (PARTIAL_SUFFIX, Action, Policy, decide, plan_get, plan_put, run_get, run_put,
                             run_with_retry)


def rep():
    return Reporter("x", 0, 0, stream=io.StringIO(), tty=False)


@pytest.mark.parametrize("exists,dst,policy,expected", [
    (False, None, Policy.DEFAULT, Action.TRANSFER),
    (True, 10, Policy.DEFAULT, Action.SKIP),
    (True, 11, Policy.DEFAULT, Action.CONFLICT),
    (True, 11, Policy.UPDATE, Action.REPLACE),
    (True, 10, Policy.UPDATE, Action.SKIP),
    (True, 10, Policy.FORCE, Action.REPLACE),
])
def test_decide(exists, dst, policy, expected):
    assert decide(10, dst, exists, policy) is expected


def test_retry_only_transient(no_sleep):
    calls = []

    def flaky():
        calls.append(1)
        if len(calls) < 3:
            raise RemoteError("network", "fetch failed")
        return "ok"

    assert run_with_retry(flaky, delays=(1, 1, 1), sleep=no_sleep) == "ok" and len(calls) == 3
    with pytest.raises(RemoteError):
        run_with_retry(lambda: (_ for _ in ()).throw(RemoteError("permission", "no")), sleep=no_sleep)
    with pytest.raises(RemoteError):
        run_with_retry(lambda: (_ for _ in ()).throw(RemoteError("network", "x")), delays=(1,), sleep=no_sleep)


# ------------------------------------------------------------------ put planning
def test_put_file_into_folder(drive, tree):
    plan = plan_put(drive, [str(tree / "a.txt")], "/Music", recursive=False)
    assert [u.remote_path for u in plan.uploads] == ["/Music/a.txt"]


def test_put_file_with_new_name(drive, tree):
    plan = plan_put(drive, [str(tree / "a.txt")], "/Music/renamed.txt", recursive=False)
    assert [u.remote_path for u in plan.uploads] == ["/Music/renamed.txt"]


def test_put_dir_requires_recursive(drive, tree):
    plan = plan_put(drive, [str(tree)], "/Music", recursive=False)
    assert plan.uploads == [] and "use -r" in plan.errors[0]


def test_put_dir_into_existing_folder(drive, tree):
    plan = plan_put(drive, [str(tree)], "/Music", recursive=True, exclude=[".DS_Store"])
    assert plan.folders == ["/Music/src", "/Music/src/sub"]
    assert [u.remote_path for u in plan.uploads] == ["/Music/src/a.txt", "/Music/src/sub/b.bin"]


def test_put_dir_trailing_slash_means_contents(drive, tree):
    plan = plan_put(drive, [str(tree) + "/"], "/Music", recursive=True, exclude=[".DS_Store"])
    assert [u.remote_path for u in plan.uploads] == ["/Music/a.txt", "/Music/sub/b.bin"]


def test_put_dir_rerun_is_idempotent_with_slash(drive, backend, tree, no_sleep):
    for _ in range(2):
        run_put(drive, plan_put(drive, [str(tree) + "/"], "/Up", recursive=True), rep(), sleep=no_sleep)
    assert backend.find("Up/src") is None and backend.find("Up/a.txt")


def test_get_trailing_slash_means_contents(drive, tmp_path, no_sleep):
    run_get(drive, plan_get(drive, ["/Movies/"], str(tmp_path), recursive=True), rep(), sleep=no_sleep)
    assert (tmp_path / "A/Alien (1979).mkv").exists() and not (tmp_path / "Movies").exists()


def test_put_dir_to_new_name(drive, tree):
    plan = plan_put(drive, [str(tree)], "/Backup", recursive=True)
    assert plan.folders[0] == "/Backup"
    assert "/Backup/sub/.DS_Store" in [u.remote_path for u in plan.uploads]


def test_put_multiple_sources_need_existing_folder(drive, tree):
    with pytest.raises(NotFound):
        plan_put(drive, [str(tree / "a.txt"), str(tree / "sub")], "/Nope", recursive=True)
    with pytest.raises(NotADirectory):
        plan_put(drive, [str(tree / "a.txt"), str(tree / "sub")], "/readme.txt", recursive=True)


def test_put_missing_source(drive, tmp_path):
    plan = plan_put(drive, [str(tmp_path / "ghost")], "/", recursive=False)
    assert "No such file" in plan.errors[0]


# ------------------------------------------------------------------ put execution
def test_run_put_uploads_and_creates_folders(drive, backend, tree, no_sleep):
    plan = plan_put(drive, [str(tree)], "/Music", recursive=True, exclude=[".DS_Store"])
    r = rep()
    assert run_put(drive, plan, r, jobs=2, sleep=no_sleep) == 0
    assert backend.find("Music/src/sub/b.bin").data == b"b" * 20
    assert r.done == 2 and r.done_bytes == 24
    # second run: everything is skipped
    r2 = rep()
    assert run_put(drive, plan_put(drive, [str(tree)], "/Music", recursive=True, exclude=[".DS_Store"]),
                   r2, sleep=no_sleep) == 0
    assert r2.skipped == 2 and r2.done == 0


def test_run_put_conflict_update_force(drive, backend, tree, no_sleep):
    backend.add("Music/a.txt", b"different-size")
    plan = lambda: plan_put(drive, [str(tree / "a.txt")], "/Music", recursive=False)
    r = rep()
    assert run_put(drive, plan(), r, sleep=no_sleep) == 1
    assert "use -u or -f" in r.stream.getvalue()
    assert run_put(drive, plan(), rep(), policy=Policy.UPDATE, sleep=no_sleep) == 0
    entry = backend.find("Music/a.txt")
    assert entry.data == b"aaaa" and entry.revisions == 2
    assert run_put(drive, plan(), rep(), policy=Policy.UPDATE, sleep=no_sleep) == 0
    assert entry.revisions == 2  # same size now: skipped
    assert run_put(drive, plan(), rep(), policy=Policy.FORCE, sleep=no_sleep) == 0
    assert entry.revisions == 3


def test_run_put_dry_run_changes_nothing(drive, backend, tree, no_sleep):
    before = dict(backend.entries)
    r = Reporter("x", 0, 0, stream=io.StringIO(), tty=False, verbose=True)
    run_put(drive, plan_put(drive, [str(tree)], "/New", recursive=True), r, dry_run=True, sleep=no_sleep)
    assert backend.entries.keys() == before.keys()
    out = r.stream.getvalue()
    assert "would create /New/" in out and "would upload" in out


def test_run_put_missing_parent_needs_p(drive, backend, tree, no_sleep):
    plan = lambda: plan_put(drive, [str(tree / "a.txt")], "/x/y/a.txt", recursive=False)
    assert run_put(drive, plan(), rep(), sleep=no_sleep) == 1
    assert run_put(drive, plan(), rep(), parents=True, sleep=no_sleep) == 0
    assert backend.find("x/y/a.txt").data == b"aaaa"


def test_run_put_retries_transient(drive, backend, tree, no_sleep):
    backend.failures["upload"] = [RemoteError("network", "fetch failed")]
    r = rep()
    assert run_put(drive, plan_put(drive, [str(tree / "a.txt")], "/", recursive=False), r, sleep=no_sleep) == 0
    assert "retrying" in r.stream.getvalue()


def test_run_put_into_folder_name_clash(drive, backend, tree, no_sleep):
    plan = plan_put(drive, [str(tree / "a.txt")], "/Music", recursive=False)
    plan.uploads[0].name = "Empty"
    plan.uploads[0].remote_dir = "/"
    r = rep()
    assert run_put(drive, plan, r, sleep=no_sleep) == 1 and "is a folder" in r.stream.getvalue()


# ------------------------------------------------------------------ get
def test_get_file_to_dir_and_name(drive, tmp_path, no_sleep):
    plan = plan_get(drive, ["/readme.txt"], str(tmp_path), recursive=False)
    assert run_get(drive, plan, rep(), sleep=no_sleep) == 0
    assert (tmp_path / "readme.txt").read_bytes() == b"hello"
    plan = plan_get(drive, ["/readme.txt"], str(tmp_path / "other.txt"), recursive=False)
    run_get(drive, plan, rep(), sleep=no_sleep)
    assert (tmp_path / "other.txt").read_bytes() == b"hello"


def test_get_folder_recursive(drive, tmp_path, no_sleep):
    assert plan_get(drive, ["/Movies"], str(tmp_path), recursive=False).errors
    plan = plan_get(drive, ["/Movies"], str(tmp_path), recursive=True, exclude=["B"])
    assert run_get(drive, plan, rep(), jobs=3, sleep=no_sleep) == 0
    assert (tmp_path / "Movies/A/Alien (1979).mkv").stat().st_size == 100
    assert not (tmp_path / "Movies/B").exists()


def test_get_folder_to_new_dir(drive, tmp_path, no_sleep):
    plan = plan_get(drive, ["/Movies"], str(tmp_path / "films"), recursive=True)
    run_get(drive, plan, rep(), sleep=no_sleep)
    assert (tmp_path / "films/B/Blade Runner.mkv").exists()


def test_get_skip_conflict_overwrite(drive, tmp_path, no_sleep):
    target = tmp_path / "readme.txt"
    target.write_bytes(b"HELLO")  # same size
    r = rep()
    run_get(drive, plan_get(drive, ["/readme.txt"], str(tmp_path), recursive=False), r, sleep=no_sleep)
    assert r.skipped == 1 and target.read_bytes() == b"HELLO"
    target.write_bytes(b"longer text")
    assert run_get(drive, plan_get(drive, ["/readme.txt"], str(tmp_path), recursive=False), rep(),
                   sleep=no_sleep) == 1
    run_get(drive, plan_get(drive, ["/readme.txt"], str(tmp_path), recursive=False), rep(),
            policy=Policy.UPDATE, sleep=no_sleep)
    assert target.read_bytes() == b"hello"


def test_get_sets_mtime_and_cleans_partial(drive, backend, tmp_path, no_sleep):
    run_get(drive, plan_get(drive, ["/readme.txt"], str(tmp_path), recursive=False), rep(), sleep=no_sleep)
    assert int(os.path.getmtime(tmp_path / "readme.txt")) == 1767323045  # 2026-01-02T03:04:05Z
    backend.failures["download"] = [RemoteError("permission", "denied")]
    r = rep()
    assert run_get(drive, plan_get(drive, ["/Music/song.mp3"], str(tmp_path), recursive=False), r,
                   sleep=no_sleep) == 1
    assert not any(p.name.endswith(PARTIAL_SUFFIX) for p in tmp_path.iterdir())


def test_get_multiple_sources_need_dir(drive, tmp_path):
    with pytest.raises(PdriveError, match="No such directory"):
        plan_get(drive, ["/readme.txt", "/Music"], str(tmp_path / "nope"), recursive=True)
