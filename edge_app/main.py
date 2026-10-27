"""Bare-metal grouped multi-camera edge application worker."""
from __future__ import annotations

import argparse
import json
import logging
import os
from pathlib import Path
import signal
import sys
import threading
import time

from .pipeline import GroupedCameraPipeline, PipelineFatal
from .secondary import SecondaryPipeline, DiscardSink
from .shared_frames import SharedFrameWriter

log = logging.getLogger(__name__)


def load_config(path: Path) -> dict:
    config = json.loads(path.read_text(encoding="utf-8"))
    config["settings"]["model"] = str((path.parent / config["settings"]["model"]).resolve())
    config["settings"]["model_proc"] = str(
        (path.parent / config["settings"]["model_proc"]).resolve()
    )
    for key in ("model", "model_proc"):
        config["smoke_fire_gpu"][key] = str(
            (path.parent / config["smoke_fire_gpu"][key]).resolve()
        )
    for key in (
        "plate_model", "ocr_model", "face_detector_model", "face_embedding_model",
        "body_reid_model", "gait_model", "scene_model",
    ):
        config["secondary"][key] = str((path.parent / config["secondary"][key]).resolve())
    ids = [camera["id"] for camera in config["cameras"] if camera.get("enabled", True)]
    if len(ids) != len(set(ids)):
        raise ValueError("camera ids must be unique")
    return config


def main() -> int:
    parser = argparse.ArgumentParser(description="Intel DL Streamer grouped inference worker")
    parser.add_argument("--config", default="edge_app/config.json")
    parser.add_argument("--camera-ids", help="comma-separated camera ids assigned to this worker")
    parser.add_argument("--worker-id", default="standalone")
    parser.add_argument("--no-config-reload", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    config_path = Path(args.config).resolve()
    config = load_config(config_path)
    if args.camera_ids:
        assigned = {item for item in args.camera_ids.split(",") if item}
        known = {str(camera["id"]) for camera in config["cameras"]}
        unknown = assigned - known
        if unknown:
            raise ValueError("unknown assigned camera ids: " + ", ".join(sorted(unknown)))
        config["cameras"] = [
            camera for camera in config["cameras"] if str(camera["id"]) in assigned
        ]
    cameras = [camera for camera in config["cameras"] if camera.get("enabled", True)]
    if not cameras:
        raise ValueError("worker has no enabled cameras")

    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())

    metrics_path = Path(f"/run/user/{os.getuid()}/sentry-cv-worker-{args.worker_id}.json")
    discard = DiscardSink(metrics_path=metrics_path)
    secondary_enabled = bool(config["secondary"].get("enabled", True))
    direct_primary = str(
        config["settings"].get("primary_backend", "dlstreamer")
    ).lower() == "openvino"
    direct_smoke = str(
        config["smoke_fire_gpu"].get("backend", "dlstreamer")
    ).lower() == "openvino"
    if direct_primary and secondary_enabled:
        raise ValueError("direct OpenVINO primary currently requires secondary.enabled=false")
    secondary = (
        SecondaryPipeline(config["secondary"], discard.consume).start()
        if secondary_enabled else None
    )
    if not secondary_enabled:
        log.info("secondary analytics disabled; primary metadata will be discarded")

    def publish_primary(result) -> None:
        if secondary is not None:
            secondary.submit(result)
            return
        discard.consume({
            "schema_version": "1.0",
            "camera_id": result.camera_id,
            "timestamp": result.timestamp,
            "frame": {"width": result.width, "height": result.height},
            "detections": result.detections,
        })

    frame_writers = {}
    if direct_primary:
        width = int(config["settings"].get("input_width", 640))
        height = int(config["settings"].get("input_height", 640))
        pixel_format = str(config["settings"].get("direct_input_format", "BGR"))
        frame_writers = {
            str(camera["id"]): SharedFrameWriter(
                str(camera["id"]), width, height, pixel_format=pixel_format
            )
            for camera in cameras
        }

    def publish_frame(camera_id: str, timestamp: float, frame) -> None:
        frame_writers[camera_id].write(frame, timestamp)

    smoke_frame_writers = {}
    if direct_smoke:
        width = int(config["smoke_fire_gpu"].get("input_width", 320))
        height = int(config["smoke_fire_gpu"].get("input_height", 320))
        pixel_format = str(
            config["smoke_fire_gpu"].get("direct_input_format", "BGR")
        )
        smoke_frame_writers = {
            str(camera["id"]): SharedFrameWriter(
                str(camera["id"]), width, height, stream="smoke",
                pixel_format=pixel_format,
            )
            for camera in cameras
        }

    def publish_smoke_frame(camera_id: str, timestamp: float, frame) -> None:
        smoke_frame_writers[camera_id].write(frame, timestamp)

    pipeline_settings = dict(config["settings"])
    pipeline_settings["smoke_fire"] = config["smoke_fire_gpu"]
    pipeline_settings["download_frames"] = secondary_enabled
    pipeline = GroupedCameraPipeline(
        cameras, pipeline_settings, publish_primary, discard.consume,
        publish_frame if direct_primary else None,
        publish_smoke_frame if direct_smoke else None,
    )
    worker_failure = threading.Event()

    def run_pipeline() -> None:
        try:
            pipeline.run(stop)
        except PipelineFatal:
            log.exception("worker=%s grouped inference pool failed", args.worker_id)
            worker_failure.set()
        except Exception:
            log.exception("worker=%s grouped pipeline failed unexpectedly", args.worker_id)
            worker_failure.set()

    thread = threading.Thread(
        target=run_pipeline, name=f"worker-pipeline-{args.worker_id}", daemon=True
    )
    thread.start()
    log.info(
        "worker=%s started grouped graph for %d cameras; primary model-instance-id=%s "
        "batch-size=%s",
        args.worker_id, len(cameras), config["settings"].get("model_instance_id"),
        config["settings"].get("batch_size", 4),
    )

    last_mtime = config_path.stat().st_mtime_ns
    exit_code = 0
    restart_for_config = False
    try:
        while not stop.wait(1):
            if worker_failure.is_set():
                log.critical("worker=%s exiting for a clean grouped-pipeline restart", args.worker_id)
                exit_code = 1
                stop.set()
                break
            if not args.no_config_reload and config_path.stat().st_mtime_ns != last_mtime:
                load_config(config_path)
                log.info("configuration changed; replacing process generation")
                restart_for_config = True
                stop.set()
                break
    finally:
        stop.set()
        pipeline.request_stop()
        thread.join(timeout=15.0)
        secondary_stopped = secondary.close() if secondary is not None else True
        for writer in frame_writers.values():
            writer.close()
        for writer in smoke_frame_writers.values():
            writer.close()
        log.info("discarded %d inference results", discard.generated)
        if thread.is_alive():
            log.critical("grouped pipeline thread survived shutdown")
        if not secondary_stopped:
            log.critical("secondary inference thread survived shutdown")
        if thread.is_alive() or not secondary_stopped:
            logging.shutdown()
            os._exit(exit_code or 1)

    if restart_for_config:
        logging.shutdown()
        os.execv(sys.executable, [
            sys.executable, "-m", "edge_app.main", "--config", str(config_path),
        ])
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
