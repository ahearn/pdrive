import io
import json
import os
import stat

import pytest

from conftest import PASSWORD, URL
from pdrive.cli import main


class TTY(io.StringIO):
    def isatty(self):
        return True


class Run:
    """Invoke main() in-process with a fake backend, isolated config dir and captured streams."""

    def __init__(self, backend, tmp_path):
        self.backend = backend
        self.cfg_dir = tmp_path / "cfg"
        self.env = {"PDRIVE_CONFIG_DIR": str(self.cfg_dir)}

    def __call__(self, *argv, stdin="", tty=False, prompt=lambda q: PASSWORD, env=None):
        self.out, self.err = io.StringIO(), io.StringIO()
        self.code = main(list(argv), env={**self.env, **(env or {})},
                         stdin=TTY(stdin) if tty else io.StringIO(stdin), stdout=self.out, stderr=self.err,
                         backend_factory=lambda s: self.backend, prompt_password=prompt,
                         keyring_loader=lambda: None)
        return self

    @property
    def lines(self):
        return self.out.getvalue().splitlines()


@pytest.fixture
def run(backend, tmp_path):
    r = Run(backend, tmp_path)
    r("login", URL, "--password-stdin", stdin=PASSWORD + "\n")
    assert r.code == 0, r.err.getvalue()
    return r


def test_login_saves_url_and_private_password(backend, tmp_path):
    r = Run(backend, tmp_path)("login", URL, "--password-stdin", stdin=PASSWORD + "\n")
    assert r.code == 0
    assert "logged in to share 'Share'" in r.err.getvalue()
    assert URL in (tmp_path / "cfg/config").read_text()
    creds = tmp_path / "cfg/credentials"
    assert PASSWORD in creds.read_text() and stat.S_IMODE(os.stat(creds).st_mode) == 0o600
    assert PASSWORD not in (tmp_path / "cfg/config").read_text()


def test_login_wrong_password_saves_nothing(backend, tmp_path):
    r = Run(backend, tmp_path)("login", URL, "--password-stdin", stdin="nope\n")
    assert r.code == 1 and "authentication failed" in r.err.getvalue()
    assert not (tmp_path / "cfg").exists()


def test_login_no_store(backend, tmp_path):
    r = Run(backend, tmp_path)
    r("login", URL, "--no-store")
    assert r.code == 0 and not (tmp_path / "cfg/credentials").exists()
    r("ls")  # no saved password, not a tty -> clear error
    assert r.code == 1 and "pdrive login" in r.err.getvalue()
    r("ls", tty=True)  # tty -> prompt
    assert r.code == 0
    r("ls", env={"PDRIVE_PASSWORD": PASSWORD})
    assert r.code == 0


def test_not_logged_in(backend, tmp_path):
    r = Run(backend, tmp_path)("ls")
    assert r.code == 1 and r.err.getvalue() == "pdrive: no share configured; run: pdrive login <share-url>\n"


def test_logout(run):
    run("logout")
    assert "password removed" in run.err.getvalue()
    run("ls")
    assert run.code == 1
    run("logout", "--all")
    run("ls")
    assert "no share configured" in run.err.getvalue()


def test_ls_variants(run):
    assert run("ls").lines == ["Empty", "Movies", "Music", "readme.txt"]
    assert run("ls", "-F", "/").lines == ["Empty/", "Movies/", "Music/", "readme.txt"]
    assert run("ls", "Movies/A").lines == ["Alien (1979).mkv"]
    assert run("ls", "readme.txt").lines == ["/readme.txt"]
    assert run("ls", "-d", "Movies").lines == ["/Movies"]
    long = run("ls", "-lh", "Movies/B").lines
    assert long[0].startswith("- ") and long[0].endswith(" Blade Runner.mkv") and " 250 " in long[0]
    assert run("ls", "Music", "Movies/A").lines == ["/Music:", "song.mp3", "", "/Movies/A:", "Alien (1979).mkv"]


def test_ls_recursive_and_json(run):
    assert run("ls", "-R", "Movies").lines == [
        "/Movies:", "A", "B", "", "/Movies/A:", "Alien (1979).mkv", "", "/Movies/B:", "Blade Runner.mkv"]
    recs = [json.loads(x) for x in run("ls", "-R", "--json", "Movies").lines]
    assert [r["path"] for r in recs if r["type"] == "file"] == ["/Movies/A/Alien (1979).mkv",
                                                               "/Movies/B/Blade Runner.mkv"]


def test_ls_missing_path_status(run):
    run("ls", "nope", "Music")
    assert run.code == 1
    assert run.err.getvalue() == "pdrive: ls: /nope: No such file or directory\n"
    assert run.lines == ["song.mp3"]


def test_du(run):
    assert run("du", "-s", "/").lines == ["365\t/"]
    assert run("du", "Movies").lines == ["100\t/Movies/A", "250\t/Movies/B", "350\t/Movies"]
    assert run("du", "-d", "0", "Movies").lines == ["350\t/Movies"]
    assert run("du", "-sh", "Movies", "readme.txt").lines == ["350\t/Movies", "5\t/readme.txt"]


def test_stat_json(run):
    rec = json.loads(run("stat", "--json", "readme.txt").lines[0])
    assert rec["size"] == 5 and rec["type"] == "file" and rec["path"] == "/readme.txt"


def test_mkdir_and_rename(run, backend):
    run("mkdir", "-p", "a/b/c")
    assert run.code == 0 and backend.find("a/b/c")
    run("mkdir", "a")
    assert run.code == 1 and "File exists" in run.err.getvalue()
    run("rename", "a/b", "bee")
    assert run.code == 0 and backend.find("a/bee/c")


def test_rm_rules(run, backend):
    run("rm", "Movies")
    assert run.code == 1 and "use -r" in run.err.getvalue() and backend.find("Movies")
    run("rm", "/")
    assert run.code == 1 and "share root" in run.err.getvalue()
    run("rm", "-rn", "Movies")
    assert run.lines == ["would remove /Movies/ (350B)"] and backend.find("Movies")
    run("rm", "nope")
    assert run.code == 1
    run("rm", "-f", "nope")
    assert run.code == 0
    run("rm", "-rv", "Movies", "readme.txt")
    assert run.code == 0 and not backend.find("Movies") and not backend.find("readme.txt")
    assert "removed /Movies" in run.err.getvalue()


def test_rm_interactive(run, backend):
    run("rm", "-i", "readme.txt", stdin="n\n")
    assert backend.find("readme.txt")
    run("rm", "-i", "readme.txt", stdin="y\n")
    assert not backend.find("readme.txt")


def test_put_get_roundtrip(run, backend, tree, tmp_path):
    run("put", "-r", "-q", "--limit-rate", "1M", str(tree), "/Up")
    assert run.code == 0 and run.err.getvalue() == ""
    assert backend.rate == 1 << 20
    assert backend.find("Up/sub/b.bin").data == b"b" * 20
    out = tmp_path / "down"
    out.mkdir()
    run("get", "-r", "/Up", str(out))
    assert run.code == 0 and (out / "Up/sub/b.bin").read_bytes() == b"b" * 20
    assert "downloaded 3 file(s)" in run.err.getvalue()


def test_put_conflict_exit_status(run, tree):
    run("put", str(tree / "a.txt"), "/readme.txt")  # 4 bytes vs 5 remote
    assert run.code == 1 and "use -u or -f" in run.err.getvalue()
    run("put", "-u", str(tree / "a.txt"), "/readme.txt")
    assert run.code == 0


def test_config_commands(run):
    run("config", "set", "jobs", "2")
    assert run("config", "get", "jobs").lines == ["2"]
    assert run("config", "get", "node").code == 1
    assert any(line.startswith("jobs = 2") for line in run("config").lines)
    run("config", "set", "jobs", "zero")
    assert run.code == 2
    run("config", "unset", "jobs")
    assert run("config", "get", "jobs").lines == ["4"]


def test_global_flags_after_command(run, tree):
    run("put", str(tree / "a.txt"), "/Music", "-v")
    assert "upload " in run.err.getvalue()
    run("-q", "put", str(tree / "sub" / "b.bin"), "/Music")
    assert run.err.getvalue() == ""


def test_usage_errors_exit_2(run, capsys):
    with pytest.raises(SystemExit) as e:
        main(["frobnicate"])
    assert e.value.code == 2
    assert main([]) == 2
