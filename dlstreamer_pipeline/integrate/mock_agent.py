#!/usr/bin/env python3
"""Stand-in for the Sentry V8 delivery agent, for testing the CV side only (no SQLite, no upload).

Listens on a Unix socket like the real agent (POST /v1/packages, GET /v1/status), checks every package the
way the contract's relationalValidation lists (parts match the manifest, evidence size and SHA-256, package
digest, parent/child ids, same package id with other bytes = 409), gives contiguous sequence numbers, saves
each package (manifest.json + evidence files) under OUT/packages/, and answers 201 durable / 200 duplicate /
4xx with the contract's error body. Schema checks of the saved packages are done afterwards by check_packages.py.

Usage: mock_agent.py SOCKET_PATH OUT_DIR
"""
import hashlib
import http.server
import json
import os
import socketserver
import sys
import threading
import time
from email.parser import BytesParser
from email.policy import HTTP

SOCKET_PATH, OUT = sys.argv[1], sys.argv[2]
os.makedirs(os.path.join(OUT, "packages"), exist_ok=True)
lock = threading.Lock()
state = {"next": 1, "packages": {}, "counts": {}, "rejected": 0, "started": time.time(), "last": None}
LOG = open(os.path.join(OUT, "agent.log"), "a", buffering=1)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode("utf-8")


def digest(manifest, parts):
    h = hashlib.sha256(canonical(manifest))
    for item in manifest["evidence"]:
        name = item["part_name"]
        h.update(b"\0" + name.encode("utf-8") + b"\0" + parts[name])
    return h.hexdigest()


def check(manifest, parts):
    """Returns an error text or None."""
    obs = manifest.get("observation") or {}
    oid = obs.get("observation_id")
    names = [e.get("part_name") for e in manifest.get("evidence", [])]
    if manifest.get("protocol_version") != "1.0":
        return "protocol_version"
    if len(set(names)) != len(names) or set(names) != set(parts):
        return "multipart parts do not equal manifest part names"
    eids = set()
    for item in manifest["evidence"]:
        meta, body = item.get("metadata") or {}, parts[item["part_name"]]
        if meta.get("observation_id") != oid:
            return "evidence belongs to another observation"
        if meta.get("size_bytes") != len(body) or meta.get("sha256") != "sha256:" + hashlib.sha256(body).hexdigest():
            return "evidence size or sha256 mismatch"
        eids.add(meta.get("evidence_id"))
    if set(obs.get("evidence_ids") or []) != eids:
        return "observation evidence_ids do not match package evidence"
    for emb in manifest.get("embeddings", []):
        if emb.get("observation_id") != oid or emb.get("evidence_id") not in eids:
            return "embedding parent or evidence reference wrong"
    if obs.get("deployment_id") != manifest.get("deployment_id"):
        return "deployment_id mismatch"
    return None


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def address_string(self):
        return "unix"

    def reply(self, code, payload):
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def error(self, code, name, retryable, message):
        state["rejected"] += 1
        LOG.write(f"{time.strftime('%T')} {code} {name}: {message}\n")
        self.reply(code, {"error": {"code": name, "retryable": retryable, "message": message[:256]}})

    def do_GET(self):
        if self.path != "/v1/status":
            return self.reply(404, {})
        with lock:
            self.reply(200, {"packages": len(state["packages"]), "next_sequence": state["next"], "event_types": state["counts"],
                             "rejected": state["rejected"], "last_durable_at": state["last"]})

    def do_POST(self):
        if self.path != "/v1/packages":
            return self.error(404, "not_found", False, self.path)
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0 or length > 64 * 1024 * 1024:
            return self.error(413, "request_too_large", False, str(length))
        raw = self.rfile.read(length)
        message = BytesParser(policy=HTTP).parsebytes(
            b"Content-Type: " + self.headers.get("Content-Type", "").encode() + b"\r\n\r\n" + raw)
        manifest, parts = None, {}
        for part in message.iter_parts():
            name = part.get_param("name", header="content-disposition")
            data = part.get_payload(decode=True) or b""
            if name == "manifest":
                manifest = json.loads(data)
            else:
                parts[name] = data
        if manifest is None:
            return self.error(400, "manifest_missing", False, "no manifest part")
        problem = check(manifest, parts)
        if problem:
            return self.error(422, "validation_failed", False, problem)
        pid, dig = manifest["package_id"], digest(manifest, parts)
        objects = ([("observation", manifest["observation"]["observation_id"])]
                   + [("evidence", e["metadata"]["evidence_id"]) for e in manifest["evidence"]]
                   + [("embedding", e["embedding_id"]) for e in manifest["embeddings"]])
        with lock:
            known = state["packages"].get(pid)
            if known is not None:
                if known["digest"] != dig:
                    return self.error(409, "package_id_conflict", False, pid)
                return self.reply(200, {"status": "duplicate", "package_id": pid, "deployment_id": manifest["deployment_id"],
                                        "sequences": known["sequences"]})
            sequences = [{"sequence": state["next"] + i, "object_type": t, "object_id": o} for i, (t, o) in enumerate(objects)]
            state["next"] += len(objects)
            every = int(os.getenv("MOCK_SAVE_EVERY") or 1)  # long runs: validate + count every package, save 1 in N to disk
            if os.getenv("MOCK_NO_SAVE") != "1" and state["next"] % every < len(objects):
                folder = os.path.join(OUT, "packages", f"{sequences[0]['sequence']:07d}_{manifest['observation']['event_type']}_{pid[:8]}")
                os.makedirs(folder)
                with open(os.path.join(folder, "manifest.json"), "w") as f:
                    json.dump(manifest, f)
                for name, body in parts.items():
                    with open(os.path.join(folder, name + ".jpg"), "wb") as f:
                        f.write(body)
            state["packages"][pid] = {"digest": dig, "sequences": sequences}
            kind = manifest["observation"]["event_type"]
            state["counts"][kind] = state["counts"].get(kind, 0) + 1
            state["last"] = time.time()
        self.reply(201, {"status": "durable", "package_id": pid, "deployment_id": manifest["deployment_id"], "sequences": sequences})


class Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True


if os.path.exists(SOCKET_PATH):
    os.remove(SOCKET_PATH)
if os.path.dirname(SOCKET_PATH):
    os.makedirs(os.path.dirname(SOCKET_PATH), exist_ok=True)
server = Server(SOCKET_PATH, Handler)
os.chmod(SOCKET_PATH, 0o660)


def summary():
    while True:
        time.sleep(30)
        with lock:
            LOG.write(f"{time.strftime('%T')} packages {len(state['packages'])} rejected {state['rejected']} {state['counts']}\n")


threading.Thread(target=summary, daemon=True).start()
print(f"mock delivery agent on {SOCKET_PATH}, saving to {OUT}", flush=True)
server.serve_forever()
