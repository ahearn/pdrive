import io
import os
import stat
import time

import pytest

from pdrive.cli import main
from conftest import PASSWORD, URL


class Run:
    def __init__(self, backend, tmp_path):
        self.backend = backend
        self.cache_dir = tmp_path / "cache"
        self.env = {"PDRIVE_CONFIG_DIR": str(tmp_path / "cfg"), "PDRIVE_CACHE_DIR": str(self.cache_dir)}

    def __call__(self, *argv, stdin="", env=None):
        self.out, self.err = io.StringIO(), io.StringIO()
        self.code = main(
            list(argv),
            env={**self.env, **(env or {})},
            stdin=io.StringIO(stdin),
            stdout=self.out,
            stderr=self.err,
            backend_factory=lambda s: self.backend,
            prompt_password=lambda q: PASSWORD,
            keyring_loader=lambda: None,
        )
        return self

    @property
    def lines(self):
        return self.out.getvalue().splitlines()


@pytest.fixture
def run(backend, tmp_path):
    backend.add("/Movies/a.mkv", b"x" * 10)
    backend.add("/Movies/Sub/b.mkv", b"y" * 20)
    backend.add("/Music/c.mp3", b"z" * 5)
    r = Run(backend, tmp_path)
    r("login", URL, "--password-stdin", stdin=PASSWORD + "\n")
    assert r.code == 0, r.err.getvalue()
    return r


def _call(run, *argv, stdin="", env=None):
    try:
        return run(*argv, stdin=stdin, env=env)
    except SystemExit as exc:
        run.code = exc.code
        return run


def _auth_count(backend):
    return sum(1 for c in backend.calls if c[0] == "auth")


def _list_count(backend):
    return sum(1 for c in backend.calls if c[0] == "children")


def _list_count_for(backend, uid):
    return sum(1 for c in backend.calls if c[0] == "children" and c[1] == uid)


def test_first_ls_authenticates_then_second_ls_is_cache_only(run, backend):
    auth_before = _auth_count(backend)
    list_before = _list_count(backend)

    run("ls", "/Movies")
    assert run.code == 0, run.err.getvalue()
    assert _auth_count(backend) - auth_before == 1
    assert _list_count(backend) - list_before > 0
    first = run.lines

    auth_before = _auth_count(backend)
    list_before = _list_count(backend)
    run("ls", "/Movies")
    assert run.code == 0, run.err.getvalue()
    assert _auth_count(backend) - auth_before == 0
    assert _list_count(backend) - list_before == 0
    assert run.lines == first


def test_cache_file_exists_with_private_mode(run):
    run("ls", "/Movies")
    assert run.code == 0, run.err.getvalue()

    cache_file = run.cache_dir / "cache.sqlite"
    assert cache_file.exists()
    assert stat.S_IMODE(cache_file.stat().st_mode) == 0o600


def test_no_cache_always_hits_backend_and_does_not_create_cache_file(backend, tmp_path):
    r = Run(backend, tmp_path)
    r("login", URL, "--password-stdin", stdin=PASSWORD + "\n")
    assert r.code == 0, r.err.getvalue()

    auth_before = _auth_count(backend)
    list_before = _list_count(backend)
    r("--no-cache", "ls", "/Movies")
    assert r.code == 0, r.err.getvalue()
    assert _auth_count(backend) - auth_before == 1
    assert _list_count(backend) - list_before > 0
    assert not (run.cache_dir / "cache.sqlite").exists() if False else not (r.cache_dir / "cache.sqlite").exists()

    auth_before = _auth_count(backend)
    list_before = _list_count(backend)
    r("--no-cache", "ls", "/Movies")
    assert r.code == 0, r.err.getvalue()
    assert _auth_count(backend) - auth_before == 1
    assert _list_count(backend) - list_before > 0
    assert not (r.cache_dir / "cache.sqlite").exists()


def test_trust_until_expiry_then_flush(run, backend):
    run("ls", "/Movies")
    assert run.code == 0, run.err.getvalue()
    assert "a.mkv" in run.lines

    backend.add("/Movies/new.mkv", b"n")

    list_before = _list_count(backend)
    run("ls", "/Movies")
    assert run.code == 0, run.err.getvalue()
    assert "new.mkv" not in run.lines
    assert _list_count(backend) - list_before == 0

    run("flush-cache")
    assert run.code == 0, run.err.getvalue()
    assert "flushed" in run.err.getvalue().lower()

    list_before = _list_count(backend)
    run("ls", "/Movies")
    assert run.code == 0, run.err.getvalue()
    assert "new.mkv" in run.lines
    assert _list_count(backend) - list_before > 0


def test_flush_cache_when_nothing_cached(run):
    run("flush-cache")
    assert run.code == 0, run.err.getvalue()
    assert "flushed 0" in run.err.getvalue()


def test_cache_ttl_zero_is_rejected(run, capsys):
    _call(run, "--cache-ttl", "0", "ls", "/Movies")
    assert run.code == 2
    out, err = capsys.readouterr()
    assert "cache-ttl" in err or "pdrive:" in err


def test_cache_ttl_non_numeric_is_rejected(run, capsys):
    _call(run, "--cache-ttl", "abc", "ls", "/Movies")
    assert run.code == 2
    out, err = capsys.readouterr()
    assert "cache-ttl" in err or "pdrive:" in err


def test_cache_expiry(run, backend):
    env = {"PDRIVE_CACHE_TTL": "0.0001"}

    run("ls", "/Movies", env=env)
    assert run.code == 0, run.err.getvalue()

    backend.calls.clear()
    time.sleep(0.05)

    auth_before = _auth_count(backend)
    list_before = _list_count(backend)
    run("ls", "/Movies", env=env)
    assert run.code == 0, run.err.getvalue()
    assert _auth_count(backend) - auth_before == 1
    assert _list_count(backend) - list_before > 0


def test_mkdir_updates_cached_listing(run, backend):
    run("ls", "/Movies")
    assert run.code == 0, run.err.getvalue()
    assert "NewDir" not in run.lines

    uid = backend.find("/Movies").uid

    run("mkdir", "/Movies/NewDir")
    assert run.code == 0, run.err.getvalue()
    assert backend.find("/Movies/NewDir") is not None

    backend.calls.clear()
    run("ls", "/Movies")
    assert run.code == 0, run.err.getvalue()
    assert "NewDir" in run.lines
    assert _list_count_for(backend, uid) == 0


def test_put_updates_cached_listing(run, backend, tmp_path):
    run("ls", "/Movies")
    assert run.code == 0, run.err.getvalue()
    assert "up.txt" not in run.lines

    uid = backend.find("/Movies").uid
    local = tmp_path / "up.txt"
    local.write_bytes(b"hello")

    run("put", str(local), "/Movies")
    assert run.code == 0, run.err.getvalue()
    assert backend.find("/Movies/up.txt") is not None

    backend.calls.clear()
    run("ls", "/Movies")
    assert run.code == 0, run.err.getvalue()
    assert "up.txt" in run.lines
    assert _list_count_for(backend, uid) == 0


def test_du_summarize_is_cached(run, backend):
    run("du", "-s", "/Movies")
    assert run.code == 0, run.err.getvalue()
    first = run.lines
    assert first

    backend.calls.clear()
    run("du", "-s", "/Movies")
    assert run.code == 0, run.err.getvalue()
    assert run.lines == first
    assert _list_count(backend) == 0


def test_rm_updates_cached_listing(run, backend):
    run("ls", "/Movies")
    assert run.code == 0, run.err.getvalue()
    assert "a.mkv" in run.lines

    uid = backend.find("/Movies").uid

    run("rm", "/Movies/a.mkv")
    assert run.code == 0, run.err.getvalue()
    assert backend.find("/Movies/a.mkv") is None

    backend.calls.clear()
    run("ls", "/Movies")
    assert run.code == 0, run.err.getvalue()
    assert "a.mkv" not in run.lines
    assert _list_count_for(backend, uid) == 0


def test_mcp_with_command_is_usage_error(run, capsys):
    _call(run, "-mcp", "ls")
    assert run.code == 2
    out, err = capsys.readouterr()
    assert "pdrive:" in err


@pytest.mark.parametrize("port", ["0", "70000"])
def test_invalid_port_is_usage_error(run, capsys, port):
    _call(run, "--port", port, "ls", "/Movies")
    assert run.code == 2
    out, err = capsys.readouterr()
    assert "pdrive:" in err


def test_config_get_and_list_show_defaults(run):
    run("config", "get", "mcp_port")
    assert run.code == 0, run.err.getvalue()
    assert run.lines == ["14912"]

    run("config", "list")
    assert run.code == 0, run.err.getvalue()
    text = run.out.getvalue()
    assert "mcp_port = 14912" in text
    assert "cache_ttl = 30" in text
