"""Regression tests for findings from the 2026-11-14 code reviews."""
import io
import os
import stat

import pytest

from conftest import PASSWORD, URL
from pdrive import remote
from pdrive.cli import main
from pdrive.config import ConfigFile, FileCredentialStore
from pdrive.drive import Drive
from pdrive.remote import RemoteError
from test_cli import Run


def test_paths_keep_leading_and_trailing_spaces(backend):
    assert remote.normpath("/notes ") == "/notes "
    assert remote.normpath(" a/b") == "/ a/b"
    backend.add("notes ", b"x")
    d = Drive(backend, backend.auth(URL, PASSWORD))
    assert d.lookup("/notes ") is not None and d.lookup("/notes") is None


def test_config_and_credentials_are_private(tmp_path):
    cfg_dir = tmp_path / "cfg"
    ConfigFile(cfg_dir).set("share_url", "https://x/urls/T#key")
    FileCredentialStore(cfg_dir).set("https://x/urls/T#key", "pw")
    assert stat.S_IMODE(os.stat(cfg_dir).st_mode) == 0o700
    for name in ("config", "credentials"):
        assert stat.S_IMODE(os.stat(cfg_dir / name).st_mode) == 0o600
    assert sorted(p.name for p in cfg_dir.iterdir()) == ["config", "credentials"]  # no temp leftovers


def test_update_and_force_are_exclusive(backend, tmp_path, capsys):
    with pytest.raises(SystemExit) as e:
        main(["put", "-u", "-f", "a", "/"])
    assert e.value.code == 2


def test_du_negative_depth_is_usage_error(backend, tmp_path):
    r = Run(backend, tmp_path)
    r("login", URL, "--password-stdin", stdin=PASSWORD + "\n")
    r("du", "-d", "-1", "/")
    assert r.code == 2 and "max-depth" in r.err.getvalue()


class BrokenKeyring:
    def get_password(self, *a):
        return None

    def set_password(self, *a):
        raise RuntimeError("keyring is locked")

    def delete_password(self, *a):
        pass


def test_login_falls_back_to_file_when_keyring_fails(backend, tmp_path):
    out, err = io.StringIO(), io.StringIO()
    code = main(["login", URL, "--password-stdin"], env={"PDRIVE_CONFIG_DIR": str(tmp_path / "cfg")},
                stdin=io.StringIO(PASSWORD + "\n"), stdout=out, stderr=err,
                backend_factory=lambda s: backend, keyring_loader=lambda: BrokenKeyring())
    assert code == 0
    assert "keyring failed" in err.getvalue()
    assert FileCredentialStore(tmp_path / "cfg").get(URL) == PASSWORD


def test_mkdir_parents_tolerates_folder_created_elsewhere(backend):
    d = Drive(backend, backend.auth(URL, PASSWORD))
    d.listdir(d.root)  # cache the root listing without "Late"
    real_mkdir = backend.mkdir

    def racing_mkdir(parent_uid, name):
        real_mkdir(parent_uid, name)  # another process wins the race...
        raise RemoteError("exists", "already exists")  # ...so ours fails

    backend.mkdir = racing_mkdir
    node = d.mkdir("/Late", parents=True)
    assert node.is_dir and node.uid == backend.find("Late").uid
    with pytest.raises(RemoteError):
        d.mkdir("/Late2", parents=False)


def test_cache_warming_failure_does_not_abort(backend, drive):
    def boom(uid):
        raise RemoteError("network", "fetch failed")
    real = backend.children
    backend.children = boom
    drive.lookup_many(["/Movies/A/x"])  # must not raise
    backend.children = real
    assert drive.lookup("/Movies/A/Alien (1979).mkv") is not None
