"""Grouped RTSP ingest with shared, batched DL Streamer inference."""
from __future__ import annotations

from dataclasses import dataclass
import json
import logging
import os
from pathlib import Path
import threading
import time
from typing import Callable

import numpy as np

log = logging.getLogger(__name__)


class PipelineUnavailable(RuntimeError):
    pass


class PipelineFatal(RuntimeError):
    """The worker inference graph is unsafe to reuse in this process."""


@dataclass(frozen=True)
class DetectionResult:
    camera_id: str
    timestamp: float
    width: int
    height: int
    detections: list[dict]
    frame: np.ndarray | None = None
    pixel_format: str = "BGR"


def _quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _source_description(camera: dict, index: int) -> str:
    uri = str(camera["uri"])
    if uri.lower().startswith(("rtsp://", "rtsps://")):
        return f"urisourcebin name=source_{index} uri={_quote(uri)} buffer-size=4096"
    return f"filesrc name=source_{index} location={_quote(uri)}"


def _detect_description(name: str, settings: dict, batch_size: int) -> str:
    device = str(settings.get("device", "GPU")).upper()
    return (
        f"gvadetect name={name} model={_quote(str(Path(settings['model']).resolve()))} "
        f"model-proc={_quote(str(Path(settings['model_proc']).resolve()))} "
        f"device={_quote(device)} "
        f"pre-process-backend={_quote(str(settings.get('pre_process_backend', 'va')))} "
        f"batch-size={batch_size} nireq={int(settings.get('nireq', 2))} "
        f"threshold={float(settings.get('confidence', .3)):.6g} "
        f"{('ie-config=' + _quote(str(settings['ie_config'])) + ' ') if settings.get('ie_config') else ''}"
        f"model-instance-id={_quote(str(settings['model_instance_id']))} "
        f"scheduling-policy={_quote(str(settings.get('scheduling_policy', 'throughput')))}"
    )


def _mux_input_description(settings: dict, width: int, height: int, fps: int) -> str:
    """Normalize every mux input to identical caps and memory type."""
    backend = str(settings.get("pre_process_backend", "va")).lower()
    if backend in {"ie", "opencv"}:
        canonical_caps = (
            f"video/x-raw,format=BGR,width={width},height={height},framerate={fps}/1"
        )
        return (
            "vapostproc add-borders=true scale-method=fast "
            f"! video/x-raw,format=BGRx,width={width},height={height},framerate={fps}/1 "
            "! videoconvert "
            f"! {canonical_caps} "
            f"! capssetter replace=true join=false caps={_quote(canonical_caps)}"
        )
    canonical_caps = (
        "video/x-raw(memory:VAMemory),format=NV12,"
        f"width={width},height={height},framerate={fps}/1"
    )
    return (
        "vapostproc add-borders=true scale-method=fast "
        f"! {canonical_caps} "
        f"! capssetter replace=true join=false caps={_quote(canonical_caps)}"
    )


def _direct_input_description(settings: dict, width: int, height: int, fps: int) -> str:
    pixel_format = str(settings.get("direct_input_format", "BGR")).upper()
    if pixel_format == "NV12":
        canonical_caps = (
            f"video/x-raw,format=NV12,width={width},height={height},framerate={fps}/1"
        )
        return (
            "vapostproc add-borders=true scale-method=fast "
            f"! {canonical_caps} "
            f"! capssetter replace=true join=false caps={_quote(canonical_caps)}"
        )
    return _mux_input_description(settings, width, height, fps)


def grouped_pipeline_description(cameras: list[dict], settings: dict) -> str:
    """Build one worker graph with one decode per camera and two shared models."""
    if not cameras:
        raise ValueError("a grouped pipeline requires at least one camera")

    primary_batch = int(settings.get("batch_size", 4))
    if primary_batch < 1:
        raise ValueError("settings.batch_size must be at least 1")
    smoke = settings["smoke_fire"]
    primary_width = int(settings.get("input_width", 640))
    primary_height = int(settings.get("input_height", 640))
    smoke_width = int(smoke.get("input_width", 320))
    smoke_height = int(smoke.get("input_height", 320))
    if min(primary_width, primary_height, smoke_width, smoke_height) < 16:
        raise ValueError("inference dimensions must be at least 16x16")

    runtime = settings.get("mux", {})
    sync_mode = str(runtime.get("sync_mode", "pipeline"))
    max_wait = int(runtime.get("max_wait_time_ns", 40_000_000))
    pts_tolerance = int(runtime.get("pts_tolerance_ns", 20_000_000))
    max_queue = int(runtime.get("max_queue_size", 2))
    mux_properties = (
        f"sync-mode={sync_mode} max-wait-time={max_wait} "
        f"pts-tolerance={pts_tolerance} max-queue-size={max_queue}"
    )

    direct_primary = str(settings.get("primary_backend", "dlstreamer")).lower() == "openvino"
    direct_smoke = str(smoke.get("backend", "dlstreamer")).lower() == "openvino"
    parts = []
    if not direct_smoke:
        parts.append(
            f"gvastreammux name=smoke_mux {mux_properties} "
            "! queue max-size-buffers=4 leaky=downstream "
            f"! {_detect_description('smoke_detector', smoke, 1)} "
            "! queue max-size-buffers=4 leaky=downstream "
            "! gvastreamdemux name=smoke_demux"
        )
    if not direct_primary:
        parts.insert(
            0,
            f"gvastreammux name=primary_mux {mux_properties} "
            "! queue max-size-buffers=8 leaky=downstream "
            f"! {_detect_description('primary_detector', settings, primary_batch)} "
            "! queue max-size-buffers=8 leaky=downstream "
            "! gvastreamdemux name=primary_demux",
        )

    primary_fps = int(settings.get("fps", 10))
    smoke_fps = int(smoke.get("fps", 2))
    for index, camera in enumerate(cameras):
        source = _source_description(camera, index)
        camera_primary_fps = int(camera.get("fps", primary_fps))
        camera_smoke_fps = int(camera.get("smoke_fps", smoke_fps))
        primary_input = (
            _direct_input_description(
                settings, primary_width, primary_height, camera_primary_fps
            ) if direct_primary else _mux_input_description(
                settings, primary_width, primary_height, camera_primary_fps
            )
        )
        smoke_input = (
            _direct_input_description(
                smoke, smoke_width, smoke_height, camera_smoke_fps
            ) if direct_smoke else _mux_input_description(
                smoke, smoke_width, smoke_height, camera_smoke_fps
            )
        )
        primary_target = (
            f"appsink name=primary_frames_{index} emit-signals=true "
            "max-buffers=1 drop=true sync=false"
            if direct_primary else f"primary_mux.sink_{index}"
        )
        smoke_target = (
            f"appsink name=smoke_frames_{index} emit-signals=true "
            "max-buffers=1 drop=true sync=false"
            if direct_smoke else f"smoke_mux.sink_{index}"
        )
        parts.append(
            f"{source} ! decodebin3 ! vapostproc "
            "! video/x-raw(memory:VAMemory),format=NV12 "
            f"! tee name=decoded_{index} "
            f"decoded_{index}. ! queue name=primary_queue_{index} "
            "max-size-buffers=2 leaky=downstream "
            f"! videorate name=primary_rate_{index} drop-only=true "
            f"! video/x-raw(memory:VAMemory),format=NV12,framerate={camera_primary_fps}/1 "
            f"! {primary_input} "
            f"! {primary_target} "
            f"decoded_{index}. ! queue name=smoke_queue_{index} "
            "max-size-buffers=2 leaky=downstream "
            f"! videorate name=smoke_rate_{index} drop-only=true "
            f"! video/x-raw(memory:VAMemory),format=NV12,framerate={camera_smoke_fps}/1 "
            f"! {smoke_input} "
            f"! {smoke_target}"
        )

        if not direct_primary:
            primary_download = ""
            if settings.get("download_frames", False):
                primary_download = (
                    "! vapostproc ! video/x-raw,format=BGRx "
                    "! videoconvert ! video/x-raw,format=BGR "
                )
            parts.append(
                f"primary_demux.src_{index} ! queue max-size-buffers=2 leaky=downstream "
                f"{primary_download}"
                f"! appsink name=primary_results_{index} emit-signals=true "
                "max-buffers=1 drop=true sync=false"
            )
        if not direct_smoke:
            parts.append(
                f"smoke_demux.src_{index} ! queue max-size-buffers=1 leaky=downstream "
                f"! appsink name=smoke_results_{index} emit-signals=true "
                "max-buffers=1 drop=true sync=false"
            )
    return " ".join(parts)


class GroupedCameraPipeline:
    """A worker-level graph sharing two inference elements across its cameras."""

    LABEL_IDS = {
        "person": 0,
        "pedestrian": 0,
        "car": 2,
        "motorcycle": 3,
        "bus": 5,
        "truck": 7,
    }

    def __init__(self, cameras: list[dict], settings: dict,
                 publish: Callable[[DetectionResult], None],
                 publish_smoke: Callable[[dict], None],
                 publish_frame: Callable[[str, float, np.ndarray], None] | None = None,
                 publish_smoke_frame: Callable[[str, float, np.ndarray], None] | None = None):
        self.cameras = list(cameras)
        self.settings = settings
        self.publish = publish
        self.publish_smoke = publish_smoke
        self.publish_frame = publish_frame
        self.publish_smoke_frame = publish_smoke_frame
        self.pipeline = None
        self._pipeline_lock = threading.Lock()
        self._stage_lock = threading.Lock()
        self._stage_counts: dict[str, int] = {}
        os.environ.setdefault(
            "GST_PLUGIN_FEATURE_RANK",
            "vah264dec:MAX,vah265dec:MAX,avdec_h264:NONE,avdec_h265:NONE",
        )
        self.Gst, self.VideoFrame = self._load_runtime()
        self._validate()

    def request_stop(self) -> None:
        """Wake bus polling without changing pipeline state from another thread."""
        with self._pipeline_lock:
            pipeline = self.pipeline
        if pipeline is not None:
            pipeline.get_bus().set_flushing(True)

    @staticmethod
    def _load_runtime():
        try:
            import gi
            gi.require_version("Gst", "1.0")
            from gi.repository import Gst
            from gstgva import VideoFrame
        except (ImportError, ValueError) as exc:
            raise PipelineUnavailable(
                "install GStreamer, DL Streamer and gstgva Python bindings"
            ) from exc
        Gst.init([])
        return Gst, VideoFrame

    def _validate(self) -> None:
        missing = [
            name for name in (
                "urisourcebin", "decodebin3", "vapostproc", "videorate",
                "capssetter", "gvastreammux", "gvadetect", "gvastreamdemux", "appsink",
            )
            if self.Gst.ElementFactory.find(name) is None
        ]
        if missing:
            raise PipelineUnavailable("missing GStreamer elements: " + ", ".join(missing))
        for section_name, section in (
            ("settings", self.settings),
            ("smoke_fire", self.settings["smoke_fire"]),
        ):
            for key in ("model", "model_proc"):
                path = Path(section[key])
                if not path.is_file():
                    raise PipelineUnavailable(f"missing {section_name}.{key}: {path}")

    def _on_primary_sample(self, sink, camera: dict):
        sample = sink.emit("pull-sample")
        if sample is None:
            return self.Gst.FlowReturn.FLUSHING
        caps = sample.get_caps()
        structure = caps.get_structure(0)
        width = int(structure.get_value("width"))
        height = int(structure.get_value("height"))
        buffer = sample.get_buffer()
        video_frame = self.VideoFrame(buffer, caps=caps)
        aliases = {
            str(source).lower(): str(target).lower()
            for source, target in self.settings.get("test_label_aliases", {}).items()
        }
        detections = []
        for region in video_frame.regions():
            source_label = str(region.label() or "").lower()
            label = aliases.get(source_label, source_label)
            if label not in self.LABEL_IDS:
                continue
            rect = region.rect()
            detections.append({
                "class_id": self.LABEL_IDS[label],
                "label": "person" if label == "pedestrian" else label,
                "confidence": round(float(region.confidence()), 6),
                "bbox": [int(rect.x), int(rect.y), int(rect.x + rect.w), int(rect.y + rect.h)],
                "test_source_label": source_label if source_label != label else None,
            })
        bgr = None
        if self.settings.get("download_frames", False):
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
            camera_id=str(camera["id"]), timestamp=time.time(), width=width,
            height=height, detections=detections, frame=bgr,
        ))
        return self.Gst.FlowReturn.OK

    def _on_smoke_sample(self, sink, camera: dict):
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
                "label": label,
                "confidence": round(float(region.confidence()), 6),
                "bbox": [int(rect.x), int(rect.y), int(rect.x + rect.w), int(rect.y + rect.h)],
            })
        self.publish_smoke({
            "schema_version": "1.0", "camera_id": str(camera["id"]),
            "timestamp": time.time(), "smoke_fire": detections,
        })
        return self.Gst.FlowReturn.OK

    def _on_primary_frame(self, sink, camera: dict):
        return self._on_direct_frame(sink, camera, self.publish_frame)

    def _on_smoke_frame(self, sink, camera: dict):
        return self._on_direct_frame(sink, camera, self.publish_smoke_frame)

    def _on_direct_frame(self, sink, camera: dict, callback):
        sample = sink.emit("pull-sample")
        if sample is None:
            return self.Gst.FlowReturn.FLUSHING
        caps = sample.get_caps()
        structure = caps.get_structure(0)
        width = int(structure.get_value("width"))
        height = int(structure.get_value("height"))
        pixel_format = str(structure.get_value("format")).upper()
        buffer = sample.get_buffer()
        ok, mapped = buffer.map(self.Gst.MapFlags.READ)
        if not ok:
            return self.Gst.FlowReturn.ERROR
        try:
            if pixel_format == "NV12":
                rows = height * 3 // 2
                stride = len(mapped.data) // rows
                pixels = np.frombuffer(mapped.data, dtype=np.uint8).reshape(rows, stride)
                frame = pixels[:, :width, np.newaxis]
            else:
                stride = len(mapped.data) // height
                pixels = np.frombuffer(mapped.data, dtype=np.uint8).reshape(height, stride)
                frame = pixels[:, :width * 3].reshape(height, width, 3)
            if callback is not None:
                callback(str(camera["id"]), time.time(), frame)
        finally:
            buffer.unmap(mapped)
        return self.Gst.FlowReturn.OK

    def _on_source_setup(self, _bin, source, camera: dict) -> None:
        latency = int(camera.get("latency_ms", self.settings.get("rtsp_latency_ms", 200)))
        if source.find_property("latency") is not None:
            source.set_property("latency", latency)
        if source.find_property("drop-on-latency") is not None:
            source.set_property("drop-on-latency", True)

    def _count_stage(self, _pad, _info, key: str):
        with self._stage_lock:
            self._stage_counts[key] = self._stage_counts.get(key, 0) + 1
        return self.Gst.PadProbeReturn.OK

    def _attach_stage_probes(self, pipeline) -> list[tuple]:
        probes = []

        def attach(element_name: str, pad_name: str, key: str) -> None:
            element = pipeline.get_by_name(element_name)
            pad = element.get_static_pad(pad_name) if element is not None else None
            if pad is None:
                raise PipelineUnavailable(
                    f"diagnostic pad unavailable: {element_name}.{pad_name}"
                )
            probe_id = pad.add_probe(
                self.Gst.PadProbeType.BUFFER, self._count_stage, key
            )
            probes.append((pad, probe_id))

        attach("primary_mux", "src", "primary_mux_out")
        attach("primary_detector", "src", "primary_infer_out")
        attach("smoke_mux", "src", "smoke_mux_out")
        attach("smoke_detector", "src", "smoke_infer_out")
        for index, camera in enumerate(self.cameras):
            camera_id = str(camera["id"])
            attach(f"decoded_{index}", "sink", f"decoded.{camera_id}")
            attach(f"primary_queue_{index}", "sink", f"primary_branch_in.{camera_id}")
            attach(f"primary_queue_{index}", "src", f"primary_branch_out.{camera_id}")
            attach(f"primary_rate_{index}", "src", f"primary_selected.{camera_id}")
            attach("primary_mux", f"sink_{index}", f"primary_ready.{camera_id}")
            attach("primary_demux", f"src_{index}", f"primary_out.{camera_id}")
            attach("smoke_mux", f"sink_{index}", f"smoke_ready.{camera_id}")
            attach("smoke_demux", f"src_{index}", f"smoke_out.{camera_id}")
        return probes

    def _stage_snapshot(self, previous: dict[str, int], elapsed: float) -> dict:
        with self._stage_lock:
            current = dict(self._stage_counts)
        rates = {
            key: round((value - previous.get(key, 0)) / elapsed, 2)
            for key, value in current.items()
        }
        aggregate = {}
        per_camera = {}
        for key, rate in rates.items():
            stage, separator, camera_id = key.partition(".")
            if separator:
                aggregate[stage] = round(aggregate.get(stage, 0.0) + rate, 2)
                if stage.startswith("primary_") or stage == "decoded":
                    per_camera.setdefault(camera_id, {})[stage] = rate
            else:
                aggregate[stage] = rate
        return {"counts": current, "aggregate": aggregate, "primary": per_camera}

    def run(self, stop_event) -> None:
        backoff = 1.0
        while not stop_event.is_set():
            handlers = []
            probes = []
            try:
                pipeline = self.Gst.parse_launch(
                    grouped_pipeline_description(self.cameras, self.settings)
                )
                with self._pipeline_lock:
                    self.pipeline = pipeline
                with self._stage_lock:
                    self._stage_counts.clear()
                diagnostic_interval = float(
                    self.settings.get("stage_metrics_interval_seconds", 0)
                )
                if diagnostic_interval > 0:
                    probes = self._attach_stage_probes(pipeline)
                for index, camera in enumerate(self.cameras):
                    source = pipeline.get_by_name(f"source_{index}")
                    if source is not None:
                        source.connect("source-setup", self._on_source_setup, camera)
                    direct_primary = str(
                        self.settings.get("primary_backend", "dlstreamer")
                    ).lower() == "openvino"
                    direct_smoke = str(
                        self.settings["smoke_fire"].get("backend", "dlstreamer")
                    ).lower() == "openvino"
                    primary_sink = pipeline.get_by_name(
                        f"primary_frames_{index}" if direct_primary
                        else f"primary_results_{index}"
                    )
                    smoke_sink = pipeline.get_by_name(
                        f"smoke_frames_{index}" if direct_smoke
                        else f"smoke_results_{index}"
                    )
                    handlers.append((
                        primary_sink,
                        primary_sink.connect(
                            "new-sample",
                            self._on_primary_frame if direct_primary else self._on_primary_sample,
                            camera,
                        ),
                    ))
                    handlers.append((
                        smoke_sink,
                        smoke_sink.connect(
                            "new-sample",
                            self._on_smoke_frame if direct_smoke else self._on_smoke_sample,
                            camera,
                        ),
                    ))
                if pipeline.set_state(self.Gst.State.PLAYING) == self.Gst.StateChangeReturn.FAILURE:
                    raise PipelineUnavailable("grouped pipeline failed to enter PLAYING")
                log.info(
                    "group connected cameras=%d primary_backend=%s batch-%d@%s FPS "
                    "smoke/fire=batch-1@%s FPS",
                    len(self.cameras), self.settings.get("primary_backend", "dlstreamer"),
                    int(self.settings.get("batch_size", 4)),
                    self.settings.get("fps", 10), self.settings["smoke_fire"].get("fps", 2),
                )
                bus = pipeline.get_bus()
                previous_counts: dict[str, int] = {}
                previous_time = time.monotonic()
                while not stop_event.wait(0.25):
                    message = bus.timed_pop_filtered(
                        0, self.Gst.MessageType.ERROR | self.Gst.MessageType.EOS
                    )
                    if message is not None:
                        if message.type == self.Gst.MessageType.ERROR:
                            error, debug = message.parse_error()
                            raise RuntimeError(f"{error.message}; {debug or ''}")
                        raise RuntimeError("end of stream")
                    now = time.monotonic()
                    if diagnostic_interval > 0 and now - previous_time >= diagnostic_interval:
                        snapshot = self._stage_snapshot(
                            previous_counts, now - previous_time
                        )
                        log.info("stage-rates %s", json.dumps({
                            "aggregate": snapshot["aggregate"],
                            "primary": snapshot["primary"],
                        }, sort_keys=True))
                        previous_counts = snapshot["counts"]
                        previous_time = now
                backoff = 1.0
            except Exception as exc:
                detail = str(exc).lower()
                if any(marker in detail for marker in (
                    "base_inference", "failed to submit images to inference",
                    "unable to convert image using va-api", "vaapiconverter",
                    "failed to compile model", "cannot reshape",
                )):
                    log.error("grouped shared inference failure: %s", exc)
                    raise PipelineFatal(str(exc)) from exc
                log.warning("camera group disconnected: %s", exc)
                stop_event.wait(backoff)
                backoff = min(backoff * 2.0, 30.0)
            finally:
                with self._pipeline_lock:
                    pipeline = self.pipeline
                if pipeline is not None:
                    for pad, probe_id in probes:
                        pad.remove_probe(probe_id)
                    for sink, handler in handlers:
                        if sink is not None and handler is not None:
                            sink.disconnect(handler)
                    pipeline.get_bus().set_flushing(True)
                    pipeline.set_state(self.Gst.State.NULL)
                    with self._pipeline_lock:
                        if self.pipeline is pipeline:
                            self.pipeline = None
