"""Stand-in for bridge_js/bridge.ts used to test BridgeBackend's protocol handling.

Speaks the same JSON-lines protocol. Responses to "slow" requests are delayed so that a later request
answers first, which checks that responses are matched by id rather than by order.
"""
import json
import os
import sys
import threading
import time

lock = threading.Lock()


def send(obj):
    with lock:
        sys.stdout.write(json.dumps(obj) + "\n")
        sys.stdout.flush()


def handle(req):
    rid, op = req["id"], req["op"]
    if op == "auth":
        if req["password"] != "pw":
            return send({"id": rid, "ok": False, "error": {"code": "auth", "message": "authentication failed"}})
        return send({"id": rid, "ok": True, "result": {"root": {"uid": "r", "name": "Share", "type": "folder"}}})
    if op == "children":
        if req["uid"] == "slow":
            time.sleep(0.3)
        return send({"id": rid, "ok": True, "result": [
            {"uid": req["uid"] + "-1", "name": "f", "type": "file", "size": 3, "mtime": None}]})
    if op == "download":
        for b in (1, 2, 3):
            send({"id": rid, "event": "progress", "bytes": b})
        with open(req["path"], "wb") as f:
            f.write(b"abc")
        return send({"id": rid, "ok": True, "result": {"bytes": 3}})
    if op == "delete":
        return send({"id": rid, "ok": True, "result": [
            {"uid": u, "ok": u != "bad", **({"error": {"code": "not_owner", "message": "nope."}} if u == "bad" else {})}
            for u in req["uids"]]})
    if op == "die":
        sys.stdout.write("not json\n")
        sys.stdout.flush()
        os._exit(3)  # sys.exit in a thread would not end the process
    send({"id": rid, "ok": False, "error": {"code": "protocol", "message": f"unknown op: {op}"}})


for line in sys.stdin:
    threading.Thread(target=handle, args=(json.loads(line),)).start()
