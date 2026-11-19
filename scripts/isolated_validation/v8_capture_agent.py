#!/usr/bin/env python3
"""Schema-validating API capture fixture; durable ACKs are simulated.

This validates /v1/packages bytes, hashes and associations. It is not the
production delivery agent and makes no disk-fsync/remote-upload guarantee.

Receipt monotonic time is taken after reading the request body (at accept entry
for direct callers). accepted_at/accepted_mono follow validation and lock wait.
Synchronous validation, hashing, JSONL/status writes and optional gallery writes
add fixture overhead before ACK. Timings below exclude the final status write
and socket ACK; they are not runtime or production storage latency.
Gallery limit is a global package cap, including existing package directories;
zero disables capture. Only evidence bytes and vector-free audit metadata persist.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from email.parser import BytesParser
from email.policy import default as email_policy
import hashlib
import json
import math
import os
from pathlib import Path
import socketserver
import sys
import tempfile
import threading
import time
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker


def canonical(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")


class Store:
    def __init__(
        self, schema_dir: Path, log_path: Path, status_path: Path,
        gallery_dir: Path | None = None, gallery_limit: int = 0,
        gallery_interval_seconds: float = 0,
    ) -> None:
        if gallery_limit < 0 or (gallery_limit and gallery_dir is None):
            raise ValueError("gallery-limit must be nonnegative and requires gallery-dir when enabled")
        if not math.isfinite(gallery_interval_seconds) or gallery_interval_seconds < 0:
            raise ValueError("gallery interval must be finite and nonnegative")
        package_schema = json.loads(
            (schema_dir / "delivery-agent-package.schema.json").read_text()
        )
        package_schema["properties"]["observation"] = {"type": "object"}
        package_schema["properties"]["evidence"]["items"]["properties"]["metadata"] = {
            "type": "object"
        }
        package_schema["properties"]["embeddings"]["items"] = {"type": "object"}
        self.validator = Draft202012Validator(
            package_schema,
            format_checker=FormatChecker(),
        )
        self.observation_validator = Draft202012Validator(
            json.loads((schema_dir / "observation.schema.json").read_text()),
            format_checker=FormatChecker(),
        )
        self.evidence_validator = Draft202012Validator(
            json.loads((schema_dir / "evidence.schema.json").read_text()),
            format_checker=FormatChecker(),
        )
        self.embedding_validator = Draft202012Validator(
            json.loads((schema_dir / "embedding.schema.json").read_text()),
            format_checker=FormatChecker(),
        )
        self.log_path = log_path
        self.status_path = status_path
        self.lock = threading.Lock()
        self.packages: dict[str, tuple[str, list[dict[str, Any]]]] = {}
        self.next_sequence = 1
        self.counts: dict[str, int] = {}
        self.cameras: dict[str, int] = {}
        self.evidence_bytes = 0
        self.embedding_count = 0
        self.per_camera: dict[str, dict[str, Any]] = {}
        self.gallery_dir = gallery_dir
        self.gallery_limit = gallery_limit
        self.gallery_saved = 0
        self.gallery_interval_seconds = gallery_interval_seconds
        self.gallery_last_saved: dict[tuple, float] = {}
        if gallery_dir is not None and gallery_limit:
            gallery_dir.mkdir(parents=True, exist_ok=True)
            self.gallery_saved = sum(path.is_dir() for path in gallery_dir.iterdir())

    @staticmethod
    def _check_ids(promised: list[str], supplied: list[str], kind: str) -> None:
        if (len(promised) != len(set(promised))
                or len(supplied) != len(set(supplied))
                or set(promised) != set(supplied)):
            raise ValueError(f"promised {kind} missing, extra or duplicated")

    @staticmethod
    def _check_association(parent: dict[str, Any], child: dict[str, Any]) -> None:
        # Child schemas differ: evidence has no camera/session/presence fields;
        # scene observations have no presence field, semantic embeddings use null.
        for field in ("deployment_id", "camera_id", "stream_session_id",
                      "source_frame_id", "frame_id", "presence_id", "use_case_id"):
            if field in parent and field in child and parent[field] != child[field]:
                raise ValueError(f"{field} association mismatch")

    def _save_gallery(self, record: dict[str, Any], manifest: dict[str, Any],
                      parts: dict[str, bytes]) -> None:
        if (self.gallery_dir is None or self.gallery_saved >= self.gallery_limit
                or not manifest["evidence"]):
            return
        # Optional JPEG retention only: every package still receives complete
        # validation, its audit record and its simulated ACK. Keep separate
        # sampling slots for cameras, sessions, bindings and modalities.
        key = (record.get('camera_id'), record.get('stream_session_id'),
               record.get('use_case_id'), record.get('event_type'),
               tuple(record.get('embeddings', [])))
        previous = self.gallery_last_saved.get(key)
        if (self.gallery_interval_seconds and previous is not None
                and record['accepted_mono'] - previous < self.gallery_interval_seconds):
            return
        directory = self.gallery_dir / manifest["package_id"]
        if directory.exists():
            return
        directory.mkdir()
        for item in manifest["evidence"]:
            suffix = ".jpg" if item["metadata"]["content_type"] == "image/jpeg" else ".png"
            (directory / (item["part_name"] + suffix)).write_bytes(parts[item["part_name"]])
        # Explicit audit record, not a replayable manifest (vectors omitted).
        (directory / "metadata.json").write_bytes(canonical(record))
        self.gallery_saved += 1
        self.gallery_last_saved[key] = record['accepted_mono']

    def _write_status(self, status: dict[str, Any]) -> None:
        # Unique temporary in the same directory keeps replacement atomic.
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=self.status_path.parent, delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(canonical(status))
            os.replace(temporary, self.status_path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    @staticmethod
    def digest(manifest: dict[str, Any], evidence: list[tuple[str, bytes]]) -> str:
        value = hashlib.sha256(canonical(manifest))
        for name, body in evidence:
            value.update(b"\0")
            value.update(name.encode())
            value.update(b"\0")
            value.update(body)
        return value.hexdigest()

    def accept(self, manifest: dict[str, Any], parts: dict[str, bytes], *,
               received_mono: float | None = None):
        started = time.monotonic()
        if received_mono is None:
            received_mono = started
        self.validator.validate(manifest)
        self.observation_validator.validate(manifest["observation"])
        for item in manifest["evidence"]:
            self.evidence_validator.validate(item["metadata"])
        for item in manifest["embeddings"]:
            self.embedding_validator.validate(item)
        expected_names = [item["part_name"] for item in manifest["evidence"]]
        if len(expected_names) != len(set(expected_names)) or set(parts) != set(expected_names):
            raise ValueError("multipart evidence parts do not match manifest")
        observation = manifest["observation"]
        if manifest["deployment_id"] != observation["deployment_id"]:
            raise ValueError("deployment_id association mismatch")
        observation_id = observation["observation_id"]
        evidence_ids = set(observation.get("evidence_ids") or [])
        self._check_ids(observation.get("evidence_ids") or [],
                        [item["metadata"]["evidence_id"] for item in manifest["evidence"]],
                        "evidence")
        self._check_ids(observation.get("embedding_ids") or [],
                        [item["embedding_id"] for item in manifest["embeddings"]],
                        "embedding")
        ordered_parts: list[tuple[str, bytes]] = []
        for item in manifest["evidence"]:
            metadata = item["metadata"]
            body = parts[item["part_name"]]
            if metadata["observation_id"] != observation_id:
                raise ValueError("evidence parent mismatch")
            if metadata["evidence_id"] not in evidence_ids:
                raise ValueError("evidence not listed by observation")
            self._check_association(observation, metadata)
            if metadata["size_bytes"] != len(body):
                raise ValueError("evidence size mismatch")
            if metadata["sha256"] != "sha256:" + hashlib.sha256(body).hexdigest():
                raise ValueError("evidence digest mismatch")
            ordered_parts.append((item["part_name"], body))
        embedding_ids = set(observation.get("embedding_ids") or [])
        embedding_norms = {}
        for item in manifest["embeddings"]:
            norm = math.hypot(*item['vector'])
            if not math.isfinite(norm) or abs(norm - 1) > .01:
                raise ValueError('embedding must be finite and L2 normalized')
            embedding_norms[item['embedding_id']] = norm
            if item["observation_id"] != observation_id:
                raise ValueError("embedding parent mismatch")
            if item["embedding_id"] not in embedding_ids:
                raise ValueError("embedding not listed by observation")
            if item["evidence_id"] not in evidence_ids:
                raise ValueError("embedding evidence mismatch")
            self._check_association(observation, item)
            if item["presence_id"] != observation.get("presence_id"):
                raise ValueError("presence_id association mismatch")
            for field in ("kind", "profile_id"):
                if field in observation and observation[field] != item[field]:
                    raise ValueError(f"{field} association mismatch")
            linked = next(e["metadata"] for e in manifest["evidence"]
                          if e["metadata"]["evidence_id"] == item["evidence_id"])
            self._check_association(linked, item)

        digest = self.digest(manifest, ordered_parts)
        package_id = manifest["package_id"]
        validated_mono = time.monotonic()
        with self.lock:
            previous = self.packages.get(package_id)
            if previous is not None:
                if previous[0] != digest:
                    return 409, {
                        "error": {"code": "package_id_conflict", "message": "conflict", "retryable": False}
                    }
                return 200, {
                    "status": "duplicate", "package_id": package_id,
                    "deployment_id": manifest["deployment_id"], "sequences": previous[1],
                }
            accepted_mono = time.monotonic()
            accepted_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
            objects = [("observation", observation_id)]
            objects.extend(
                ("evidence", item["metadata"]["evidence_id"])
                for item in manifest["evidence"]
            )
            objects.extend(
                ("embedding", item["embedding_id"])
                for item in manifest["embeddings"]
            )
            first = self.next_sequence
            sequences = [
                {"sequence": first + index, "object_type": kind, "object_id": identifier}
                for index, (kind, identifier) in enumerate(objects)
            ]
            self.next_sequence += len(sequences)
            self.packages[package_id] = (digest, sequences)
            event_type = observation["event_type"]
            camera_id = observation["camera_id"]
            self.counts[event_type] = self.counts.get(event_type, 0) + 1
            self.cameras[camera_id] = self.cameras.get(camera_id, 0) + 1
            camera = self.per_camera.setdefault(camera_id, {
                "packages": 0, "event_types": {}, "embedding_profiles": {},
            })
            camera["packages"] += 1
            camera["event_types"][event_type] = camera["event_types"].get(event_type, 0) + 1
            for item in manifest["embeddings"]:
                profile = item["profile_id"]
                camera["embedding_profiles"][profile] = camera["embedding_profiles"].get(profile, 0) + 1
            self.evidence_bytes += sum(len(body) for _, body in ordered_parts)
            self.embedding_count += len(manifest["embeddings"])
            record = {
                "package_id": package_id,
                "camera_id": camera_id,
                "stream_session_id": observation.get("stream_session_id"),
                "frame_id": observation.get("frame_id"),
                "event_type": event_type,
                "presence_id": observation.get("presence_id"),
                "track_id": observation.get("track_id"),
                "plate_text": observation.get("plate_text"),
                "plate_outcome": observation.get("outcome"),
                "hazard": observation.get("hazard"),
                "hazard_episode_id": observation.get("hazard_episode_id"),
                "evidence": [item["metadata"]["evidence_type"] for item in manifest["evidence"]],
                "embeddings": [item["profile_id"] for item in manifest["embeddings"]],
                "observed_at": observation["observed_at"],
                "sequences": sequences,
                "accepted_at": accepted_at,
                "accepted_mono": accepted_mono,
                "received_mono": received_mono,
                "observation_id": observation_id,
                "deployment_id": manifest["deployment_id"],
                "evidence_ids": observation.get("evidence_ids", []),
                "embedding_ids": observation.get("embedding_ids", []),
                "evidence_metadata": [dict(item["metadata"], part_name=item["part_name"])
                                      for item in manifest["evidence"]],
                "embedding_metadata": [dict(
                    {key: value for key, value in item.items() if key != "vector"},
                    dims=len(item["vector"]),
                    l2_norm=embedding_norms[item['embedding_id']],
                ) for item in manifest["embeddings"]],
            }
            for field in (
                "config_revision", "use_case_id", "reason", "sample_reason", "scope",
                "trigger", "triggering_observation_id", "zone_id", "zone_episode_id",
                "zone_episodes", "line_id", "direction", "direction_raw", "source_frame_id",
                "source_bbox_normalized", "bbox_normalized", "plate_bbox_normalized",
                "kind", "profile_id", "object_type", "vehicle_type",
            ):
                if field in observation:
                    record[field] = observation[field]
            gallery_started = time.monotonic()
            self._save_gallery(record, manifest, parts)
            record["fixture_overhead"] = {
                "validation_seconds": validated_mono - started,
                "lock_wait_seconds": accepted_mono - validated_mono,
                "gallery_seconds": time.monotonic() - gallery_started,
                "receipt_to_log_seconds": time.monotonic() - received_mono,
            }
            with self.log_path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(record, separators=(",", ":")) + "\n")
            self._write_status({
                "packages": len(self.packages), "next_sequence": self.next_sequence,
                "event_types": self.counts, "cameras": self.cameras,
                "evidence_bytes": self.evidence_bytes,
                "embeddings": self.embedding_count,
                "per_camera": self.per_camera,
                "gallery": {"enabled": bool(self.gallery_limit), "limit": self.gallery_limit,
                            "saved_packages": self.gallery_saved,
                            **({"interval_seconds_per_camera_binding_modality": self.gallery_interval_seconds}
                               if self.gallery_interval_seconds else {})},
                "fixture_overhead": {
                    **record["fixture_overhead"],
                    "receipt_to_status_seconds": time.monotonic() - received_mono,
                    "scope": "Synchronous fixture work; excludes final status write and socket ACK. "
                             "Includes parsing for HTTP calls; not production storage latency.",
                },
                "ack_semantics": "Simulated durable; no production storage fsync or remote durability; "
                                 "duplicate knowledge is process-local.",
            })
            return 201, {
                "status": "durable", "package_id": package_id,
                "deployment_id": manifest["deployment_id"], "sequences": sequences,
            }


class Handler(socketserver.StreamRequestHandler):
    store: Store

    def handle(self) -> None:
        request = self.rfile.readline().decode("ascii", "replace").strip().split()
        if request[:2] != ["POST", "/v1/packages"]:
            self._send(404, {"error": {"code": "not_found", "message": "not found", "retryable": False}})
            return
        headers = {}
        while True:
            line = self.rfile.readline()
            if line in {b"\r\n", b"\n", b""}:
                break
            key, value = line.decode("latin-1").split(":", 1)
            headers[key.lower()] = value.strip()
        try:
            length = int(headers["content-length"])
            if not 0 <= length <= 64 * 1024 * 1024:
                raise ValueError("invalid request size")
            body = self.rfile.read(length)
            received_mono = time.monotonic()
            if len(body) != length:
                raise ValueError("truncated request body")
            message = BytesParser(policy=email_policy).parsebytes(
                f"Content-Type: {headers['content-type']}\r\nMIME-Version: 1.0\r\n\r\n".encode()
                + body
            )
            if not message.is_multipart() or message.defects:
                raise ValueError("invalid multipart body")
            parts = {}
            for part in message.iter_parts():
                name = part.get_param("name", header="content-disposition")
                if name in parts:
                    raise ValueError("duplicate multipart part name")
                parts[name] = part.get_payload(decode=True)
            manifest = json.loads(parts.pop("manifest"))
            status, payload = self.store.accept(manifest, parts, received_mono=received_mono)
        except Exception as exc:  # noqa: BLE001
            # Schema/parser exceptions can quote arbitrary submitted data. Never
            # echo request contents, credentials, URLs or vectors into fixture logs.
            print(f"invalid_package: {type(exc).__name__}", file=sys.stderr, flush=True)
            status, payload = 400, {
                "error": {"code": "invalid_package", "message": "package validation failed", "retryable": False}
            }
        self._send(status, payload)

    def _send(self, status: int, payload: dict[str, Any]) -> None:
        reason = {200: "OK", 201: "Created", 400: "Bad Request", 404: "Not Found", 409: "Conflict"}.get(status, "Error")
        body = canonical(payload)
        self.wfile.write(
            f"HTTP/1.1 {status} {reason}\r\nContent-Type: application/json\r\nContent-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode()
            + body
        )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--socket", type=Path, required=True)
    parser.add_argument("--schemas", type=Path, required=True)
    parser.add_argument("--log", type=Path, required=True)
    parser.add_argument("--status", type=Path, required=True)
    parser.add_argument("--gallery-dir", type=Path, help="retain evidence bytes and vector-free audit metadata")
    parser.add_argument("--gallery-limit", type=int, default=0,
                        help="maximum packages retained globally, including existing directories (0 disables)")
    parser.add_argument("--gallery-interval-seconds", type=float, default=0,
                        help="JPEG sampling interval per camera/session/binding/modality; audits all packages")
    args = parser.parse_args()
    if args.gallery_limit < 0 or (args.gallery_limit and args.gallery_dir is None):
        parser.error("gallery-limit must be nonnegative and requires gallery-dir when enabled")
    if not math.isfinite(args.gallery_interval_seconds) or args.gallery_interval_seconds < 0:
        parser.error("gallery interval must be finite and nonnegative")
    args.socket.parent.mkdir(parents=True, exist_ok=True)
    args.log.parent.mkdir(parents=True, exist_ok=True)
    args.status.parent.mkdir(parents=True, exist_ok=True)
    args.socket.unlink(missing_ok=True)
    Handler.store = Store(args.schemas, args.log, args.status, args.gallery_dir,
                         args.gallery_limit, args.gallery_interval_seconds)
    with socketserver.ThreadingUnixStreamServer(str(args.socket), Handler) as server:
        os.chmod(args.socket, 0o666)
        server.serve_forever()


if __name__ == "__main__":
    main()
