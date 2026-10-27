"""Centralized direct-OpenVINO primary inference scheduler."""
from __future__ import annotations

import argparse
from collections import deque
import json
import logging
import os
from pathlib import Path
import signal
import threading
import time

import cv2
import numpy as np
import openvino as ov
from openvino.preprocess import ColorFormat, PrePostProcessor

from .pipeline import DetectionResult
from .secondary import SecondaryPipeline
from .shared_frames import SharedFrameReader


log = logging.getLogger(__name__)
PRIMARY_DETECTION_LABELS = {
    0: "bus",
    1: "car",
    2: "minivan",
    3: "motorcycle",
    4: "rickshaw",
    5: "truck",
    6: "bicycle",
    7: "person",
    8: "others",
}
SMOKE_DETECTION_LABELS = {0: "fire", 1: "smoke"}


def compile_image_model(core, path: Path, width: int, height: int,
                        nireq: int, performance_hint: str,
                        pixel_format: str = "BGR",
                        model_color_format: str = "RGB"):
    model = core.read_model(path)
    model.reshape({model.input(0): [1, 3, height, width]})
    preprocessor = PrePostProcessor(model)
    preprocessor.input().tensor().set_element_type(ov.Type.u8)
    preprocessor.input().tensor().set_layout(ov.Layout("NHWC"))
    target_color = {
        "BGR": ColorFormat.BGR,
        "RGB": ColorFormat.RGB,
    }.get(model_color_format.upper())
    if target_color is None:
        raise ValueError(f"unsupported model color format: {model_color_format}")
    if pixel_format.upper() == "NV12":
        preprocessor.input().tensor().set_color_format(ColorFormat.NV12_SINGLE_PLANE)
        preprocessor.input().tensor().set_spatial_static_shape(height, width)
        preprocessor.input().preprocess().convert_color(target_color)
    else:
        preprocessor.input().tensor().set_color_format(ColorFormat.BGR)
        if target_color == ColorFormat.RGB:
            preprocessor.input().preprocess().convert_color(ColorFormat.RGB)
    preprocessor.input().preprocess().convert_element_type(ov.Type.f32)
    preprocessor.input().preprocess().scale(255.0)
    preprocessor.input().model().set_layout(ov.Layout("NCHW"))
    return core.compile_model(preprocessor.build(), "GPU", {
        "PERFORMANCE_HINT": performance_hint,
        "PERFORMANCE_HINT_NUM_REQUESTS": str(nireq),
    })


class DirectInference:
    def __init__(self, config_path: Path):
        config = json.loads(config_path.read_text(encoding="utf-8"))
        self.settings = config["settings"]
        self.smoke_settings = config["smoke_fire_gpu"]
        self.direct = config.get("direct_openvino", {})
        self.secondary_settings = dict(config.get("secondary", {}))
        self.secondary_enabled = bool(self.secondary_settings.get("enabled", False))
        for key in (
            "plate_model", "ocr_model", "face_detector_model",
            "face_embedding_model", "body_reid_model", "gait_model",
            "scene_model",
        ):
            value = self.secondary_settings.get(key)
            if value:
                self.secondary_settings[key] = str((config_path.parent / value).resolve())
        self.cameras = [
            str(camera["id"]) for camera in config["cameras"]
            if camera.get("enabled", True)
        ]
        self.width = int(self.settings.get("input_width", 640))
        self.height = int(self.settings.get("input_height", 640))
        self.pixel_format = str(self.settings.get("direct_input_format", "BGR"))
        self.model_color_format = str(
            self.settings.get("model_color_format", "RGB")
        )
        model_path = (config_path.parent / self.settings["model"]).resolve()
        self.readers = {
            camera_id: SharedFrameReader(
                camera_id, self.width, self.height, pixel_format=self.pixel_format
            )
            for camera_id in self.cameras
        }
        self.smoke_enabled = str(
            self.smoke_settings.get("backend", "dlstreamer")
        ).lower() == "openvino"
        self.smoke_width = int(self.smoke_settings.get("input_width", 320))
        self.smoke_height = int(self.smoke_settings.get("input_height", 320))
        self.smoke_pixel_format = str(
            self.smoke_settings.get("direct_input_format", "BGR")
        )
        self.smoke_model_color_format = str(
            self.smoke_settings.get("model_color_format", "RGB")
        )
        self.smoke_readers = {
            camera_id: SharedFrameReader(
                camera_id, self.smoke_width, self.smoke_height, stream="smoke",
                pixel_format=self.smoke_pixel_format,
            )
            for camera_id in self.cameras
        } if self.smoke_enabled else {}
        self.stop_event = threading.Event()
        self.metrics_path = Path(f"/run/user/{os.getuid()}/sentry-cv-primary.json")
        self.samples: dict[str, deque[float]] = {
            camera_id: deque() for camera_id in self.cameras
        }
        self.smoke_samples: dict[str, deque[float]] = {
            camera_id: deque() for camera_id in self.cameras
        }
        self.smoke_detection_samples: dict[str, deque[float]] = {
            camera_id: deque() for camera_id in self.cameras
        }
        self.smoke_label_samples = {
            label: deque() for label in SMOKE_DETECTION_LABELS.values()
        }
        self.total_results = 0
        self.total_smoke_inferences = 0
        self.total_smoke_detections = 0
        self.total_smoke_labels = {
            label: 0 for label in SMOKE_DETECTION_LABELS.values()
        }
        self.total_detections = 0
        self.total_primary_labels = {
            label: 0 for label in PRIMARY_DETECTION_LABELS.values()
        }
        self.primary_label_samples = {
            label: deque() for label in PRIMARY_DETECTION_LABELS.values()
        }
        self.latencies_ms: deque[float] = deque(maxlen=1000)
        self.secondary_input_fps = float(
            self.secondary_settings.get("input_fps", 2.0)
        )
        self.secondary_last_submit = {camera_id: 0.0 for camera_id in self.cameras}
        self.secondary_totals = {
            "frames": 0,
            "plates": 0,
            "anpr": 0,
            "faces": 0,
            "body_reid": 0,
            "gait": 0,
            "scene": 0,
        }
        self.secondary_samples = {
            name: deque() for name in self.secondary_totals
        }
        self.metrics_lock = threading.Lock()
        self.started_at = time.time()

        core = ov.Core()
        self.nireq = int(self.direct.get("nireq", 1))
        performance_hint = str(self.direct.get("performance_hint", "THROUGHPUT"))
        self.compiled = compile_image_model(
            core, model_path, self.width, self.height, self.nireq, performance_hint,
            self.pixel_format, self.model_color_format,
        )
        self.queue = ov.AsyncInferQueue(self.compiled, self.nireq)
        self.queue.set_callback(self._on_complete)
        self.input_port = self.compiled.input(0)
        self.output_port = self.compiled.output(0)
        log.info(
            "central OpenVINO compiled model=%s shape=%s color=%s cameras=%d requests=%s",
            model_path, self.input_port.shape, self.model_color_format,
            len(self.cameras),
            self.nireq,
        )
        if self.smoke_enabled:
            smoke_path = (config_path.parent / self.smoke_settings["model"]).resolve()
            self.smoke_nireq = int(self.direct.get("smoke_nireq", 1))
            self.smoke_compiled = compile_image_model(
                core, smoke_path, self.smoke_width, self.smoke_height,
                self.smoke_nireq, performance_hint, self.smoke_pixel_format,
                self.smoke_model_color_format,
            )
            self.smoke_queue = ov.AsyncInferQueue(
                self.smoke_compiled, self.smoke_nireq
            )
            self.smoke_queue.set_callback(self._on_smoke_complete)
            self.smoke_input_port = self.smoke_compiled.input(0)
            log.info(
                "central smoke OpenVINO compiled model=%s shape=%s color=%s requests=%d",
                smoke_path, self.smoke_input_port.shape,
                self.smoke_model_color_format, self.smoke_nireq,
            )
        self.secondary = None
        if self.secondary_enabled:
            self.secondary = SecondaryPipeline(
                self.secondary_settings, self._consume_secondary
            ).start()
            log.info(
                "central secondary enabled input=%.2f FPS/camera; transient outputs are discarded",
                self.secondary_input_fps,
            )

    def request_stop(self, *_args) -> None:
        self.stop_event.set()

    def _postprocess(self, output: np.ndarray) -> list[dict]:
        rows = np.asarray(output).reshape(-1, 6)
        confidence = float(self.settings.get("confidence", 0.3))
        detections = []
        for row in rows:
            score = float(row[4])
            class_id = int(round(float(row[5])))
            label = PRIMARY_DETECTION_LABELS.get(class_id)
            if score < confidence or label is None:
                continue
            x1 = max(0, min(self.width, int(round(float(row[0])))))
            y1 = max(0, min(self.height, int(round(float(row[1])))))
            x2 = max(0, min(self.width, int(round(float(row[2])))))
            y2 = max(0, min(self.height, int(round(float(row[3])))))
            if x2 <= x1 or y2 <= y1:
                continue
            detections.append({
                "label": label,
                "class_id": class_id,
                "confidence": score,
                "bbox": [x1, y1, x2, y2],
            })
        return detections

    def _postprocess_smoke(self, output: np.ndarray) -> list[dict]:
        prediction = np.asarray(output)
        if prediction.ndim != 3 or prediction.shape[0] != 1:
            raise ValueError(f"unexpected smoke/fire output shape: {prediction.shape}")
        rows = prediction[0].T if prediction.shape[1] < prediction.shape[2] else prediction[0]
        if rows.shape[1] != 4 + len(SMOKE_DETECTION_LABELS):
            raise ValueError(f"unexpected smoke/fire row shape: {rows.shape}")
        threshold = float(self.smoke_settings.get("confidence", 0.35))
        iou_threshold = float(self.smoke_settings.get("nms_iou_threshold", 0.7))
        class_scores = rows[:, 4:]
        class_ids = np.argmax(class_scores, axis=1)
        scores = class_scores[np.arange(len(rows)), class_ids]
        selected_rows = rows[scores >= threshold]
        selected_scores = scores[scores >= threshold]
        selected_classes = class_ids[scores >= threshold]
        boxes = np.empty((len(selected_rows), 4), dtype=np.float32)
        boxes[:, 0] = selected_rows[:, 0] - selected_rows[:, 2] / 2.0
        boxes[:, 1] = selected_rows[:, 1] - selected_rows[:, 3] / 2.0
        boxes[:, 2] = selected_rows[:, 0] + selected_rows[:, 2] / 2.0
        boxes[:, 3] = selected_rows[:, 1] + selected_rows[:, 3] / 2.0
        boxes[:, [0, 2]] = np.clip(boxes[:, [0, 2]], 0, self.smoke_width)
        boxes[:, [1, 3]] = np.clip(boxes[:, [1, 3]], 0, self.smoke_height)
        candidates = []
        for box, score, class_id in zip(boxes, selected_scores, selected_classes):
            x1, y1, x2, y2 = map(float, box)
            if x2 <= x1 or y2 <= y1:
                continue
            class_id = int(class_id)
            candidates.append({
                "label": SMOKE_DETECTION_LABELS[class_id],
                "class_id": class_id,
                "confidence": float(score),
                "bbox": [int(round(x1)), int(round(y1)), int(round(x2)), int(round(y2))],
            })
        detections = []
        for class_id in SMOKE_DETECTION_LABELS:
            class_candidates = [
                item for item in candidates if item["class_id"] == class_id
            ]
            if not class_candidates:
                continue
            boxes = [
                [
                    item["bbox"][0], item["bbox"][1],
                    item["bbox"][2] - item["bbox"][0],
                    item["bbox"][3] - item["bbox"][1],
                ]
                for item in class_candidates
            ]
            scores = [item["confidence"] for item in class_candidates]
            selected = cv2.dnn.NMSBoxes(boxes, scores, threshold, iou_threshold)
            detections.extend(class_candidates[int(index)] for index in selected)
        return detections

    def _secondary_due(self, camera_id: str, capture_time: float) -> bool:
        if self.secondary is None or self.secondary_input_fps <= 0:
            return False
        interval = 1.0 / self.secondary_input_fps
        with self.metrics_lock:
            previous = self.secondary_last_submit[camera_id]
            if capture_time <= previous or capture_time - previous < interval:
                return False
            self.secondary_last_submit[camera_id] = capture_time
        return True

    def _consume_secondary(self, payload: dict) -> None:
        """Count transient products and immediately release the payload contents."""
        now = time.time()
        counts = {
            "frames": 1,
            "plates": len(payload.get("plates", ())),
            "anpr": sum(1 for item in payload.get("plates", ()) if item.get("text")),
            "faces": len(payload.get("faces", ())),
            "body_reid": len(payload.get("body_reid", ())),
            "gait": len(payload.get("gait", ())),
            "scene": 1 if payload.get("scene") is not None else 0,
        }
        with self.metrics_lock:
            for name, count in counts.items():
                self.secondary_totals[name] += count
                self.secondary_samples[name].extend([now] * count)

    def _on_complete(self, request, userdata) -> None:
        camera_id, capture_time, started, frame = userdata
        latency_ms = (time.perf_counter() - started) * 1000.0
        detections = self._postprocess(request.get_output_tensor().data)
        if self._secondary_due(camera_id, capture_time):
            if self.pixel_format.upper() == "NV12":
                bgr = cv2.cvtColor(
                    frame.reshape(self.height * 3 // 2, self.width),
                    cv2.COLOR_YUV2BGR_NV12,
                )
            else:
                bgr = frame.reshape(self.height, self.width, 3)
            self.secondary.submit(DetectionResult(
                camera_id=camera_id,
                timestamp=capture_time,
                width=self.width,
                height=self.height,
                detections=detections,
                frame=bgr,
            ))
        del frame  # keep the input allocation alive until inference completes
        now = time.time()
        with self.metrics_lock:
            self.total_results += 1
            self.total_detections += len(detections)
            for detection in detections:
                label = detection["label"]
                self.total_primary_labels[label] += 1
                self.primary_label_samples[label].append(now)
            self.latencies_ms.append(latency_ms)
            self.samples[camera_id].append(now)

    def _on_smoke_complete(self, request, userdata) -> None:
        camera_id, _started, frame = userdata
        detections = self._postprocess_smoke(request.get_output_tensor().data)
        del frame
        now = time.time()
        with self.metrics_lock:
            self.total_smoke_inferences += 1
            self.total_smoke_detections += len(detections)
            self.smoke_samples[camera_id].append(now)
            self.smoke_detection_samples[camera_id].extend([now] * len(detections))
            for detection in detections:
                label = detection["label"]
                self.total_smoke_labels[label] += 1
                self.smoke_label_samples[label].append(now)

    def _write_metrics(self, now: float) -> None:
        cutoff = now - 10.0
        cameras = {}
        aggregate = 0.0
        smoke_aggregate = 0.0
        smoke_detection_aggregate = 0.0
        with self.metrics_lock:
            for camera_id, samples in self.samples.items():
                while samples and samples[0] < cutoff:
                    samples.popleft()
                rate = len(samples) / 10.0
                aggregate += rate
                cameras[camera_id] = {
                    "primary_results_per_second": round(rate, 2),
                }
                smoke_samples = self.smoke_samples[camera_id]
                while smoke_samples and smoke_samples[0] < cutoff:
                    smoke_samples.popleft()
                smoke_rate = len(smoke_samples) / 10.0
                smoke_aggregate += smoke_rate
                cameras[camera_id]["smoke_fire_inferences_per_second"] = round(
                    smoke_rate, 2
                )
                detection_samples = self.smoke_detection_samples[camera_id]
                while detection_samples and detection_samples[0] < cutoff:
                    detection_samples.popleft()
                detection_rate = len(detection_samples) / 10.0
                smoke_detection_aggregate += detection_rate
                cameras[camera_id]["smoke_fire_detections_per_second"] = round(
                    detection_rate, 2
                )
            latency = list(self.latencies_ms)
            total_results = self.total_results
            total_detections = self.total_detections
            total_primary_labels = dict(self.total_primary_labels)
            primary_label_rates = {}
            for label, samples in self.primary_label_samples.items():
                while samples and samples[0] < cutoff:
                    samples.popleft()
                primary_label_rates[label] = round(len(samples) / 10.0, 2)
            total_smoke_inferences = self.total_smoke_inferences
            total_smoke_detections = self.total_smoke_detections
            total_smoke_labels = dict(self.total_smoke_labels)
            smoke_label_rates = {}
            for label, samples in self.smoke_label_samples.items():
                while samples and samples[0] < cutoff:
                    samples.popleft()
                smoke_label_rates[f"{label}_detections_per_second"] = round(
                    len(samples) / 10.0, 2
                )
            secondary_rates = {}
            for name, samples in self.secondary_samples.items():
                while samples and samples[0] < cutoff:
                    samples.popleft()
                secondary_rates[f"{name}_per_second"] = round(len(samples) / 10.0, 2)
            secondary_totals = dict(self.secondary_totals)
        secondary_metrics = {
            "enabled": self.secondary is not None,
            "input_fps_per_camera": self.secondary_input_fps,
            **secondary_totals,
            **secondary_rates,
        }
        if self.secondary is not None:
            secondary_metrics.update(self.secondary.snapshot())
        payload = {
            "primary_results": total_results,
            "primary_results_per_second": round(aggregate, 2),
            "primary_detections": total_detections,
            "primary_detections_by_class": total_primary_labels,
            "primary_detections_per_second_by_class": primary_label_rates,
            "smoke_fire_inferences": total_smoke_inferences,
            "smoke_fire_inferences_per_second": round(smoke_aggregate, 2),
            "smoke_fire_detections": total_smoke_detections,
            "smoke_fire_detections_per_second": round(smoke_detection_aggregate, 2),
            "fire_detections": total_smoke_labels["fire"],
            "smoke_detections": total_smoke_labels["smoke"],
            **smoke_label_rates,
            "average_inference_latency_ms": round(float(np.mean(latency)), 3) if latency else 0.0,
            "p95_inference_latency_ms": round(float(np.percentile(latency, 95)), 3) if latency else 0.0,
            "uptime_seconds": round(now - self.started_at, 1),
            "updated_at": now,
            "cameras": cameras,
            "secondary": secondary_metrics,
        }
        temporary = self.metrics_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload), encoding="utf-8")
        temporary.replace(self.metrics_path)

    def run(self) -> int:
        cursor = 0
        smoke_cursor = 0
        last_metrics = 0.0
        last_log = 0.0
        while not self.stop_event.is_set():
            selected = None
            for offset in range(len(self.cameras)):
                index = (cursor + offset) % len(self.cameras)
                camera_id = self.cameras[index]
                snapshot = self.readers[camera_id].read_latest()
                if snapshot is not None:
                    selected = (index, camera_id, snapshot)
                    break
            if selected is None:
                now = time.time()
                if now - last_metrics >= 1.0:
                    self._write_metrics(now)
                    last_metrics = now
                self.stop_event.wait(0.001)
                continue

            index, camera_id, (_sequence, capture_time, frame) = selected
            cursor = (index + 1) % len(self.cameras)
            started = time.perf_counter()
            self.queue.start_async(
                {self.input_port: frame[np.newaxis, ...]},
                userdata=(camera_id, capture_time, started, frame),
            )
            if self.smoke_enabled:
                for offset in range(len(self.cameras)):
                    smoke_index = (smoke_cursor + offset) % len(self.cameras)
                    smoke_camera = self.cameras[smoke_index]
                    smoke_snapshot = self.smoke_readers[smoke_camera].read_latest()
                    if smoke_snapshot is None:
                        continue
                    _, _smoke_capture_time, smoke_frame = smoke_snapshot
                    smoke_cursor = (smoke_index + 1) % len(self.cameras)
                    self.smoke_queue.start_async(
                        {self.smoke_input_port: smoke_frame[np.newaxis, ...]},
                        userdata=(smoke_camera, time.perf_counter(), smoke_frame),
                    )
                    break
            now = time.time()
            if now - last_metrics >= 1.0:
                self._write_metrics(now)
                last_metrics = now
            if now - last_log >= 10.0:
                with self.metrics_lock:
                    rate = sum(len(samples) for samples in self.samples.values()) / 10.0
                    latency = list(self.latencies_ms)
                log.info(
                    "central primary rate=%.1f FPS avg-latency=%.2f ms p95=%.2f ms",
                    rate, float(np.mean(latency)) if latency else 0.0,
                    float(np.percentile(latency, 95)) if latency else 0.0,
                )
                last_log = now
        self.queue.wait_all()
        if self.smoke_enabled:
            self.smoke_queue.wait_all()
        if self.secondary is not None:
            self.secondary.close()
        self._write_metrics(time.time())
        for reader in self.readers.values():
            reader.close()
        for reader in self.smoke_readers.values():
            reader.close()
        return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Central direct OpenVINO inference")
    parser.add_argument("--config", default="edge_app/config.json")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    runtime = DirectInference(Path(args.config).resolve())
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, runtime.request_stop)
    return runtime.run()


if __name__ == "__main__":
    raise SystemExit(main())
