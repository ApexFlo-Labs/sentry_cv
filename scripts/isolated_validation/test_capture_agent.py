"""Offline capture-fixture checks; no runtime, remote host or live socket tests.

The canonical package/evidence samples contain a placeholder size and SHA-256,
not an actual JPEG. Read and check that digest before supplying the embedded
1x1 JPEG and its computed metadata. Only stdlib plus the agent's existing
jsonschema dependency are used.
"""
from __future__ import annotations

import base64
from concurrent.futures import ThreadPoolExecutor
import copy
from datetime import datetime, timezone
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
from uuid import uuid4

from jsonschema import Draft202012Validator, ValidationError


SPEC = importlib.util.spec_from_file_location(
    "capture_agent", Path(__file__).with_name("v8_capture_agent.py")
)
agent = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(agent)
SCHEMAS = Path(__file__).resolve().parents[2] / "newdetails" / "sentry-v8"

# A complete, fixed 1x1 RGB JPEG. No image codec dependency at test time.
JPEG = base64.b64decode(
    "/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAAgGBgcGBQgHBwcJCQgKDBQNDAsLDBkSEw8UHRof"
    "Hh0aHBwgJC4nICIsIxwcKDcpLDAxNDQ0Hyc5PTgyPC4zNDL/2wBDAQkJCQwLDBgNDRgyIRwh"
    "MjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIy"
    "MjIyMjL/wAARCAABAAEDASIAAhEBAxEB/8QAHwAAAQUBAQEBAQEAAAAAAAAAAAECAwQFBgcICQoL"
    "/8QAtRAAAgEDAwIEAwUFBAQAAAF9AQIDAAQRBRIhMUEGE1FhByJxFDKBkaEII0KxwRVS0fAk"
    "M2JyggkKFhcYGRolJicoKSo0NTY3ODk6Q0RFRkdISUpTVFVWV1hZWmNkZWZnaGlqc3R1dnd4"
    "eXqDhIWGh4iJipKTlJWWl5iZmqKjpKWmp6ipqrKztLW2t7i5usLDxMXGx8jJytLT1NXW19jZ"
    "2uHi4+Tl5ufo6erx8vP09fb3+Pn6/8QAHwEAAwEBAQEBAQEBAQAAAAAAAAECAwQFBgcICQoL"
    "/8QAtREAAgECBAQDBAcFBAQAAQJ3AAECAxEEBSExBhJBUQdhcRMiMoEIFEKRobHBCSMzUvAV"
    "YnLRChYkNOEl8RcYGRomJygpKjU2Nzg5OkNERUZHSElKU1RVVldYWVpjZGVmZ2hpanN0dXZ3"
    "eHl6goOEhYaHiImKkpOUlZaXmJmaoqOkpaanqKmqsrO0tba3uLm6wsPExcbHyMnK0tPU1dbX"
    "2Nna4uPk5ebn6Onq8vP09fb3+Pn6/9oADAMBAAIRAxEAPwDDooooA//Z"
)
OTHER_ID = "00000000-0000-4000-8000-000000000000"


class CaptureAgentTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        sample = json.loads((SCHEMAS / "delivery-agent-package.example.json").read_text())
        metadata = json.loads((SCHEMAS / "evidence.example.json").read_text())
        # Inspect the original sample digest before creating the byte fixture.
        self.assertEqual(sample["manifest"]["evidence"][0]["metadata"], metadata)
        self.assertEqual(metadata["sha256"], "sha256:" + "a" * 64)
        self.assertEqual(metadata["size_bytes"], 182400)
        self.manifest = copy.deepcopy(sample["manifest"])
        metadata.update(size_bytes=len(JPEG), width=1, height=1,
                        sha256="sha256:" + hashlib.sha256(JPEG).hexdigest())
        self.manifest["evidence"][0]["metadata"] = metadata
        self.parts = {"evidence_0": JPEG}
        self.examples = json.loads((SCHEMAS / "observation-examples.json").read_text())
        self.store = agent.Store(SCHEMAS, self.root / "accepted.jsonl", self.root / "status.json")

    def records(self):
        return [json.loads(line) for line in self.store.log_path.read_text().splitlines()]

    def status(self):
        return json.loads(self.store.status_path.read_text())

    def package(self, event_type, *, camera="entrance-camera"):
        manifest = copy.deepcopy(self.manifest)
        manifest["package_id"] = str(uuid4())
        observation = copy.deepcopy(next(o for o in self.examples if o["event_type"] == event_type))
        observation["camera_id"] = camera
        manifest["observation"] = observation
        if observation.get("evidence_ids"):
            metadata = manifest["evidence"][0]["metadata"]
            metadata.update(evidence_id=observation["evidence_ids"][0],
                            observation_id=observation["observation_id"],
                            source_frame_id=observation["source_frame_id"],
                            captured_at=observation["observed_at"])
        else:
            manifest["evidence"] = []
        if observation.get("embedding_ids"):
            face = event_type == "person_feature_sample"
            dims = 512 if face else 768
            manifest["embeddings"] = [{
                "schema_version": "7.0", "embedding_id": observation["embedding_ids"][0],
                "observation_id": observation["observation_id"],
                "use_case_id": observation["use_case_id"],
                "evidence_id": observation["evidence_ids"][0],
                "profile_id": "adaface-ir101-v18.1" if face else "siglip2-base-v1",
                "kind": "face" if face else "semantic", "vector": [1.0] + [0.0] * (dims - 1),
                "captured_at": observation["observed_at"],
                "source_frame_id": observation["source_frame_id"],
                "presence_id": observation.get("presence_id"),
                "model_id": "offline-test", "model_version": "1",
                "preprocessing_id": "offline-test", "normalization": "l2",
            }]
            if face:
                manifest["embeddings"][0]["quality"] = observation["quality"]
                metadata.update(evidence_type="face_crop", source_bbox_normalized={
                    "x1": 0.2, "y1": 0.2, "x2": 0.8, "y2": 0.8,
                })
        return manifest

    def assert_rejected(self, manifest, parts=None):
        with self.assertRaises((ValueError, ValidationError)):
            self.store.accept(manifest, self.parts if parts is None else parts)
        self.assertEqual(self.store.packages, {})
        self.assertEqual(self.store.next_sequence, 1)
        self.assertFalse(self.store.log_path.exists())
        self.assertFalse(self.store.status_path.exists())

    def test_valid_metadata_and_legacy_ack_fields(self):
        code, ack = self.store.accept(self.manifest, self.parts)
        self.assertEqual(code, 201)
        self.assertEqual(ack["status"], "durable")
        self.assertEqual(set(ack), {"status", "package_id", "deployment_id", "sequences"})
        Draft202012Validator(json.loads(
            (SCHEMAS / "delivery-agent-acknowledgement.schema.json").read_text()
        )).validate(ack)
        record, = self.records()
        for field in ("package_id", "camera_id", "stream_session_id", "frame_id", "event_type",
                      "presence_id", "track_id", "plate_text", "plate_outcome", "hazard",
                      "hazard_episode_id", "evidence", "embeddings", "observed_at", "sequences"):
            self.assertIn(field, record)
        self.assertEqual(record["frame_id"], None)  # Keep its old meaning; source has its own field.
        self.assertEqual(record["source_frame_id"], self.manifest["observation"]["source_frame_id"])
        self.assertEqual(record["evidence"], ["frame"])
        self.assertEqual(record["embeddings"], [])
        self.assertEqual(record["evidence_ids"], self.manifest["observation"]["evidence_ids"])
        self.assertEqual(record["evidence_metadata"][0], dict(
            self.manifest["evidence"][0]["metadata"], part_name="evidence_0"))
        self.assertEqual(record["use_case_id"], "plate-reading")

    def test_timestamp_fields_and_fixture_overhead_scope(self):
        before = datetime.now(timezone.utc)
        received = time.monotonic()
        self.store.accept(self.manifest, self.parts, received_mono=received)
        after = datetime.now(timezone.utc)
        record, = self.records()
        self.assertTrue(record["accepted_at"].endswith("Z"))
        accepted = datetime.fromisoformat(record["accepted_at"].replace("Z", "+00:00"))
        self.assertLessEqual(before, accepted)
        self.assertLessEqual(accepted, after)
        self.assertEqual(record["received_mono"], received)
        self.assertLessEqual(received, record["accepted_mono"])
        self.assertLessEqual(record["accepted_mono"], time.monotonic())
        self.assertNotEqual(record["accepted_at"], record["observed_at"])
        self.assertTrue(all(value >= 0 for value in record["fixture_overhead"].values()))
        status = self.status()
        self.assertIn("excludes final status write and socket ACK", status["fixture_overhead"]["scope"])
        self.assertIn("Simulated durable", status["ack_semantics"])
        self.assertIn("no production storage fsync", status["ack_semantics"])

    def test_duplicate_ack_is_stable_without_any_second_write(self):
        first = self.store.accept(self.manifest, self.parts)
        original_log = self.store.log_path.read_bytes()
        original_status = self.store.status_path.read_bytes()
        with patch.object(self.store, "_save_gallery", side_effect=AssertionError("duplicate gallery write")):
            for _ in range(2):
                code, ack = self.store.accept(copy.deepcopy(self.manifest), dict(self.parts))
                self.assertEqual(code, 200)
                self.assertEqual(ack, dict(first[1], status="duplicate"))
        self.assertEqual(self.store.log_path.read_bytes(), original_log)
        self.assertEqual(self.store.status_path.read_bytes(), original_status)
        self.assertEqual(len(self.records()), 1)

    def test_conflicting_duplicate_id_has_no_second_log_or_counts(self):
        self.store.accept(self.manifest, self.parts)
        before = self.status()
        changed = copy.deepcopy(self.manifest)
        changed["observation"]["plate_text"] = "CHANGED"
        code, ack = self.store.accept(changed, self.parts)
        self.assertEqual(code, 409)
        self.assertEqual(ack["error"]["code"], "package_id_conflict")
        changed = copy.deepcopy(self.manifest)
        changed_bytes = JPEG + b"changed"
        changed["evidence"][0]["metadata"].update(
            size_bytes=len(changed_bytes), sha256="sha256:" + hashlib.sha256(changed_bytes).hexdigest())
        self.assertEqual(self.store.accept(changed, {"evidence_0": changed_bytes})[0], 409)
        self.assertEqual(self.status(), before)
        self.assertEqual(len(self.records()), 1)

    def test_concurrent_retries_accept_and_log_once(self):
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(
                lambda _: self.store.accept(copy.deepcopy(self.manifest), dict(self.parts)), range(8)
            ))
        self.assertEqual(sorted(code for code, _ in results), [200] * 7 + [201])
        self.assertTrue(all(ack["sequences"] == results[0][1]["sequences"] for _, ack in results))
        self.assertEqual(len(self.records()), 1)
        self.assertEqual(self.status()["packages"], 1)

    def test_geometry_scene_retains_scope_trigger_zone_and_source_bbox(self):
        manifest = self.package("scene_sample")
        observation = manifest["observation"]
        observation.update(scope="geometry", zone_id="test-zone", trigger="event",
                           triggering_observation_id=OTHER_ID)
        bbox = {"x1": 0.2, "y1": 0.3, "x2": 0.6, "y2": 0.8}
        manifest["evidence"][0]["metadata"].update(
            evidence_type="scene_region_crop", source_bbox_normalized=bbox)
        self.assertEqual(self.store.accept(manifest, self.parts)[0], 201)
        record, = self.records()
        for field in ("scope", "trigger", "zone_id", "triggering_observation_id"):
            self.assertEqual(record[field], observation[field])
        self.assertEqual(record["evidence_metadata"][0]["source_bbox_normalized"], bbox)

    def test_embedding_dimensions_are_validated_before_metadata_is_saved(self):
        manifest = self.package("scene_sample")
        manifest["embeddings"][0]["vector"].pop()
        self.assert_rejected(manifest)

    def test_nonfinite_zero_and_non_normalized_vectors_cannot_be_counted_as_good_output(self):
        for value in (float('nan'), float('inf'), 0.0, 0.5):
            with self.subTest(value=value):
                manifest = self.package('scene_sample')
                manifest['embeddings'][0]['vector'][0] = value
                self.assert_rejected(manifest)

    def test_actual_norm_is_saved_without_vector_values(self):
        manifest = self.package('scene_sample')
        self.assertEqual(self.store.accept(manifest, self.parts)[0], 201)
        record, = self.records()
        self.assertEqual(record['embedding_metadata'][0]['l2_norm'], 1.0)
        self.assertNotIn('vector', record['embedding_metadata'][0])

    def test_promised_ids_and_manifest_ids_are_exact_one_to_one_sets(self):
        for kind in ("evidence", "embedding"):
            for change in ("missing", "unlisted", "duplicate", "promised_duplicate"):
                with self.subTest(kind=kind, change=change):
                    manifest = self.package("scene_sample")
                    ids = manifest["observation"][kind + "_ids"]
                    items = manifest["evidence" if kind == "evidence" else "embeddings"]
                    if change == "missing":
                        ids.append(OTHER_ID)
                    elif change == "unlisted":
                        ids[:] = [OTHER_ID]
                    elif change == "promised_duplicate":
                        ids.append(ids[0])
                    else:
                        duplicate = copy.deepcopy(items[0])
                        if kind == "evidence":
                            duplicate["part_name"] = "evidence_1"
                        else:
                            duplicate["model_version"] = "2"  # Distinct JSON, same ID.
                        items.append(duplicate)
                    parts = dict(self.parts)
                    if kind == "evidence" and change == "duplicate":
                        parts["evidence_1"] = JPEG
                    self.assert_rejected(manifest, parts)

    def test_missing_promised_objects_fail(self):
        for field in ("evidence", "embeddings"):
            with self.subTest(field=field):
                manifest = self.package("scene_sample")
                manifest[field] = []
                self.assert_rejected(manifest, {} if field == "evidence" else self.parts)

    def test_tampered_associations_fail(self):
        changes = [
            ("manifest", "deployment_id", "another-deployment"),
            ("evidence", "observation_id", OTHER_ID),
            ("evidence", "source_frame_id", "another-frame"),
            ("embedding", "observation_id", OTHER_ID),
            ("embedding", "source_frame_id", "another-frame"),
            ("embedding", "evidence_id", OTHER_ID),
            ("embedding", "use_case_id", "another-use-case"),
            ("embedding", "presence_id", OTHER_ID),
        ]
        for target, field, value in changes:
            with self.subTest(target=target, field=field):
                manifest = self.package("person_feature_sample")
                entity = {"manifest": manifest, "evidence": manifest["evidence"][0]["metadata"],
                          "embedding": manifest["embeddings"][0]}[target]
                entity[field] = value
                self.assert_rejected(manifest)

    def test_evidence_cannot_invent_observation_camera_session_presence_fields(self):
        for target in ("evidence", "embedding"):
            for field in ("camera_id", "stream_session_id", "presence_id"):
                if target == "embedding" and field == "presence_id":
                    continue  # Its real schema defines presence; checked above.
                with self.subTest(target=target, field=field):
                    manifest = self.package("scene_sample")
                    entity = (manifest["evidence"][0]["metadata"] if target == "evidence"
                              else manifest["embeddings"][0])
                    entity[field] = OTHER_ID
                    self.assert_rejected(manifest)

    def test_scene_null_presence_valid_and_nonnull_presence_rejected(self):
        manifest = self.package("scene_sample")
        self.assertNotIn("presence_id", manifest["observation"])
        self.assertIsNone(manifest["embeddings"][0]["presence_id"])
        changed = copy.deepcopy(manifest)
        changed["embeddings"][0]["presence_id"] = OTHER_ID
        self.assert_rejected(changed)
        self.assertEqual(self.store.accept(manifest, self.parts)[0], 201)
        record, = self.records()
        metadata, = record["embedding_metadata"]
        self.assertEqual(metadata["dims"], 768)
        self.assertEqual(metadata["profile_id"], "siglip2-base-v1")
        self.assertEqual(metadata["evidence_id"], record["evidence_ids"][0])
        self.assertNotIn("vector", metadata)
        self.assertIsNone(record["presence_id"])

    def test_tampered_bytes_parts_and_schema_fail(self):
        cases = [
            ("missing-part", self.manifest, {}),
            ("extra-part", self.manifest, dict(self.parts, evidence_1=JPEG)),
            ("truncated-bytes", self.manifest, {"evidence_0": JPEG[:-1]}),
            ("changed-bytes", self.manifest, {"evidence_0": JPEG[:-1] + b"x"}),
        ]
        for name, manifest, parts in cases:
            with self.subTest(case=name):
                self.assert_rejected(manifest, parts)
        for field, value in (("sha256", "sha256:" + "0" * 64), ("size_bytes", len(JPEG) + 1),
                             ("width", 0), ("captured_at", "2026-10-01T10:00:02+05:30")):
            with self.subTest(field=field):
                manifest = copy.deepcopy(self.manifest)
                manifest["evidence"][0]["metadata"][field] = value
                self.assert_rejected(manifest)
        manifest = copy.deepcopy(self.manifest)
        duplicate = copy.deepcopy(manifest["evidence"][0])
        duplicate["metadata"]["evidence_id"] = OTHER_ID
        manifest["evidence"].append(duplicate)
        manifest["observation"]["evidence_ids"].append(OTHER_ID)
        self.assert_rejected(manifest)  # Distinct IDs, repeated part_name.

    def test_per_camera_counts_metadata_and_no_evidence_health(self):
        for camera in ("camera-a", "camera-b"):
            for event in ("scene_sample", "person_feature_sample", "object_present", "line_cross", "camera_health"):
                manifest = self.package(event, camera=camera)
                self.assertEqual(self.store.accept(manifest, self.parts if manifest["evidence"] else {})[0], 201)
        status = self.status()
        self.assertEqual(status["packages"], 10)
        self.assertEqual(status["cameras"], {"camera-a": 5, "camera-b": 5})
        self.assertEqual(status["embeddings"], 4)
        self.assertEqual(status["evidence_bytes"], 8 * len(JPEG))
        for camera in status["per_camera"].values():
            self.assertEqual(camera["packages"], 5)
            self.assertEqual(camera["event_types"], dict.fromkeys(
                ("scene_sample", "person_feature_sample", "object_present", "line_cross", "camera_health"), 1))
            self.assertEqual(camera["embedding_profiles"], {"siglip2-base-v1": 1, "adaface-ir101-v18.1": 1})
        by_type = {r["event_type"]: r for r in self.records()}
        for event, fields in {
            "scene_sample": ("scope", "trigger", "zone_id"),
            "person_feature_sample": ("sample_reason", "kind", "profile_id"),
            "object_present": ("reason", "bbox_normalized", "zone_episodes"),
            "line_cross": ("line_id", "direction", "direction_raw"),
        }.items():
            original = next(o for o in self.examples if o["event_type"] == event)
            for field in fields:
                self.assertEqual(by_type[event][field], original[field])
        self.assertEqual(by_type["camera_health"]["evidence_ids"], [])
        self.assertEqual(by_type["camera_health"]["embedding_ids"], [])

    def test_gallery_disabled_by_default_and_zero(self):
        gallery = self.root / "disabled"
        self.store = agent.Store(SCHEMAS, self.root / "accepted.jsonl", self.root / "status.json", gallery)
        self.store.accept(self.manifest, self.parts)
        self.assertFalse(gallery.exists())
        self.assertEqual(self.status()["gallery"], {"enabled": False, "limit": 0, "saved_packages": 0})

    def test_bounded_gallery_saves_exact_bytes_and_vector_free_metadata(self):
        gallery = self.root / "gallery"
        self.store = agent.Store(SCHEMAS, self.root / "accepted.jsonl", self.root / "status.json", gallery, 1)
        manifest = self.package("scene_sample")
        self.store.accept(manifest, self.parts)
        self.store.accept(manifest, self.parts)
        self.store.accept(self.package("person_feature_sample"), self.parts)
        self.assertEqual(len(list(gallery.iterdir())), 1)
        directory = gallery / manifest["package_id"]
        self.assertEqual((directory / "evidence_0.jpg").read_bytes(), JPEG)
        metadata = json.loads((directory / "metadata.json").read_text())
        self.assertEqual(metadata["evidence_metadata"][0]["sha256"], "sha256:" + hashlib.sha256(JPEG).hexdigest())
        self.assertEqual(metadata["embedding_metadata"][0]["dims"], 768)
        self.assertNotIn("vector", metadata["embedding_metadata"][0])
        self.assertEqual(self.status()["gallery"]["saved_packages"], 1)
        # An existing gallery consumes the cap even after a new fixture process.
        second = agent.Store(SCHEMAS, self.root / "other.jsonl", self.root / "other-status.json", gallery, 1)
        second.accept(self.package("person_feature_sample"), self.parts)
        self.assertEqual(len(list(gallery.iterdir())), 1)

    def test_gallery_arguments_reject_invalid_bounds(self):
        for gallery, limit in ((None, 1), (self.root / "gallery", -1)):
            with self.subTest(limit=limit), self.assertRaises(ValueError):
                agent.Store(SCHEMAS, self.root / "log", self.root / "status", gallery, limit)

    def test_temporal_gallery_keeps_late_pixels_and_all_package_audits(self):
        gallery = self.root / 'temporal'
        self.store = agent.Store(SCHEMAS, self.root/'accepted.jsonl',
                                 self.root/'status.json', gallery, 10, 30)
        clock = [100.0]
        with patch.object(agent.time, 'monotonic', side_effect=lambda: clock[0]):
            first = self.package('scene_sample', camera='one')
            self.store.accept(first, self.parts)
            clock[0] = 110.0
            skipped = self.package('scene_sample', camera='one')
            self.store.accept(skipped, self.parts)
            # A second camera and a different modality have independent slots.
            second = self.package('scene_sample', camera='two')
            self.store.accept(second, self.parts)
            face = self.package('person_feature_sample', camera='one')
            self.store.accept(face, self.parts)
            clock[0] = 131.0
            late = self.package('scene_sample', camera='one')
            self.store.accept(late, self.parts)
        self.assertEqual(len(self.records()), 5)
        self.assertEqual(self.status()['packages'], 5)
        self.assertEqual(self.status()['gallery']['saved_packages'], 4)
        self.assertFalse((gallery/skipped['package_id']).exists())
        self.assertEqual((gallery/late['package_id']/'evidence_0.jpg').read_bytes(), JPEG)
        self.assertEqual(self.status()['gallery']['interval_seconds_per_camera_binding_modality'], 30)

    def test_temporal_gallery_rejects_nonfinite_or_negative_interval(self):
        for value in (-1, float('nan'), float('inf')):
            with self.subTest(value=value), self.assertRaises(ValueError):
                agent.Store(SCHEMAS, self.root/'log', self.root/'status',
                            self.root/'gallery', 10, value)

    def test_status_replacement_is_atomic_and_duplicate_does_not_replace(self):
        original_replace = agent.os.replace
        replacements = []

        def replace(source, destination):
            self.assertEqual(source.parent, self.store.status_path.parent)
            self.assertNotEqual(source, destination)
            replacements.append(json.loads(source.read_text()))
            original_replace(source, destination)

        with patch.object(agent.os, "replace", side_effect=replace):
            self.store.accept(self.manifest, self.parts)
            self.store.accept(self.manifest, self.parts)
            self.store.accept(self.package("scene_sample"), self.parts)
        self.assertEqual([s["packages"] for s in replacements], [1, 2])
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), ["accepted.jsonl", "status.json"])

    def request(self, parts, method="POST", path="/v1/packages", *,
                body_transform=None, length_delta=0):
        # Exercise the real HTTP parser and responses entirely in memory.
        boundary = b"offline-test-boundary"
        body = b"".join(
            b"--" + boundary + b'\r\nContent-Disposition: form-data; name="' + name.encode()
            + b'"\r\n\r\n' + data + b"\r\n" for name, data in parts
        ) + b"--" + boundary + b"--\r\n"
        if body_transform is not None:
            body = body_transform(body)
        request = (f"{method} {path} HTTP/1.1\r\nContent-Length: {len(body) + length_delta}\r\n"
                   "Content-Type: multipart/form-data; boundary=offline-test-boundary\r\n"
                   "X-Test-URL: rtsps://private:password@example.invalid/camera\r\n"
                   "X-Test-TLS-Key: PRIVATE-KEY-MARKER\r\n\r\n").encode() + body
        handler = object.__new__(agent.Handler)
        handler.store = self.store
        handler.rfile = io.BytesIO(request)
        handler.wfile = io.BytesIO()
        errors = io.StringIO()
        with patch.object(agent.sys, "stderr", errors):
            handler.handle()
        headers, payload = handler.wfile.getvalue().split(b"\r\n\r\n", 1)
        return int(headers.split()[1]), json.loads(payload), errors.getvalue()

    def test_in_memory_http_truncated_body_and_multipart_fail(self):
        parts = [("manifest", agent.canonical(self.manifest)), ("evidence_0", JPEG)]
        self.assertEqual(self.request(parts, length_delta=10)[0], 400)
        self.assertEqual(self.request(
            parts, body_transform=lambda body: body.rsplit(b"--offline-test-boundary--", 1)[0]
        )[0], 400)
        self.assertFalse(self.store.log_path.exists())

    def test_in_memory_http_success_duplicate_and_duplicate_part_rejection(self):
        parts = [("manifest", agent.canonical(self.manifest)), ("evidence_0", JPEG)]
        first = self.request(parts)
        self.assertEqual(first[0], 201)
        duplicate = self.request(parts)
        self.assertEqual(duplicate[0], 200)
        self.assertEqual(duplicate[1]["sequences"], first[1]["sequences"])
        for extra in (parts[:1], parts[1:]):
            with self.subTest(extra=extra[0][0]):
                code, body, _ = self.request(parts + extra)
                self.assertEqual(code, 400)
                self.assertEqual(body["error"]["code"], "invalid_package")
        self.assertEqual(self.request(parts, path="/not-found")[0], 404)
        self.assertEqual(len(self.records()), 1)
        for data in (self.store.log_path.read_text(), self.store.status_path.read_text()):
            self.assertNotIn("private:password", data)
            self.assertNotIn("PRIVATE-KEY-MARKER", data)

    def test_invalid_http_never_echoes_private_request_data(self):
        manifest = copy.deepcopy(self.manifest)
        manifest["fullURL"] = "rtsps://private:password@example.invalid/camera"
        manifest["private_tls_key"] = "PRIVATE-KEY-MARKER"
        code, body, error_log = self.request([
            ("manifest", agent.canonical(manifest)), ("evidence_0", JPEG),
        ])
        self.assertEqual(code, 400)
        combined = json.dumps(body) + error_log
        self.assertNotIn("private:password", combined)
        self.assertNotIn("PRIVATE-KEY-MARKER", combined)
        self.assertFalse(self.store.log_path.exists())


if __name__ == "__main__":
    unittest.main()
