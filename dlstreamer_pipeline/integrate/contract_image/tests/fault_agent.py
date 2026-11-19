#!/usr/bin/env python3
"""Delivery-agent stand-in with faults, for the CV-side handoff contract (delivery.cvRuntimeHandoff).

Same validation as sentry_cv's v8_capture_agent (its Store is used unchanged for accepted packages).
The mode is read from --mode-file on every request:
  ok     validate + accept (201 durable / 200 duplicate / 409 conflict, as the capture agent)
  429    {"code":"outbox_full","retryable":true}
  503    {"code":"durability_unavailable","retryable":true}
  hang   read the request, then send nothing for 40 s (client timeout is 30 s), then close
Every attempt is logged to --attempts (package_id, manifest/evidence hashes, mode, concurrency).
"""
import argparse
import hashlib
import json
import os
import socketserver
import sys
import threading
import time
from email.policy import default as email_policy
from email.parser import BytesParser
from pathlib import Path

sys.path.insert(0, str(Path.home() / "Documents/sentry_cv/scripts/isolated_validation"))
from v8_capture_agent import Store, canonical  # noqa: E402

LOCK = threading.Lock()
ACTIVE = [0]


class FaultHandler(socketserver.StreamRequestHandler):
    store: Store
    mode_file: Path
    attempts: Path
    save_faces = None
    observations = None

    def handle(self):
        with LOCK:
            ACTIVE[0] += 1
            active = ACTIVE[0]
        try:
            self._handle(active)
        finally:
            with LOCK:
                ACTIVE[0] -= 1

    def _handle(self, active):
        request = self.rfile.readline().decode("ascii", "replace").strip().split()
        headers = {}
        while True:
            line = self.rfile.readline()
            if line in {b"\r\n", b"\n", b""}:
                break
            k, v = line.decode("latin-1").split(":", 1)
            headers[k.lower()] = v.strip()
        if request[:2] != ["POST", "/v1/packages"]:
            self._send(404, {"error": {"code": "not_found", "message": "not found", "retryable": False}})
            return
        body = self.rfile.read(int(headers["content-length"]))
        received = time.monotonic()
        message = BytesParser(policy=email_policy).parsebytes(
            f"Content-Type: {headers['content-type']}\r\nMIME-Version: 1.0\r\n\r\n".encode() + body)
        parts = {}
        for part in message.iter_parts():
            parts[part.get_param("name", header="content-disposition")] = part.get_payload(decode=True)
        manifest_bytes = parts.pop("manifest")
        manifest = json.loads(manifest_bytes)
        try:
            mode = self.mode_file.read_text().strip()
        except OSError:
            mode = "ok"
        record = {"t": time.time(), "mono": received, "mode": mode, "active": active,
                  "package_id": manifest.get("package_id"),
                  "event_type": (manifest.get("observation") or {}).get("event_type"),
                  "camera_id": (manifest.get("observation") or {}).get("camera_id"),
                  "manifest_sha": hashlib.sha256(canonical(manifest)).hexdigest(),
                  "evidence_sha": hashlib.sha256(b"".join(n.encode() + b"\0" + parts[n] for n in sorted(parts))).hexdigest()}
        if mode == "429":
            status, payload = 429, {"error": {"code": "outbox_full", "message": "outbox full", "retryable": True}}
        elif mode == "503":
            status, payload = 503, {"error": {"code": "durability_unavailable", "message": "unavailable", "retryable": True}}
        elif mode == "hang":
            record["status"] = "hang"
            self._log(record)
            time.sleep(40)
            return
        else:
            try:
                status, payload = self.store.accept(manifest, parts, received_mono=received)
            except Exception as exc:  # noqa: BLE001
                record["invalid"] = type(exc).__name__
                status, payload = 400, {"error": {"code": "invalid_package", "message": "package validation failed",
                                                  "retryable": False}}
        record["status"] = status
        self._log(record)
        if self.observations and status == 201:
            with LOCK, self.observations.open("a") as f:
                f.write(json.dumps(manifest["observation"]) + "\n")
        if self.save_faces and status in (200, 201):
            evidence = {e["metadata"]["evidence_id"]: e for e in manifest.get("evidence") or []}
            for emb in manifest.get("embeddings") or []:
                if emb.get("kind") == "face" and emb.get("evidence_id") in evidence:
                    ev = evidence[emb["evidence_id"]]
                    stem = self.save_faces / emb["embedding_id"]
                    stem.with_suffix(".jpg").write_bytes(parts[ev["part_name"]])
                    stem.with_suffix(".json").write_text(json.dumps({"embedding": emb, "evidence": ev["metadata"]}))
        self._send(status, payload)

    def _log(self, record):
        with LOCK, self.attempts.open("a") as f:
            f.write(json.dumps(record) + "\n")

    def _send(self, status, payload):
        reason = {200: "OK", 201: "Created", 400: "Bad Request", 404: "Not Found", 409: "Conflict",
                  429: "Too Many Requests", 503: "Service Unavailable"}.get(status, "Error")
        body = canonical(payload)
        try:
            self.wfile.write(f"HTTP/1.1 {status} {reason}\r\nContent-Type: application/json\r\nContent-Length: {len(body)}\r\n"
                             f"Connection: close\r\n\r\n".encode() + body)
        except OSError:
            pass


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--socket", type=Path, required=True)
    p.add_argument("--schemas", type=Path, required=True)
    p.add_argument("--log", type=Path, required=True)
    p.add_argument("--status", type=Path, required=True)
    p.add_argument("--mode-file", type=Path, required=True)
    p.add_argument("--attempts", type=Path, required=True)
    p.add_argument("--save-faces", type=Path)
    p.add_argument("--observations", type=Path)
    a = p.parse_args()
    if a.save_faces:
        a.save_faces.mkdir(parents=True, exist_ok=True)
    FaultHandler.save_faces = a.save_faces
    FaultHandler.observations = a.observations
    a.socket.unlink(missing_ok=True)
    FaultHandler.store = Store(a.schemas, a.log, a.status, None, 0, 0)
    FaultHandler.mode_file, FaultHandler.attempts = a.mode_file, a.attempts
    socketserver.ThreadingUnixStreamServer.daemon_threads = True
    with socketserver.ThreadingUnixStreamServer(str(a.socket), FaultHandler) as server:
        os.chmod(a.socket, 0o666)
        server.serve_forever()


if __name__ == "__main__":
    main()
