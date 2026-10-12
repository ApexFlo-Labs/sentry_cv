"""RTSP -> VA-API decode -> GPU preprocessing -> shared OpenVINO inference."""
from __future__ import annotations

from dataclasses import dataclass
import logging
import os
from pathlib import Path
import time
from typing import Callable

import numpy as np

log = logging.getLogger(__name__)


class PipelineUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class DetectionResult:
    camera_id: str
    timestamp: float
    width: int
    height: int
    detections: list[dict]
    frame: np.ndarray | None = None


def _quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def pipeline_description(camera: dict, settings: dict) -> str:
    """Build two zero-copy GPU inference branches from one decoded VA surface."""
    uri = str(camera["uri"])
    source = (
        f"urisourcebin name=source uri={_quote(uri)} buffer-size=4096"
        if uri.lower().startswith(("rtsp://", "rtsps://"))
        else f"filesrc location={_quote(uri)}"
    )
    smoke = settings["smoke_fire"]
    return (
        f"{source} ! decodebin3 ! vapostproc "
        "! video/x-raw(memory:VAMemory),format=NV12 "
        "! tee name=decoded "
        "decoded. ! queue max-size-buffers=2 leaky=downstream "
        f"! videorate drop-only=true ! video/x-raw(memory:VAMemory),framerate={int(camera.get('fps', settings['fps']))}/1 "
        f"! gvadetect name=detector model={_quote(str(Path(settings['model']).resolve()))} "
        f"model-proc={_quote(str(Path(settings['model_proc']).resolve()))} device=GPU "
        "pre-process-backend=va-surface-sharing batch-size=1 "
        f"nireq={int(settings.get('nireq', 6))} threshold={float(settings.get('confidence', .3)):.6g} "
        f"model-instance-id={_quote(str(settings.get('model_instance_id', 'vehicle-primary')))} "
        "scheduling-policy=latency "
        "! queue max-size-buffers=2 leaky=downstream "
        "! vapostproc ! video/x-raw,format=BGR "
        "! appsink name=results emit-signals=true max-buffers=1 drop=true sync=false "
        "decoded. ! queue max-size-buffers=2 leaky=downstream "
        f"! videorate drop-only=true ! video/x-raw(memory:VAMemory),framerate={int(smoke.get('fps', 1))}/1 "
        f"! gvadetect name=smoke_detector model={_quote(str(Path(smoke['model']).resolve()))} "
        f"model-proc={_quote(str(Path(smoke['model_proc']).resolve()))} device=GPU "
        "pre-process-backend=va-surface-sharing batch-size=1 "
        f"nireq={int(smoke.get('nireq', 2))} threshold={float(smoke.get('confidence', .35)):.6g} "
        f"model-instance-id={_quote(str(smoke.get('model_instance_id', 'smoke-fire-primary')))} "
        "scheduling-policy=latency "
        "! queue max-size-buffers=1 leaky=downstream "
        "! appsink name=smoke_results emit-signals=true max-buffers=1 drop=true sync=false"
    )


class CameraPipeline:
    """One camera pipeline; all instances share one OpenVINO model request pool."""

    LABEL_IDS = {"person": 0, "pedestrian": 0, "car": 2, "motorcycle": 3, "bus": 5, "truck": 7}

    def __init__(self, camera: dict, settings: dict, publish: Callable[[DetectionResult], None],
                 publish_smoke: Callable[[dict], None]):
        self.camera = camera
        self.settings = settings
        self.publish = publish
        self.publish_smoke = publish_smoke
        self.pipeline = None
        os.environ.setdefault(
            "GST_PLUGIN_FEATURE_RANK",
            "vah264dec:MAX,vah265dec:MAX,avdec_h264:NONE,avdec_h265:NONE",
        )
        self.Gst, self.VideoFrame = self._load_runtime()
        self._validate()

    @staticmethod
    def _load_runtime():
        try:
            import gi
            gi.require_version("Gst", "1.0")
            from gi.repository import Gst
            from gstgva import VideoFrame
        except (ImportError, ValueError) as exc:
            raise PipelineUnavailable("install GStreamer, DL Streamer and gstgva Python bindings") from exc
        Gst.init([])
        return Gst, VideoFrame

    def _validate(self) -> None:
        missing = [
            name for name in ("urisourcebin", "decodebin3", "vapostproc", "videorate", "gvadetect", "appsink")
            if self.Gst.ElementFactory.find(name) is None
        ]
        if missing:
            raise PipelineUnavailable("missing GStreamer elements: " + ", ".join(missing))
        for key in ("model", "model_proc"):
            if not Path(self.settings[key]).is_file():
                raise PipelineUnavailable(f"missing {key}: {self.settings[key]}")
        for key in ("model", "model_proc"):
            path = self.settings["smoke_fire"][key]
            if not Path(path).is_file():
                raise PipelineUnavailable(f"missing smoke_fire.{key}: {path}")

    def _on_sample(self, sink):
        sample = sink.emit("pull-sample")
        if sample is None:
            return self.Gst.FlowReturn.FLUSHING
        caps = sample.get_caps()
        structure = caps.get_structure(0)
        width, height = int(structure.get_value("width")), int(structure.get_value("height"))
        buffer = sample.get_buffer()
        video_frame = self.VideoFrame(buffer, caps=caps)
        detections = []
        for region in video_frame.regions():
            label = str(region.label() or "").lower()
            if label not in self.LABEL_IDS:
                continue
            rect = region.rect()
            detections.append({
                "class_id": self.LABEL_IDS[label],
                "label": "person" if label == "pedestrian" else label,
                "confidence": round(float(region.confidence()), 6),
                "bbox": [int(rect.x), int(rect.y), int(rect.x + rect.w), int(rect.y + rect.h)],
            })
        ok, mapped = buffer.map(self.Gst.MapFlags.READ)
        if not ok:
            return self.Gst.FlowReturn.ERROR
        try:
            stride = len(mapped.data) // height
            pixels = np.frombuffer(mapped.data, dtype=np.uint8).reshape(height, stride)
            bgr = pixels[:, :width * 3].reshape(height, width, 3).copy()
        finally:
            buffer.unmap(mapped)
        self.publish(DetectionResult(
            camera_id=str(self.camera["id"]), timestamp=time.time(), width=width,
            height=height, detections=detections, frame=bgr,
        ))
        return self.Gst.FlowReturn.OK

    def _on_smoke_sample(self, sink):
        """Read smoke/fire ROI metadata without mapping or downloading the frame."""
        sample = sink.emit("pull-sample")
        if sample is None:
            return self.Gst.FlowReturn.FLUSHING
        frame = self.VideoFrame(sample.get_buffer(), caps=sample.get_caps())
        detections = []
        for region in frame.regions():
            label = str(region.label() or "").lower()
            if label not in {"fire", "smoke"}:
                continue
            rect = region.rect()
            detections.append({
                "label": label, "confidence": round(float(region.confidence()), 6),
                "bbox": [int(rect.x), int(rect.y), int(rect.x + rect.w), int(rect.y + rect.h)],
            })
        self.publish_smoke({
            "schema_version": "1.0", "camera_id": str(self.camera["id"]),
            "timestamp": time.time(), "smoke_fire": detections,
        })
        return self.Gst.FlowReturn.OK

    def _on_source_setup(self, _bin, source) -> None:
        """Apply RTSP latency when the URI source exposes those properties."""
        latency = int(self.camera.get("latency_ms", self.settings.get("rtsp_latency_ms", 200)))
        if source.find_property("latency") is not None:
            source.set_property("latency", latency)
        if source.find_property("drop-on-latency") is not None:
            source.set_property("drop-on-latency", True)

    def run(self, stop_event) -> None:
        backoff = 1.0
        while not stop_event.is_set():
            try:
                self.pipeline = self.Gst.parse_launch(pipeline_description(self.camera, self.settings))
                source = self.pipeline.get_by_name("source")
                if source is not None:
                    source.connect("source-setup", self._on_source_setup)
                self.pipeline.get_by_name("results").connect("new-sample", self._on_sample)
                self.pipeline.get_by_name("smoke_results").connect("new-sample", self._on_smoke_sample)
                if self.pipeline.set_state(self.Gst.State.PLAYING) == self.Gst.StateChangeReturn.FAILURE:
                    raise PipelineUnavailable("pipeline failed to enter PLAYING")
                log.info("camera=%s connected (shared vehicle GPU=%s FPS, smoke/fire GPU=%s FPS)",
                         self.camera["id"], self.camera.get("fps", self.settings["fps"]),
                         self.settings["smoke_fire"].get("fps", 1))
                bus = self.pipeline.get_bus()
                while not stop_event.wait(0.25):
                    message = bus.timed_pop_filtered(0, self.Gst.MessageType.ERROR | self.Gst.MessageType.EOS)
                    if message is not None:
                        if message.type == self.Gst.MessageType.ERROR:
                            error, debug = message.parse_error()
                            raise RuntimeError(f"{error.message}; {debug or ''}")
                        raise RuntimeError("end of stream")
                backoff = 1.0
            except Exception as exc:
                log.warning("camera=%s disconnected: %s", self.camera["id"], exc)
                stop_event.wait(backoff)
                backoff = min(backoff * 2.0, 30.0)
            finally:
                if self.pipeline is not None:
                    self.pipeline.set_state(self.Gst.State.NULL)
                    self.pipeline = None
