from __future__ import annotations

import asyncio
import threading
import time
from typing import Any

import pytest

from pdrive.cache import CacheStore
from pdrive.drive import Drive
from pdrive.mcp_server import ServerState, Tools, build_server, port_free, setup_text
from pdrive.remote import PdriveError
from pdrive.session import LazyBackend

from conftest import PASSWORD, URL


def children_count(backend: Any) -> int:
    return sum(1 for call in backend.calls if call[0] == "children")


class _MakeState:
    def __init__(self, tmp_path) -> None:
        self.tmp_path = tmp_path

    def __call__(self, backend: Any, now: float | None = None, db_name: str = "c.sqlite"):
        now = [1000.0] if now is None else [float(now)]
        store = CacheStore(self.tmp_path / db_name, URL, ttl_s=1800, clock=lambda: now[0])
        lazy = LazyBackend(lambda: backend, URL, lambda: PASSWORD)
        state = ServerState(store, lazy, list_jobs=4, refresh_interval=60, clock=lambda: now[0])
        tools = Tools(state)
        return state, lazy, tools, now


@pytest.fixture
def make_state(tmp_path):
    made: list[ServerState] = []

    def factory(backend: Any, now: float | None = None, db_name: str = "c.sqlite"):
        state, lazy, tools, now_holder = _MakeState(tmp_path)(backend, now=now, db_name=db_name)
        made.append(state)
        return state, lazy, tools, now_holder

    yield factory

    for state in reversed(made):
        state.stop()


def test_ls_basic_root_subfolder_file_limit_and_recursive(backend, make_state):
    state, lazy, tools, now = make_state(backend)

    root = tools.ls("/")
    assert root["path"] == "/"
    assert root["truncated"] is False
    by_name = {entry["name"]: entry for entry in root["entries"]}
    assert {"Movies", "Music", "readme.txt", "Empty"} <= set(by_name)
    assert by_name["Movies"]["type"] == "folder"
    assert by_name["readme.txt"]["type"] == "file"
    assert by_name["readme.txt"]["size"] == len(b"hello")
    for entry in root["entries"]:
        assert set(entry) >= {"path", "name", "type", "size"}

    movies = tools.ls("/Movies")
    assert movies["path"] == "/Movies"
    assert movies["truncated"] is False
    names = {entry["name"] for entry in movies["entries"]}
    assert names == {"A", "B"}
    assert all(entry["type"] == "folder" for entry in movies["entries"])
    assert all(entry["path"].startswith("/Movies/") for entry in movies["entries"])

    one_file = tools.ls("/readme.txt")
    assert one_file["path"] == "/readme.txt"
    assert one_file["truncated"] is False
    assert len(one_file["entries"]) == 1
    entry = one_file["entries"][0]
    assert entry == {
        "path": "/readme.txt",
        "name": "readme.txt",
        "type": "file",
        "size": 5,
        "mtime": entry["mtime"],
    }

    limited = tools.ls("/", limit=2)
    assert limited["truncated"] is True
    assert len(limited["entries"]) == 2

    recursive = tools.ls("/Movies", recursive=True)
    assert recursive["truncated"] is False
    paths = {entry["path"] for entry in recursive["entries"]}
    assert "/Movies/A" in paths
    assert "/Movies/B" in paths
    assert "/Movies/A/Alien (1979).mkv" in paths
    assert "/Movies/B/Blade Runner.mkv" in paths
    assert lazy.logins == 1


def test_second_ls_same_folder_uses_sqlite_cache_without_children(backend, make_state):
    state, lazy, tools, now = make_state(backend)

    first = tools.ls("/Movies")
    before_children = children_count(backend)

    second = tools.ls("/Movies")

    assert [entry["name"] for entry in first["entries"]] == [entry["name"] for entry in second["entries"]]
    assert children_count(backend) == before_children
    assert lazy.logins == 1


def test_new_state_over_same_sqlite_is_warm_and_does_not_login(backend, make_state):
    state1, lazy1, tools1, now = make_state(backend, db_name="shared.sqlite")
    tools1.ls("/")
    state1.stop()

    backend.calls.clear()
    state2, lazy2, tools2, _ = make_state(backend, db_name="shared.sqlite")
    assert lazy2.logins == 0

    result = tools2.ls("/")

    assert lazy2.logins == 0
    assert backend.calls == []
    assert {entry["name"] for entry in result["entries"]} == {"Movies", "Music", "readme.txt", "Empty"}


def test_stat_returns_uid_size_and_missing_path_raises(backend, make_state):
    state, lazy, tools, now = make_state(backend)

    root = tools.stat("/")
    assert root["path"] == "/"
    assert root["type"] == "folder"
    assert isinstance(root["uid"], str)
    assert root["uid"]
    assert root["size"] is None
    assert "media_type" in root

    movie = tools.stat("/Movies/B/Blade Runner.mkv")
    assert movie["path"] == "/Movies/B/Blade Runner.mkv"
    assert movie["type"] == "file"
    assert movie["size"] == 250
    assert isinstance(movie["uid"], str)
    assert movie["uid"]

    with pytest.raises(PdriveError) as exc:
        tools.stat("/nope")
    assert "nope" in str(exc.value)


def test_du_totals_children_depth_and_negative_depth(backend, make_state):
    state, lazy, tools, now = make_state(backend)

    full = tools.du("/", max_depth=1)
    assert full["path"] == "/"
    assert full["bytes"] == 365
    rows = {row["path"]: row["bytes"] for row in full["children"]}
    assert rows["/Movies"] == 350
    assert rows["/Music"] == 10
    assert rows["/Empty"] == 0

    leaf = tools.du("/Movies/B/Blade Runner.mkv", max_depth=2)
    assert leaf["path"] == "/Movies/B/Blade Runner.mkv"
    assert leaf["bytes"] == 250
    assert leaf["children"] == []

    total_only = tools.du("/", max_depth=0)
    assert total_only["bytes"] == 365
    assert total_only["children"] == []

    with pytest.raises(PdriveError):
        tools.du("/", max_depth=-1)


def test_search_substring_glob_type_limit_and_bad_type(backend, make_state):
    state, lazy, tools, now = make_state(backend)

    substring = tools.search("alien")
    assert substring["truncated"] is False
    paths = {entry["path"] for entry in substring["matches"]}
    assert "/Movies/A/Alien (1979).mkv" in paths

    glob = tools.search("*.mkv")
    assert glob["truncated"] is False
    names = {entry["name"] for entry in glob["matches"]}
    assert {"Alien (1979).mkv", "Blade Runner.mkv"} <= names

    folders = tools.search("*", type="folder")
    assert folders["truncated"] is False
    names = {entry["name"] for entry in folders["matches"]}
    assert {"Movies", "A", "B", "Music", "Empty"} <= names
    assert all(entry["type"] == "folder" for entry in folders["matches"])

    limited = tools.search("*", limit=2)
    assert limited["truncated"] is True
    assert len(limited["matches"]) == 2

    with pytest.raises(PdriveError):
        tools.search("*", type="dir")


def test_mkdir_shows_in_cached_parent_listing_without_children_call(backend, make_state):
    state, lazy, tools, now = make_state(backend)

    before_parent_cache = tools.ls("/Movies")
    assert {entry["name"] for entry in before_parent_cache["entries"]} == {"A", "B"}
    before_children = children_count(backend)

    created = tools.mkdir("/Movies/Documentaries")
    assert created["path"] == "/Movies/Documentaries"
    assert created["name"] == "Documentaries"
    assert created["type"] == "folder"

    assert backend.find("/Movies/Documentaries") is not None

    after = tools.ls("/Movies")
    assert {entry["name"] for entry in after["entries"]} == {"A", "B", "Documentaries"}
    assert children_count(backend) == before_children


def test_get_downloads_and_dry_run_downloads_nothing(backend, make_state, tmp_path):
    state, lazy, tools, now = make_state(backend)

    dl = tmp_path / "dl"
    dl.mkdir()
    result = tools.get(["/readme.txt"], str(dl))
    assert result["ok"] is True
    assert result["transferred"] == 1
    assert result["bytes"] == 5
    assert (dl / "readme.txt").read_bytes() == b"hello"

    dry_dl = tmp_path / "dry-dl"
    dry_dl.mkdir()
    dry = tools.get(["/readme.txt"], str(dry_dl), dry_run=True)
    assert dry["ok"] is True
    assert dry["transferred"] == 1
    assert dry["bytes"] == 5
    assert not (dry_dl / "readme.txt").exists()


def test_put_uploads_local_file_then_ls_shows_it(backend, make_state, tmp_path):
    state, lazy, tools, now = make_state(backend)

    local = tmp_path / "upload.txt"
    local.write_bytes(b"hi")

    result = tools.put([str(local)], "/")
    assert result["ok"] is True
    assert result["transferred"] == 1
    assert result["bytes"] == 2

    root = tools.ls("/")
    assert {entry["name"] for entry in root["entries"]} >= {"upload.txt"}
    assert backend.find("/upload.txt") is not None


def test_flush_cache_forgets_listings_and_next_ls_calls_backend_again(backend, make_state):
    state, lazy, tools, now = make_state(backend)

    tools.ls("/Movies")
    before = children_count(backend)

    flushed = tools.flush_cache()
    assert flushed["flushed_listings"] > 0

    after_flush_children = children_count(backend)
    assert after_flush_children == before

    tools.ls("/Movies")
    assert children_count(backend) > after_flush_children


def test_cache_status_reports_warm_connection_and_refreshes(backend, make_state):
    state, lazy, tools, now = make_state(backend)

    status_before = tools.cache_status()
    assert "warm" in status_before
    assert "connected_to_proton" in status_before
    assert "background_refreshes" in status_before
    assert status_before["warm"] is False
    assert status_before["connected_to_proton"] is False
    assert status_before["background_refreshes"] == 0

    tools.ls("/")

    status = tools.cache_status()
    assert status["warm"] is True
    assert status["connected_to_proton"] is True
    assert status["background_refreshes"] == 0


def test_tick_cold_cache_detaches_and_closes_backend(make_state, backend):
    state, lazy, tools, now = make_state(backend)

    tools.ls("/")
    assert lazy.connected is True

    now[0] += 1801
    result = state.tick()

    assert result is None
    assert lazy.connected is False
    assert backend.closed is True


def test_tick_warm_refreshes_one_cached_folder_without_counting_use(make_state, backend):
    state, lazy, tools, now = make_state(backend)

    tools.ls("/Movies")
    now[0] = 1400.0

    before_children = children_count(backend)
    before_last_used = state.store.last_used()
    cached_uids = {uid for uid, _parent, _last in state.store.folders()}

    uid = state.tick()

    assert uid in cached_uids
    assert children_count(backend) == before_children + 1
    assert state.store.last_used() == before_last_used
    assert lazy.logins == 1


def test_tick_no_activity_recorded_returns_none_without_backend_calls(make_state, backend):
    state, lazy, tools, now = make_state(backend)

    result = state.tick()

    assert result is None
    assert backend.calls == []
    assert lazy.logins == 0
    assert state.active_calls == 0


def test_tick_does_nothing_while_tool_call_is_active(make_state, backend):
    state, lazy, tools, now = make_state(backend)

    before_children = children_count(backend)
    with state.drive(["/Movies"]):
        assert state.active_calls == 1
        assert lazy.connected is True

        now[0] = 1400.0
        warm_result = state.tick()
        assert warm_result is None
        assert children_count(backend) == before_children
        assert lazy.connected is True

        now[0] = 3000.0
        cold_result = state.tick()
        assert cold_result is None
        assert children_count(backend) == before_children
        assert lazy.connected is True

    assert state.active_calls == 0
    assert children_count(backend) == before_children


def test_tick_never_logs_in(make_state, backend):
    state, lazy, tools, now = make_state(backend)

    tools.ls("/")
    lazy.close()
    assert lazy.connected is False
    logins_before = lazy.logins
    auth_calls_before = sum(1 for call in backend.calls if call[0] == "auth")
    now[0] += 400  # "/" is now old enough to be refreshed, if the refresher were allowed to log in

    result = state.tick()

    assert result is None
    assert lazy.connected is False
    assert lazy.logins == logins_before
    assert sum(1 for call in backend.calls if call[0] == "auth") == auth_calls_before


def test_cold_tick_closes_via_detach_and_next_tool_logs_in_again(make_state, backend):
    state, lazy, tools, now = make_state(backend)

    tools.ls("/")
    assert lazy.logins == 1

    now[0] += 1801
    assert state.tick() is None
    assert lazy.connected is False
    assert lazy.logins == 1

    tools.ls("/")
    assert lazy.logins == 2
    assert lazy.connected is True


def test_tick_returns_promptly_while_tool_blocks_inside_backend(make_state, backend):
    class BlockingBackend:
        def __init__(self, real, block_uid):
            self.real = real
            self.block_uid = block_uid
            self.blocked_event = threading.Event()
            self.release_event = threading.Event()

        def __getattr__(self, name):
            return getattr(self.real, name)

        def children(self, uid):
            if uid == self.block_uid:
                self.blocked_event.set()
                self.release_event.wait()
            return self.real.children(uid)

    root = backend.entries["root"]
    movies_uid = next(e.uid for e in backend.entries.values() if e.parent == root.uid and e.name == "Movies")

    wrapper = BlockingBackend(backend, movies_uid)
    state, lazy, tools, now = make_state(wrapper)

    result_box: dict[str, Any] = {}
    errors: list[BaseException] = []

    def ls_thread():
        try:
            result_box["result"] = tools.ls("/Movies")
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    thread = threading.Thread(target=ls_thread, name="pdrive-test-blocking-ls")
    thread.start()

    assert wrapper.blocked_event.wait(2) is True
    deadline = time.monotonic() + 2.0
    while state.active_calls == 0 and time.monotonic() < deadline:
        time.sleep(0.01)

    assert state.active_calls == 1
    assert lazy.connected is True

    started = time.perf_counter()
    tick_result = state.tick()
    elapsed = time.perf_counter() - started

    assert tick_result is None
    assert elapsed < 1.0
    assert lazy.connected is True

    wrapper.release_event.set()
    thread.join(5)
    assert not errors
    assert thread.is_alive() is False
    assert "result" in result_box
    assert state.active_calls == 0


def test_lazy_detach_returns_live_backend_and_is_none_when_disconnected(backend):
    lazy = LazyBackend(lambda: backend, URL, lambda: PASSWORD)
    lazy.connect()

    real = lazy.detach()

    assert real is backend
    assert lazy.connected is False
    assert lazy.logins == 1
    assert backend.closed is False

    assert lazy.detach() is None


def test_build_server_exposes_expected_tools_and_maps_pdrive_errors_to_tool_errors(make_state, backend):
    state, lazy, tools, now = make_state(backend)

    mcp = pytest.importorskip("mcp")
    server = build_server(tools)

    listed = asyncio.run(server.list_tools())
    listed = getattr(listed, "tools", listed)
    names = {tool.name for tool in listed}
    by_name = {tool.name: tool for tool in listed}
    # Annotations are strings (from __future__ import annotations); they must still become real schemas.
    assert by_name["search"].input_schema["properties"]["limit"]["type"] == "integer"
    assert by_name["get"].input_schema["properties"]["remote_paths"]["type"] == "array"
    assert set(by_name["get"].input_schema["required"]) == {"remote_paths", "local_dir"}
    assert names == {"ls", "stat", "du", "search", "get", "put", "mkdir", "flush_cache", "cache_status"}
    assert "rm" not in names
    assert "rename" not in names

    with pytest.raises(Exception) as exc:
        asyncio.run(server.call_tool("stat", {"path": "/nope"}))
    assert "nope" in str(exc.value)


def test_setup_text_contains_claude_command_and_mcp_json():
    text = setup_text(14912, "Share", 30)
    assert "claude mcp add --transport http pdrive http://127.0.0.1:14912/mcp" in text
    assert ".mcp.json" in text
    assert "Share" in text


def test_port_free_reports_busy_and_free_ports():
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen(1)
        port = sock.getsockname()[1]
        assert port_free(port) is False

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        free_port = probe.getsockname()[1]
    assert port_free(free_port) is True


def test_streamable_http_rejects_foreign_host_and_origin():
    """DNS rebinding guard: only loopback Host/Origin headers reach the tools."""
    from mcp.server.mcpserver import MCPServer
    from starlette.testclient import TestClient
    from pdrive.mcp_server import transport_security

    app = MCPServer(name="t").streamable_http_app(transport_security=transport_security())
    body = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}}
    headers = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
    with TestClient(app, base_url="http://127.0.0.1:14912") as c:
        assert c.post("/mcp", json=body, headers=headers).status_code == 200
        assert c.post("/mcp", json=body, headers={**headers, "Host": "evil.example:14912"}).status_code in (403, 421)
        assert c.post("/mcp", json=body, headers={**headers, "Origin": "http://evil.example"}).status_code in (403, 421)
