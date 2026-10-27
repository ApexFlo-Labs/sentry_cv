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
DETECTION_LABELS = {
    0: "person",
    2: "car",
    3: "motorcycle",
    5: "bus",
    7: "truck",
}


def compile_image_model(core, path: Path, width: int, height: int,
                        nireq: int, performance_hint: str,
                        pixel_format: str = "BGR"):
    model = core.read_model(path)
    model.reshape({model.input(0): [1, 3, height, width]})
    preprocessor = PrePostProcessor(model)
    preprocessor.input().tensor().set_element_type(ov.Type.u8)
    preprocessor.input().tensor().set_layout(ov.Layout("NHWC"))
    if pixel_format.upper() == "NV12":
        preprocessor.input().tensor().set_color_format(ColorFormat.NV12_SINGLE_PLANE)
        preprocessor.input().tensor().set_spatial_static_shape(height, width)
        preprocessor.input().preprocess().convert_color(ColorFormat.BGR)
    else:
        preprocessor.input().tensor().set_color_format(ColorFormat.BGR)
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
        self.total_results = 0
        self.total_smoke_results = 0
        self.total_detections = 0
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
            self.pixel_format,
        )
        self.queue = ov.AsyncInferQueue(self.compiled, self.nireq)
        self.queue.set_callback(self._on_complete)
        self.input_port = self.compiled.input(0)
        self.output_port = self.compiled.output(0)
        log.info(
            "central OpenVINO compiled model=%s shape=%s cameras=%d requests=%s",
            model_path, self.input_port.shape, len(self.cameras),
            self.nireq,
        )
        if self.smoke_enabled:
            smoke_path = (config_path.parent / self.smoke_settings["model"]).resolve()
            self.smoke_nireq = int(self.direct.get("smoke_nireq", 1))
            self.smoke_compiled = compile_image_model(
                core, smoke_path, self.smoke_width, self.smoke_height,
                self.smoke_nireq, performance_hint, self.smoke_pixel_format,
            )
            self.smoke_queue = ov.AsyncInferQueue(
                self.smoke_compiled, self.smoke_nireq
            )
            self.smoke_queue.set_callback(self._on_smoke_complete)
            self.smoke_input_port = self.smoke_compiled.input(0)
            log.info(
                "central smoke OpenVINO compiled model=%s shape=%s requests=%d",
                smoke_path, self.smoke_input_port.shape, self.smoke_nireq,
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
            label = DETECTION_LABELS.get(class_id)
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
            self.latencies_ms.append(latency_ms)
            self.samples[camera_id].append(now)

    def _on_smoke_complete(self, _request, userdata) -> None:
        camera_id, _started, frame = userdata
        del frame
        now = time.time()
        with self.metrics_lock:
            self.total_smoke_results += 1
            self.smoke_samples[camera_id].append(now)

    def _write_metrics(self, now: float) -> None:
        cutoff = now - 10.0
        cameras = {}
        aggregate = 0.0
        smoke_aggregate = 0.0
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
                cameras[camera_id]["smoke_fire_results_per_second"] = round(
                    smoke_rate, 2
                )
            latency = list(self.latencies_ms)
            total_results = self.total_results
            total_detections = self.total_detections
            total_smoke_results = self.total_smoke_results
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
            "smoke_fire_results": total_smoke_results,
            "smoke_fire_results_per_second": round(smoke_aggregate, 2),
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
