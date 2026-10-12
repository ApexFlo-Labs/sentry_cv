"""CPU crop/preparation with shared NPU embedding inference."""
from __future__ import annotations

import logging
import os
from pathlib import Path
import queue
import threading
from types import SimpleNamespace

import cv2
import numpy as np

from .pipeline import DetectionResult

log = logging.getLogger(__name__)
ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ_"


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
        self.plate = core.compile_model(ppp.build(), "CPU", {"PERFORMANCE_HINT": "LATENCY"})
        self.plate_output = self.plate.output(0)

        self.ocr = core.compile_model(
            core.read_model(config["ocr_model"]), "CPU", {"PERFORMANCE_HINT": "LATENCY"}
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
        if devices["face_detector"] != "CPU":
            raise ValueError("face detection must remain on CPU in this configuration")
        for name in ("face_embedding", "body_reid", "gait", "scene"):
            if devices[name] != "NPU":
                raise ValueError(f"{name} must use NPU, got {devices[name]}")
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
        log.info(
            "secondary ready: plate=CPU ocr=CPU face-detect=CPU "
            "face-embed=NPU body-reid=NPU gait=NPU scene=NPU"
        )

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
        text = "".join(ALPHABET[index] for index in indices[keep])
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
        self.next_track_id = 1

    def start(self):
        self.thread.start()
        return self

    def submit(self, result: DetectionResult) -> None:
        try:
            self.queue.put_nowait(result)
        except queue.Full:
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
        frame_index = self.frame_indices.get(result.camera_id, 0) + 1
        self.frame_indices[result.camera_id] = frame_index
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
        updated = {}
        people = []
        for detection in (item for item in result.detections if item["label"] == "person"):
            best = max(previous, key=lambda key: self._iou(detection["bbox"], previous[key]), default=None)
            if best is None or self._iou(detection["bbox"], previous[best]) < 0.25 or best in updated:
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
        while not self.stop_event.is_set() or not self.queue.empty():
            try:
                result = self.queue.get(timeout=0.25)
            except queue.Empty:
                continue
            try:
                self.consume(self._process(result))
            except Exception:
                log.exception("secondary inference failed camera=%s", result.camera_id)

    def close(self) -> None:
        self.stop_event.set()
        self.thread.join(timeout=10)


class DiscardSink:
    """Explicit terminal sink for generated inference results."""

    def __init__(self):
        self.generated = 0

    def consume(self, _payload: dict) -> None:
        self.generated += 1
