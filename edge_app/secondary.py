"""CPU crop/preparation with shared NPU embedding inference."""
from __future__ import annotations

from collections import deque
import json
import logging
import os
from pathlib import Path
import queue
import re
import threading
import time
from types import SimpleNamespace

import cv2
import numpy as np

from .pipeline import DetectionResult

log = logging.getLogger(__name__)
ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ_"
INDIAN_PLATE_PATTERNS = (
    re.compile(r"^[A-Z]{2}[0-9]{1,2}[A-Z]{1,3}[0-9]{4}$"),
    re.compile(r"^[0-9]{2}BH[0-9]{4}[A-Z]{1,2}$"),
)


def _indian_plate_text(indices: np.ndarray) -> str:
    """Decode positional OCR output and accept Indian registration formats."""
    blank = ALPHABET.index("_")
    text = "".join(ALPHABET[int(value)] for value in indices if int(value) != blank)
    return text if any(pattern.fullmatch(text) for pattern in INDIAN_PLATE_PATTERNS) else ""


def _letterbox(image: np.ndarray, size: int):
    height, width = image.shape[:2]
    scale = size / max(height, width)
    resized_width, resized_height = round(width * scale), round(height * scale)
    canvas = np.full((size, size, 3), 114, np.uint8)
    pad_x, pad_y = (size - resized_width) // 2, (size - resized_height) // 2
    canvas[pad_y:pad_y + resized_height, pad_x:pad_x + resized_width] = cv2.resize(
        image, (resized_width, resized_height), interpolation=cv2.INTER_LINEAR
    )
    return canvas, scale, pad_x, pad_y


class SecondaryModels:
    def __init__(self, config: dict):
        import openvino as ov
        from openvino import Layout, Type
        from openvino.preprocess import ColorFormat, PrePostProcessor

        core = ov.Core()
        cache_dir = str(Path(config.get("cache_dir", "/tmp/sentrycv-ov-cache")))
        Path(cache_dir).mkdir(parents=True, exist_ok=True)
        core.set_property({"CACHE_DIR": cache_dir})

        plate_model = core.read_model(config["plate_model"])
        ppp = PrePostProcessor(plate_model)
        inp = ppp.input()
        inp.tensor().set_element_type(Type.u8).set_layout(Layout("NHWC")).set_color_format(ColorFormat.BGR)
        inp.model().set_layout(Layout("NCHW"))
        inp.preprocess().convert_element_type(Type.f32).convert_color(ColorFormat.RGB).scale(255.0)
        plate_device = str(config.get("plate_device", "CPU"))
        self.plate = core.compile_model(
            ppp.build(), plate_device, {"PERFORMANCE_HINT": "LATENCY"}
        )
        self.plate_output = self.plate.output(0)

        ocr_device = str(config.get("ocr_device", "CPU"))
        self.ocr = core.compile_model(
            core.read_model(config["ocr_model"]), ocr_device,
            {"PERFORMANCE_HINT": "LATENCY"},
        )
        self.ocr_output = self.ocr.output(0)
        self.plate_size = int(config.get("plate_size", 224))
        self.plate_confidence = float(config.get("plate_confidence", 0.35))
        self.ocr_confidence = float(config.get("ocr_confidence", 0.4))
        self.face = self.body = self.gait = self.scene = None
        devices = {
            "face_detector": str(config.get("face_detector_device", "CPU")),
            "face_embedding": str(config.get("face_embedding_device", "NPU")),
            "body_reid": str(config.get("body_reid_device", "NPU")),
            "gait": str(config.get("gait_device", "NPU")),
            "scene": str(config.get("scene_device", "NPU")),
        }
        for name in ("face_embedding", "body_reid", "gait"):
            if devices[name] != "NPU":
                raise ValueError(f"{name} must use NPU, got {devices[name]}")
        if config.get("scene_enabled", True) and devices["scene"] != "NPU":
            raise ValueError(f"scene must use NPU, got {devices['scene']}")
        os.environ.update({
            "FACE_DETECTOR_DEVICE": devices["face_detector"],
            "FACE_EMBEDDER_DEVICE": devices["face_embedding"],
            "BODY_REID_DEVICE": devices["body_reid"],
            "GAIT_DEVICE": devices["gait"],
            "SCENE_EMBEDDER_DEVICE": devices["scene"],
        })
        if config.get("face_enabled", True):
            from edge_app.detectors.backends.openvino_face import OpenVINOFaceExtractor
            self.face = OpenVINOFaceExtractor(config["face_detector_model"], config["face_embedding_model"])
        if config.get("body_reid_enabled", True):
            from edge_app.runtime_pipeline.person_reid import OpenVINOBodyExtractor
            self.body = OpenVINOBodyExtractor(config["body_reid_model"], devices["body_reid"])
        if config.get("gait_enabled", True):
            from edge_app.runtime_pipeline.person_reid import OpenVINOGaitExtractor
            self.gait = OpenVINOGaitExtractor(config["gait_model"], gait_device=devices["gait"])
        if config.get("scene_enabled", True):
            from edge_app.runtime_pipeline.scene_embeddings import OpenVINOSceneExtractor
            self.scene = OpenVINOSceneExtractor(config["scene_model"], device=devices["scene"])
        for model in (self.face, self.body, self.gait, self.scene):
            if model is not None and hasattr(model, "warmup"):
                model.warmup()
        enabled = [f"plate={plate_device}", f"ocr={ocr_device}"]
        if self.face is not None:
            enabled.extend((f"face-detect={devices['face_detector']}", "face-embed=NPU"))
        if self.body is not None:
            enabled.append("body-reid=NPU")
        if self.gait is not None:
            enabled.append("gait=NPU")
        if self.scene is not None:
            enabled.append("scene=NPU")
        log.info("secondary ready: %s", " ".join(enabled))

    def detect_plate(self, vehicle: np.ndarray) -> list[dict]:
        canvas, scale, pad_x, pad_y = _letterbox(vehicle, self.plate_size)
        rows = np.asarray(self.plate([canvas[None]])[self.plate_output])[0]
        height, width = vehicle.shape[:2]
        output = []
        for row in rows:
            confidence = float(row[4])
            if confidence < self.plate_confidence:
                break
            x1 = int(max(0, min(width, (row[0] - pad_x) / scale)))
            y1 = int(max(0, min(height, (row[1] - pad_y) / scale)))
            x2 = int(max(0, min(width, (row[2] - pad_x) / scale)))
            y2 = int(max(0, min(height, (row[3] - pad_y) / scale)))
            if x2 > x1 and y2 > y1:
                output.append({"bbox": [x1, y1, x2, y2], "confidence": confidence})
        return output

    def read_plate(self, crop: np.ndarray) -> tuple[str, float]:
        rgb = cv2.cvtColor(cv2.resize(crop, (128, 64)), cv2.COLOR_BGR2RGB)
        tensor = rgb.astype(np.float32).transpose(2, 0, 1)[None]
        scores = np.asarray(self.ocr([tensor])[self.ocr_output], dtype=np.float64)
        if scores.ndim == 3:
            scores = scores[0]
        shifted = scores - scores.max(axis=-1, keepdims=True)
        probabilities = np.exp(shifted)
        probabilities /= probabilities.sum(axis=-1, keepdims=True)
        indices = np.argmax(probabilities, axis=-1)
        keep = indices != ALPHABET.index("_")
        text = _indian_plate_text(indices)
        if not np.any(keep):
            return text, 0.0
        selected = probabilities[np.arange(len(indices))[keep], indices[keep]]
        return text, float(np.exp(np.mean(np.log(np.clip(selected, 1e-12, 1.0)))))


class SecondaryPipeline:
    """One shared model set and bounded secondary queue for every camera."""

    VEHICLE_LABELS = {"car", "motorcycle", "bus", "truck"}

    def __init__(self, config: dict, consume):
        self._config = config
        self.models = SecondaryModels(config)
        self.consume = consume
        self.queue: queue.Queue[DetectionResult] = queue.Queue(maxsize=int(config.get("queue_size", 64)))
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._run, name="cpu-secondary", daemon=True)
        self.frame_indices: dict[str, int] = {}
        self.track_state: dict[str, dict[int, list[int]]] = {}
        self.track_misses: dict[str, int] = {}
        self.next_track_id = 1
        self.submitted = 0
        self.dropped = 0
        self.processed = 0
        self.failures = 0

    def start(self):
        self.thread.start()
        return self

    def submit(self, result: DetectionResult) -> None:
        self.submitted += 1
        try:
            self.queue.put_nowait(result)
        except queue.Full:
            self.dropped += 1
            try:
                self.queue.get_nowait()
            except queue.Empty:
                pass
            self.queue.put_nowait(result)

    def _process(self, result: DetectionResult) -> dict:
        frame = result.frame
        payload = {
            "schema_version": "1.0", "camera_id": result.camera_id,
            "timestamp": result.timestamp, "frame": {"width": result.width, "height": result.height},
            "detections": result.detections, "plates": [], "faces": [],
            "body_reid": [], "gait": [], "scene": None,
        }
        if frame is None:
            return payload
        frame_index = self.frame_indices.get(result.camera_id, 0) + 1
        self.frame_indices[result.camera_id] = frame_index
        plate_interval = max(1, int(self.models_config.get("plate_interval", 2)))
        if frame_index % plate_interval == 0:
            for vehicle in result.detections:
                if vehicle["label"] not in self.VEHICLE_LABELS:
                    continue
                x1, y1, x2, y2 = vehicle["bbox"]
                crop = frame[max(0, y1):min(result.height, y2), max(0, x1):min(result.width, x2)]
                if crop.size == 0:
                    continue
                for plate in self.models.detect_plate(crop):
                    px1, py1, px2, py2 = plate["bbox"]
                    plate_crop = crop[py1:py2, px1:px2]
                    if plate_crop.size == 0:
                        continue
                    text, confidence = self.models.read_plate(plate_crop)
                    if confidence < self.models.ocr_confidence:
                        text = ""
                    payload["plates"].append({
                        "vehicle_bbox": vehicle["bbox"],
                        "bbox": [x1 + px1, y1 + py1, x1 + px2, y1 + py2],
                        "detection_confidence": plate["confidence"],
                        "text": text, "ocr_confidence": confidence,
                    })
        people = self._tracked_people(result)
        person_interval = max(1, int(self.models_config.get("person_interval", 5)))
        if people and frame_index % person_interval == 0:
            if self.models.face is not None:
                for sample in self.models.face.extract(frame, people):
                    payload["faces"].append({
                        "track_id": sample.track_id, "bbox": list(sample.bbox),
                        "confidence": sample.detector_confidence,
                        "quality": sample.quality, "embedding_dimensions": int(sample.embedding.size),
                    })
            if self.models.body is not None:
                crops, track_ids = self._person_crops(frame, people)
                vectors = self.models.body.embed(crops)
                payload["body_reid"] = [
                    {"track_id": track_id, "embedding_dimensions": int(vector.size)}
                    for track_id, vector in zip(track_ids, vectors)
                ]
        if people and self.models.gait is not None:
            keys = {int(p.metadata["track_id"]): f"{result.camera_id}:{p.metadata['track_id']}" for p in people}
            vectors = self.models.gait.collect(frame, people, result.camera_id, keys, result.timestamp)
            payload["gait"] = [
                {"track_id": track_id, "embedding_dimensions": int(vector.size)}
                for track_id, vector in vectors.items()
            ]
        scene_interval = max(1, int(self.models_config.get("scene_interval", 50)))
        if self.models.scene is not None and frame_index % scene_interval == 0:
            vector = self.models.scene.embed(frame)
            payload["scene"] = {"embedding_dimensions": int(vector.size)}
        return payload

    @property
    def models_config(self) -> dict:
        return self._config

    @staticmethod
    def _iou(first: list[int], second: list[int]) -> float:
        x1, y1 = max(first[0], second[0]), max(first[1], second[1])
        x2, y2 = min(first[2], second[2]), min(first[3], second[3])
        intersection = max(0, x2 - x1) * max(0, y2 - y1)
        area_a = max(0, first[2] - first[0]) * max(0, first[3] - first[1])
        area_b = max(0, second[2] - second[0]) * max(0, second[3] - second[1])
        union = area_a + area_b - intersection
        return intersection / union if union else 0.0

    def _tracked_people(self, result: DetectionResult) -> list:
        previous = self.track_state.setdefault(result.camera_id, {})
        detections = [
            item for item in result.detections if item["label"] == "person"
        ]
        if not detections:
            misses = self.track_misses.get(result.camera_id, 0) + 1
            self.track_misses[result.camera_id] = misses
            if misses >= int(self.models_config.get("track_hold_frames", 3)):
                self.track_state[result.camera_id] = {}
            return []
        self.track_misses[result.camera_id] = 0
        updated = {}
        people = []
        for detection in detections:
            best = max(previous, key=lambda key: self._iou(detection["bbox"], previous[key]), default=None)
            threshold = float(self.models_config.get("track_iou_threshold", 0.1))
            if best is None or self._iou(detection["bbox"], previous[best]) < threshold or best in updated:
                best = self.next_track_id
                self.next_track_id += 1
            updated[best] = detection["bbox"]
            people.append(SimpleNamespace(bbox=detection["bbox"], metadata={"track_id": best}))
        self.track_state[result.camera_id] = updated
        return people

    @staticmethod
    def _person_crops(frame: np.ndarray, people: list) -> tuple[list[np.ndarray], list[int]]:
        height, width = frame.shape[:2]
        crops, track_ids = [], []
        for person in people:
            x1, y1, x2, y2 = person.bbox
            crop = frame[max(0, y1):min(height, y2), max(0, x1):min(width, x2)]
            if crop.size:
                crops.append(crop)
                track_ids.append(int(person.metadata["track_id"]))
        return crops, track_ids

    def _run(self) -> None:
        while not self.stop_event.is_set():
            try:
                result = self.queue.get(timeout=0.25)
            except queue.Empty:
                continue
            try:
                self.consume(self._process(result))
                self.processed += 1
            except Exception:
                self.failures += 1
                log.exception("secondary inference failed camera=%s", result.camera_id)

    def snapshot(self) -> dict:
        return {
            "submitted": self.submitted,
            "processed": self.processed,
            "dropped": self.dropped,
            "failures": self.failures,
            "queue_depth": self.queue.qsize(),
            "queue_capacity": self.queue.maxsize,
        }

    def close(self, timeout: float = 10.0) -> bool:
        """Stop accepting backlog and wait for the active inference to finish."""
        self.stop_event.set()
        while True:
            try:
                self.queue.get_nowait()
            except queue.Empty:
                break
        self.thread.join(timeout=timeout)
        if self.thread.is_alive():
            log.error("secondary inference did not stop within %.1f seconds", timeout)
            return False
        return True


class DiscardSink:
    """Explicit terminal sink for primary, smoke/fire and secondary results."""

    def __init__(self, metrics_path: Path | None = None):
        self.generated = 0
        self.primary = 0
        self.smoke_fire = 0
        self.started_at = time.time()
        self._samples = deque()
        self._camera_samples: dict[str, deque] = {}
        self._camera_counts: dict[str, dict[str, int]] = {}
        self._last_write = 0.0
        self._lock = threading.Lock()
        self._metrics_path = metrics_path or Path(f"/run/user/{os.getuid()}/sentry-cv-metrics.json")

    def consume(self, payload: dict) -> None:
        now = time.time()
        with self._lock:
            self.generated += 1
            if "smoke_fire" in payload:
                self.smoke_fire += 1
                result_type = "smoke_fire"
            else:
                self.primary += 1
                result_type = "primary"
            self._samples.append(now)
            camera_id = str(payload.get("camera_id", "unknown"))
            camera_samples = self._camera_samples.setdefault(camera_id, deque())
            camera_samples.append((now, result_type))
            counts = self._camera_counts.setdefault(
                camera_id, {"primary_results": 0, "smoke_fire_results": 0}
            )
            counts[f"{result_type}_results"] += 1
            cutoff = now - 10.0
            while self._samples and self._samples[0] < cutoff:
                self._samples.popleft()
            camera_metrics = {}
            for identifier, samples in self._camera_samples.items():
                while samples and samples[0][0] < cutoff:
                    samples.popleft()
                primary_rate = sum(1 for _, kind in samples if kind == "primary") / 10.0
                smoke_rate = sum(1 for _, kind in samples if kind == "smoke_fire") / 10.0
                camera_metrics[identifier] = {
                    **self._camera_counts[identifier],
                    "primary_results_per_second": round(primary_rate, 2),
                    "smoke_fire_results_per_second": round(smoke_rate, 2),
                }
            if now - self._last_write < 1.0:
                return
            metrics = {
                "generated_results": self.generated,
                "primary_results": self.primary,
                "smoke_fire_results": self.smoke_fire,
                "results_per_second": round(len(self._samples) / 10.0, 2),
                "uptime_seconds": round(now - self.started_at, 1),
                "updated_at": now,
                "cameras": camera_metrics,
            }
            temporary = self._metrics_path.with_suffix(".tmp")
            temporary.write_text(json.dumps(metrics), encoding="utf-8")
            temporary.replace(self._metrics_path)
            self._last_write = now
