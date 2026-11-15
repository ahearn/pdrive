import os
import sys
import threading
from pathlib import Path

import pytest

from pdrive.bridge import BridgeBackend, bridge_command
from pdrive.remote import PdriveError, RemoteError

FAKE = str(Path(__file__).with_name("fake_bridge.py"))


@pytest.fixture
def bb():
    b = BridgeBackend([sys.executable, FAKE], cwd=Path(__file__).parent)
    yield b
    b.close()


def test_auth_ok_and_error(bb):
    assert bb.auth("u", "pw").name == "Share"
    with pytest.raises(RemoteError) as e:
        bb.auth("u", "wrong")
    assert e.value.code == "auth"


def test_concurrent_calls_matched_by_id(bb):
    results = {}

    def go(uid):
        results[uid] = bb.children(uid)[0].uid

    threads = [threading.Thread(target=go, args=(u,)) for u in ("slow", "a", "b")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(5)
    assert results == {"slow": "slow-1", "a": "a-1", "b": "b-1"}


def test_progress_events(bb, tmp_path):
    seen = []
    assert bb.download("x", str(tmp_path / "f"), progress=seen.append) == 3
    assert seen == [1, 2, 3] and (tmp_path / "f").read_bytes() == b"abc"


def test_delete_results(bb):
    res = bb.delete(["ok1", "bad"])
    assert [(r.uid, r.ok) for r in res] == [("ok1", True), ("bad", False)]
    assert res[1].error.startswith("nope (a share link may only rename/delete items created in the same session")


def test_unknown_op_is_error(bb):
    with pytest.raises(RemoteError, match="unknown op"):
        bb.call("bogus")


def test_bridge_exit_fails_pending_and_later_calls(bb):
    with pytest.raises(RemoteError) as e:
        bb.call("die")
    assert e.value.code == "bridge"
    with pytest.raises(RemoteError):
        bb.children("a")


def test_bridge_command_requires_install(tmp_path):
    with pytest.raises(PdriveError, match="npm ci"):
        bridge_command("node", bridge_dir=tmp_path)
    (tmp_path / "node_modules/@protontech/drive-sdk").mkdir(parents=True)
    (tmp_path / "bridge.ts").write_text("")
    cmd = bridge_command("/n", bridge_dir=tmp_path, dns="cloudflare")
    assert cmd[:2] == ["/n", "--conditions=browser"] and "--require" in cmd and cmd[-1].endswith("bridge.ts")
    (tmp_path / "dist").mkdir()
    (tmp_path / "dist/bridge.mjs").write_text("")
    assert bridge_command("/n", bridge_dir=tmp_path)[-1].endswith("dist/bridge.mjs")
    os.utime(tmp_path / "dist/bridge.mjs", (0, 0))  # stale bundle -> back to tsx
    assert bridge_command("/n", bridge_dir=tmp_path)[-1].endswith("bridge.ts")
