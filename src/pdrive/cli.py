"""pdrive: command-line access to a Proton Drive public share link.

Exit status: 0 success, 1 an operation failed, 2 usage error, 130 interrupted.
Data goes to stdout; diagnostics and progress go to stderr.
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterable, Iterator, Optional, TextIO

from . import __version__, remote
from .bridge import BridgeBackend, BridgePool, bridge_command, find_node
from .cache import CacheStore
from .cache import default_cache_path as cache_path
from .config import (KEYS, ConfigFile, FileCredentialStore, KeyringCredentialStore, Settings, _usable_keyring,
                     config_dir, parse_size)
from .drive import LIST_JOBS, Drive, NotFound
from .output import Reporter, StatusLine, human, long_line
from .remote import Backend, Node, PdriveError, UsageError
from .session import LazyBackend
from .transfer import Policy, plan_get, plan_put, run_get, run_put

BackendFactory = Callable[[Settings], Backend]


def default_backend_factory(settings: Settings) -> Backend:
    cmd = bridge_command(find_node(settings.get("node")), dns=settings.get("dns"))
    return BridgePool(lambda: BridgeBackend(cmd), max_bridges=settings.get("bridges"))


class App:
    """Shared state for one invocation. Streams and the backend factory are injectable for tests."""

    def __init__(self, args: argparse.Namespace, *, env=os.environ, stdin: TextIO = sys.stdin,
                 stdout: TextIO = sys.stdout, stderr: TextIO = sys.stderr,
                 backend_factory: BackendFactory = default_backend_factory,
                 prompt_password: Callable[[str], str] = getpass.getpass, keyring_loader=None):
        self.args = args
        self.env = env
        self.stdin, self.stdout, self.stderr = stdin, stdout, stderr
        self.backend_factory = backend_factory
        self.prompt_password = prompt_password
        self.cfg = ConfigFile(Path(args.config_dir) if args.config_dir else config_dir(env))
        self.settings = Settings(self.cfg, env, {"share_url": args.share_url, "cache_ttl": args.cache_ttl,
                                                 "mcp_port": getattr(args, "port", None)})
        self.keyring_loader = keyring_loader
        quiet = getattr(args, "quiet", False)
        self.status = StatusLine(stderr, enabled=stderr.isatty() and not quiet)

    def out(self, text: str) -> None:
        """Write to stdout, first clearing any transient status line on the terminal."""
        self.status.write_through(self.stdout, text)

    def _kr(self) -> dict:
        return {"keyring_loader": self.keyring_loader} if self.keyring_loader is not None else {}

    def err(self, msg: str) -> None:
        self.stderr.write(f"pdrive: {msg}\n")

    def info(self, msg: str) -> None:
        if not self.args.quiet:
            self.stderr.write(msg + "\n")

    def share_url(self) -> str:
        url = self.settings.get("share_url")
        if not url:
            raise PdriveError("no share configured; run: pdrive login <share-url>")
        return url

    def password(self, url: str) -> str:
        password = self.settings.password(url, **self._kr())
        if password is None:
            if not self.stdin.isatty():
                raise PdriveError("no saved password; run: pdrive login (or set PDRIVE_PASSWORD)")
            password = self.prompt_password("Share password: ")
        return password

    def open_store(self) -> Optional[CacheStore]:
        if self.args.no_cache:
            return None
        return CacheStore(cache_path(self.env), self.share_url(), ttl_s=60 * self.settings.get("cache_ttl"))

    @contextmanager
    def drive(self, paths: Iterable[str] = ()) -> Iterator[Drive]:
        """A Drive for one command. With a warm cache, nothing connects to Proton until a listing
        or transfer actually needs it. ``paths`` are recorded as recent activity, which steers the
        MCP server's background refresh."""
        url = self.share_url()
        store = self.open_store()
        backend = LazyBackend(lambda: self.backend_factory(self.settings), url, lambda: self.password(url))
        try:
            if store:
                store.begin_use()
                for p in paths:
                    store.touch(remote.normpath(p))
            root = store.get_root() if store else None
            if root is None:
                root = backend.connect()
                if store:
                    store.set_root(root)
            d = Drive(backend, root, list_jobs=list_jobs(self.settings), store=store)
            d.on_list = self.status.update
            try:
                yield d
            finally:
                d.stop()
        finally:
            self.status.clear()
            backend.close()
            if store:
                store.close()


def list_jobs(settings: Settings) -> int:
    # ~0.7 s per listing is mostly waiting on Proton, so keep 8 in flight per bridge
    # (measured: 8 bridges x 8 = 64 listed /Music/CDMP3 in 20 s vs 46 s with 1 x 16).
    return max(LIST_JOBS, 8 * settings.get("bridges"))


# ------------------------------------------------------------------------------------- commands
def cmd_mcp(app: App) -> int:
    from .mcp_server import ServerState, serve  # imports the MCP SDK only when serving
    url = app.share_url()
    ttl_min = app.settings.get("cache_ttl")
    password = app.password(url)
    store = CacheStore(cache_path(app.env), url, ttl_s=60 * ttl_min)
    backend = LazyBackend(lambda: app.backend_factory(app.settings), url, lambda: password)
    state = ServerState(store, backend, list_jobs(app.settings), app.settings.get("refresh_interval"))
    return serve(state, app.settings.get("mcp_port"), ttl_min, out=app.stderr)


def cmd_flush_cache(app: App) -> int:
    store = CacheStore(cache_path(app.env), app.share_url(), ttl_s=60 * app.settings.get("cache_ttl"))
    try:
        n = store.flush()
    finally:
        store.close()
    app.info(f"flushed {n} cached folder listing(s)")
    return 0


def cmd_login(app: App) -> int:
    a = app.args
    url = a.url or app.settings.get("share_url")
    if not url:
        if not app.stdin.isatty():
            raise UsageError("login: share URL required")
        app.stderr.write("Share URL: ")
        app.stderr.flush()
        url = app.stdin.readline().strip()
    if "#" not in url:
        app.err("warning: share URL has no #fragment; the link's key is usually after '#'")
    if a.password_stdin:
        password = app.stdin.readline().rstrip("\n")
    else:
        password = app.prompt_password("Share password (empty if none): ")
    backend = app.backend_factory(app.settings)
    try:
        root = backend.auth(url, password)
    finally:
        backend.close()
    app.cfg.set("share_url", url)
    if a.no_store:
        app.info(f"logged in to share {root.name!r}; URL saved, password not saved")
    else:
        store = app.settings.store(**app._kr())
        try:
            store.set(url, password)
        except Exception as e:  # e.g. the keyring is locked or its service isn't running
            if app.settings.get("password_store") == "keyring":
                raise PdriveError(f"could not save the password to the keyring: {e}") from None
            app.err(f"warning: keyring failed ({e}); saving the password to a private file instead")
            store = FileCredentialStore(app.cfg.dir)
            store.set(url, password)
        where = "keyring" if store.name == "keyring" else str(app.cfg.dir / "credentials")
        app.info(f"logged in to share {root.name!r}; password saved to {where}")
    return 0


def cmd_logout(app: App) -> int:
    url = app.settings.get("share_url")
    removed = False
    if url:
        kr = (app.keyring_loader or _usable_keyring)()
        stores = [FileCredentialStore(app.cfg.dir)] + ([KeyringCredentialStore(kr)] if kr else [])
        for s in stores:
            removed = s.delete(url) or removed
    if app.args.all:
        app.cfg.unset("share_url")
    app.info("password removed" if removed else "no saved password")
    return 0


def cmd_config(app: App) -> int:
    a = app.args
    out = app.stdout
    if a.action == "path":
        out.write(f"{app.cfg.path}\n")
    elif a.action in (None, "list"):
        for name, key in KEYS.items():
            val = app.settings.get(name)
            out.write(f"{name} = {'' if val is None else val}    # {app.settings.source(name)}; {key.help}\n")
    elif a.action == "get":
        _need(a.key, "config get KEY")
        if a.key not in KEYS:
            raise UsageError(f"unknown setting: {a.key}")
        val = app.settings.get(a.key)
        if val is None:
            return 1
        out.write(f"{val}\n")
    elif a.action == "set":
        _need(a.key and a.value is not None, "config set KEY VALUE")
        app.cfg.set(a.key, a.value)
    elif a.action == "unset":
        _need(a.key, "config unset KEY")
        app.cfg.unset(a.key)
    return 0


def _need(cond, usage: str) -> None:
    if not cond:
        raise UsageError(f"usage: pdrive {usage}")


def cmd_ls(app: App) -> int:
    a = app.args
    paths = a.paths or ["/"]
    status = 0
    with app.drive(paths) as d:
        listings: list[tuple[str, Node]] = []
        for p in paths:
            try:
                listings.append((remote.normpath(p), d.stat(p)))
            except PdriveError as e:
                app.err(f"ls: {e}")
                status = 1
        files = [(p, n) for p, n in listings if not n.is_dir or a.directory]
        dirs = [(p, n) for p, n in listings if n.is_dir and not a.directory]
        for p, n in files:
            _ls_entry(app, d, p, n, p)
        for i, (p, n) in enumerate(dirs):
            if a.recursive:
                if a.json:
                    for sub_path, sub in d.walk(n, p):
                        _ls_entry(app, d, sub_path, sub, sub_path)
                else:
                    d.start_prefetch(n)
                    _ls_tree(app, d, p, n, first=(i == 0 and not files))
            else:
                if len(listings) > 1 and not a.json:
                    app.out(("\n" if (i or files) else "") + f"{p}:\n")
                for kid in d.listdir(n):
                    _ls_entry(app, d, remote.join(p, kid.name), kid, kid.name)
    return status


def _ls_tree(app: App, d: Drive, path: str, node: Node, first: bool) -> None:
    app.out(("" if first else "\n") + f"{path}:\n")
    kids = d.listdir(node)
    for kid in kids:
        _ls_entry(app, d, remote.join(path, kid.name), kid, kid.name)
    for kid in kids:
        if kid.is_dir:
            _ls_tree(app, d, remote.join(path, kid.name), kid, first=False)


def _ls_entry(app: App, d: Drive, path: str, node: Node, label: str) -> None:
    a = app.args
    if a.json:
        rec = {"path": path, "name": node.name, "type": node.type, "size": node.size, "mtime": node.mtime,
               "media_type": node.media_type, "uid": node.uid}
        app.out(json.dumps(rec) + "\n")
        return
    if a.classify and node.is_dir:
        label += "/"
    if a.long:
        app.out(long_line(node, label, a.human_readable) + "\n")
    else:
        app.out(label + "\n")


def cmd_du(app: App) -> int:
    a = app.args
    status = 0
    fmt = human if a.human_readable else str
    if a.max_depth is not None and a.max_depth < 0:
        raise UsageError("du: --max-depth must be 0 or more")
    with app.drive(a.paths) as d:
        for p in a.paths or ["/"]:
            p = remote.normpath(p)
            try:
                node = d.stat(p)
            except PdriveError as e:
                app.err(f"du: {e}")
                status = 1
                continue
            if not node.is_dir or a.summarize:
                app.out(f"{fmt(d.tree_size(node))}\t{p}\n")
                continue
            d.start_prefetch(node)
            _du(d, node, p, 0, a.max_depth, lambda s, q: app.out(f"{fmt(s)}\t{q}\n"))
    return status


def _du(d: Drive, node: Node, path: str, depth: int, max_depth: Optional[int], emit) -> int:
    total = 0
    for kid in d.listdir(node):
        if kid.is_dir:
            total += _du(d, kid, remote.join(path, kid.name), depth + 1, max_depth, emit)
        else:
            total += kid.size or 0
    if max_depth is None or depth <= max_depth:
        emit(total, path)
    return total


def cmd_stat(app: App) -> int:
    status = 0
    with app.drive(app.args.paths) as d:
        for p in app.args.paths:
            try:
                n = d.stat(p)
            except PdriveError as e:
                app.err(f"stat: {e}")
                status = 1
                continue
            rec = {"path": remote.normpath(p), "name": n.name, "type": n.type, "size": n.size,
                   "mtime": n.mtime, "media_type": n.media_type, "uid": n.uid}
            if app.args.json:
                app.out(json.dumps(rec) + "\n")
            else:
                for k, v in rec.items():
                    app.out(f"{k + ':':<12}{'' if v is None else v}\n")
                if len(app.args.paths) > 1:
                    app.out("\n")
    return status


def cmd_mkdir(app: App) -> int:
    status = 0
    with app.drive(app.args.paths) as d:
        for p in app.args.paths:
            try:
                d.mkdir(p, parents=app.args.parents)
                if app.args.verbose:
                    app.stderr.write(f"created {remote.normpath(p)}/\n")
            except PdriveError as e:
                app.err(f"mkdir: {e}")
                status = 1
    return status


def cmd_rename(app: App) -> int:
    with app.drive([app.args.path]) as d:
        d.rename(app.args.path, app.args.new_name)
    return 0


def cmd_rm(app: App) -> int:
    a = app.args
    status = 0
    with app.drive(a.paths) as d:
        targets: list[tuple[str, Node]] = []
        for p in a.paths:
            path = remote.normpath(p)
            try:
                node = d.stat(path)
            except NotFound as e:
                if not a.force:
                    app.err(f"rm: {e}")
                    status = 1
                continue
            if node.uid == d.root.uid:
                app.err("rm: refusing to remove the share root")
                status = 1
                continue
            if node.is_dir and not a.recursive:
                app.err(f"rm: {path}: is a folder (use -r)")
                status = 1
                continue
            if a.interactive and not _confirm(app, f"remove {'folder' if node.is_dir else 'file'} {path}? "):
                continue
            targets.append((path, node))
        if a.dry_run:
            for path, node in targets:
                extra = f" ({human(d.tree_size(node))}B)" if node.is_dir else ""
                app.out(f"would remove {path}{'/' if node.is_dir else ''}{extra}\n")
            return status
        for path, err in d.delete(targets):
            if err:
                app.err(f"rm: {path}: {err}")
                status = 1
            elif a.verbose:
                app.stderr.write(f"removed {path}\n")
    return status


def _confirm(app: App, question: str) -> bool:
    app.stderr.write(question)
    app.stderr.flush()
    return app.stdin.readline().strip().lower() in ("y", "yes")


def _policy(a: argparse.Namespace) -> Policy:
    return Policy.FORCE if a.force else Policy.UPDATE if a.update else Policy.DEFAULT


def cmd_put(app: App) -> int:
    a = app.args
    jobs = a.jobs or app.settings.get("jobs")
    rate = parse_size(a.limit_rate) if a.limit_rate is not None else app.settings.get("limit_rate")
    with app.drive([a.dest]) as d:
        if rate and not a.dry_run:
            d.backend.set_rate_limit(rate)
        plan = plan_put(d, a.sources, a.dest, recursive=a.recursive, exclude=a.exclude)
        app.status.clear()
        d.on_list = None  # the transfer meter takes over the status line
        rep = Reporter("uploaded" if not a.dry_run else "would upload", len(plan.uploads),
                       sum(u.size for u in plan.uploads), verbose=a.verbose or a.dry_run, quiet=a.quiet,
                       stream=app.stderr)
        try:
            failed = run_put(d, plan, rep, policy=_policy(a), jobs=jobs, dry_run=a.dry_run, parents=a.parents)
        finally:
            rep.close()
    return 1 if failed else 0


def cmd_get(app: App) -> int:
    a = app.args
    jobs = a.jobs or app.settings.get("jobs")
    with app.drive(a.sources) as d:
        plan = plan_get(d, a.sources, a.dest, recursive=a.recursive, exclude=a.exclude)
        app.status.clear()
        d.on_list = None
        rep = Reporter("downloaded" if not a.dry_run else "would download", len(plan.downloads),
                       sum(x.node.size or 0 for x in plan.downloads), verbose=a.verbose or a.dry_run,
                       quiet=a.quiet, stream=app.stderr)
        try:
            failed = run_get(d, plan, rep, policy=_policy(a), jobs=jobs, dry_run=a.dry_run)
        finally:
            rep.close()
    return 1 if failed else 0


# ------------------------------------------------------------------------------------- parser
class _Parser(argparse.ArgumentParser):
    def error(self, message: str):  # usage errors -> exit 2 with a one-line hint
        self.print_usage(sys.stderr)
        self.exit(2, f"pdrive: {message}\n")


def _ttl(text: str) -> float:
    try:
        return KEYS["cache_ttl"].parse(text)
    except UsageError as e:
        raise argparse.ArgumentTypeError(str(e)) from None


def _port_arg(text: str) -> int:
    try:
        return KEYS["mcp_port"].parse(text)
    except UsageError as e:
        raise argparse.ArgumentTypeError(str(e)) from None


def build_parser() -> argparse.ArgumentParser:
    def flags(default):  # -v/-q work before or after the command; SUPPRESS keeps the global value
        c = argparse.ArgumentParser(add_help=False)
        c.add_argument("-v", "--verbose", action="store_true", default=default, help="report each file/operation on stderr")
        c.add_argument("-q", "--quiet", action="store_true", default=default, help="no progress or summary output")
        return c
    common = flags(False)
    sub_common = flags(argparse.SUPPRESS)

    p = _Parser(prog="pdrive", description="Command-line access to a Proton Drive public share link.",
                epilog="Remote paths are relative to the share root ('/' or empty). "
                       "Run 'pdrive COMMAND -h' for command help.", parents=[common])
    p.add_argument("--version", action="version", version=f"pdrive {__version__}")
    p.add_argument("--share-url", metavar="URL", help="share link to use (default: saved by login; env PDRIVE_SHARE_URL)")
    p.add_argument("--config-dir", metavar="DIR", help="config directory (default: ~/.config/pdrive; env PDRIVE_CONFIG_DIR)")
    p.add_argument("--cache-ttl", metavar="MIN", type=_ttl,
                   help="minutes cached listings stay warm after the last use (default: config 'cache_ttl', 30)")
    p.add_argument("--no-cache", action="store_true", help="don't read or write the listing cache")
    p.add_argument("-mcp", "--mcp", action="store_true",
                   help="run the MCP server (HTTP on 127.0.0.1) instead of a command")
    p.add_argument("--port", type=_port_arg, metavar="PORT", help="MCP server port (default: config 'mcp_port', 14912)")
    sub = p.add_subparsers(dest="command", metavar="COMMAND", parser_class=_Parser)

    def add(name: str, func, help: str, **kw) -> argparse.ArgumentParser:
        sp = sub.add_parser(name, help=help, description=help, parents=[sub_common], **kw)
        sp.set_defaults(func=func)
        return sp

    add("flush-cache", cmd_flush_cache, "forget all cached folder listings for the share")

    sp = add("login", cmd_login, "verify a share link and password and save them")
    sp.add_argument("url", nargs="?", help="share link, e.g. https://drive.proton.me/urls/XXXX#key")
    sp.add_argument("--password-stdin", action="store_true", help="read the password from the first line of stdin")
    sp.add_argument("--no-store", action="store_true", help="save the URL only, not the password")

    sp = add("logout", cmd_logout, "forget the saved password")
    sp.add_argument("--all", action="store_true", help="also forget the saved share URL")

    sp = add("config", cmd_config, "show or change saved settings")
    sp.add_argument("action", nargs="?", choices=["list", "get", "set", "unset", "path"])
    sp.add_argument("key", nargs="?")
    sp.add_argument("value", nargs="?")

    sp = add("ls", cmd_ls, "list folders", add_help=False)
    sp.add_argument("paths", nargs="*", metavar="PATH")
    sp.add_argument("-l", dest="long", action="store_true", help="long format: type, size, modified, name")
    sp.add_argument("-h", "--human-readable", action="store_true", help="sizes like 1.5G (with -l)")
    sp.add_argument("-R", "--recursive", action="store_true", help="list subfolders recursively")
    sp.add_argument("-d", "--directory", action="store_true", help="list folders themselves, not their contents")
    sp.add_argument("-F", "--classify", action="store_true", help="append / to folder names")
    sp.add_argument("--json", action="store_true", help="one JSON object per entry (with full path)")
    sp.add_argument("--help", action="help", help="show this help message and exit")

    sp = add("du", cmd_du, "show total size of folders", add_help=False)
    sp.add_argument("paths", nargs="*", metavar="PATH")
    sp.add_argument("-s", "--summarize", action="store_true", help="only a total for each argument")
    sp.add_argument("-d", "--max-depth", type=int, metavar="N", help="print totals only N levels deep")
    sp.add_argument("-h", "--human-readable", action="store_true", help="sizes like 1.5G")
    sp.add_argument("--help", action="help", help="show this help message and exit")

    sp = add("stat", cmd_stat, "show details of files or folders")
    sp.add_argument("paths", nargs="+", metavar="PATH")
    sp.add_argument("--json", action="store_true")

    sp = add("mkdir", cmd_mkdir, "create folders")
    sp.add_argument("paths", nargs="+", metavar="PATH")
    sp.add_argument("-p", "--parents", action="store_true", help="create parents as needed; no error if it exists")

    sp = add("rename", cmd_rename, "rename a file or folder in place (the SDK cannot move between folders)")
    sp.add_argument("path", metavar="PATH")
    sp.add_argument("new_name", metavar="NEW_NAME")

    sp = add("rm", cmd_rm, "permanently delete files or folders (no trash)")
    sp.add_argument("paths", nargs="+", metavar="PATH")
    sp.add_argument("-r", "-R", "--recursive", action="store_true", help="allow removing folders and their contents")
    sp.add_argument("-f", "--force", action="store_true", help="ignore paths that don't exist")
    sp.add_argument("-i", "--interactive", action="store_true", help="ask before each removal")
    sp.add_argument("-n", "--dry-run", action="store_true", help="show what would be removed")

    for name, func, help, verb in (("put", cmd_put, "upload local files/folders", "upload"),
                                   ("get", cmd_get, "download remote files/folders", "download")):
        sp = add(name, func, help)
        src, dst = ("LOCAL", "REMOTE") if name == "put" else ("REMOTE", "LOCAL")
        sp.add_argument("sources", nargs="+", metavar=src)
        sp.add_argument("dest", metavar=dst, help="destination; must be an existing folder when there are several sources")
        sp.add_argument("-r", "-R", "--recursive", action="store_true", help=f"{verb} folders recursively")
        policy = sp.add_mutually_exclusive_group()
        policy.add_argument("-u", "--update", action="store_true", help="replace existing files whose size differs")
        policy.add_argument("-f", "--force", action="store_true", help="replace existing files even if the size matches")
        sp.add_argument("-n", "--dry-run", action="store_true", help=f"show what would {verb}")
        sp.add_argument("-j", "--jobs", type=int, metavar="N", help="parallel transfers (default: config 'jobs', 4)")
        sp.add_argument("--exclude", action="append", default=[], metavar="GLOB",
                        help="skip files/folders whose name matches (repeatable)")
        if name == "put":
            sp.add_argument("-p", "--parents", action="store_true", help="create missing remote parent folders")
            sp.add_argument("--limit-rate", metavar="RATE", help="cap upload speed, e.g. 20M (bytes/s)")
    return p


def main(argv: Optional[list[str]] = None, **app_kw) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.mcp:
        if getattr(args, "func", None):
            parser.error("--mcp runs the server; don't give a command as well")
        args.func = cmd_mcp
    if not getattr(args, "func", None):
        parser.print_help(sys.stderr)
        return 2
    app = App(args, **app_kw)
    try:
        return args.func(app)
    except KeyboardInterrupt:
        app.stderr.write("\n")
        return 130
    except BrokenPipeError:  # e.g. pdrive ls | head: the reader has what it wanted
        try:
            devnull = os.open(os.devnull, os.O_WRONLY)
            os.dup2(devnull, sys.stdout.fileno())
            os.close(devnull)
        except (OSError, ValueError):
            pass
        return 0
    except PdriveError as e:
        app.err(str(e))
        return e.exit_code


def main_entry() -> None:
    sys.exit(main())
