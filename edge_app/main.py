"""Small, bare-metal multi-camera edge application."""
from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path
import signal
import threading
import time

from .pipeline import CameraPipeline
from .secondary import SecondaryPipeline, DiscardSink

log = logging.getLogger(__name__)


def load_config(path: Path) -> dict:
    config = json.loads(path.read_text(encoding="utf-8"))
    config["settings"]["model"] = str((path.parent / config["settings"]["model"]).resolve())
    config["settings"]["model_proc"] = str((path.parent / config["settings"]["model_proc"]).resolve())
    for key in ("model", "model_proc"):
        config["smoke_fire_gpu"][key] = str((path.parent / config["smoke_fire_gpu"][key]).resolve())
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
    parser = argparse.ArgumentParser(description="Intel DL Streamer multi-camera inference")
    parser.add_argument("--config", default="edge_app/config.json")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    config_path = Path(args.config).resolve()
    config = load_config(config_path)
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    discard = DiscardSink()
    secondary = SecondaryPipeline(config["secondary"], discard.consume).start()
    class CameraRunner:
        def __init__(self, camera: dict, settings: dict, smoke_fire: dict):
            self.camera = camera
            self.stop_event = threading.Event()
            pipeline_settings = dict(settings)
            pipeline_settings["smoke_fire"] = smoke_fire
            self.pipeline = CameraPipeline(
                camera, pipeline_settings, secondary.submit, discard.consume
            )
            self.thread = threading.Thread(
                target=self.pipeline.run, args=(self.stop_event,),
                name=f"camera-{camera['id']}", daemon=True,
            )

        def start(self) -> None:
            self.thread.start()

        def close(self) -> None:
            self.stop_event.set()
            self.thread.join(timeout=5)

    runners: dict[str, CameraRunner] = {}
    signatures: dict[str, str] = {}

    def reconcile(updated: dict) -> None:
        wanted = {str(c["id"]): c for c in updated["cameras"] if c.get("enabled", True)}
        for camera_id in set(runners) - set(wanted):
            runners.pop(camera_id).close()
            signatures.pop(camera_id, None)
            log.info("camera=%s removed; other streams remain active", camera_id)
        for camera_id, camera in wanted.items():
            signature = json.dumps({
                "camera": camera, "settings": updated["settings"],
                "smoke_fire_gpu": updated["smoke_fire_gpu"],
            }, sort_keys=True)
            if signatures.get(camera_id) == signature:
                continue
            if camera_id in runners:
                runners.pop(camera_id).close()
                log.info("camera=%s changed; replacing only this stream", camera_id)
            runner = CameraRunner(camera, updated["settings"], updated["smoke_fire_gpu"])
            runners[camera_id] = runner
            signatures[camera_id] = signature
            runner.start()

    reconcile(config)
    log.info("started %d cameras with model-instance-id=%s", len(runners), config["settings"].get("model_instance_id"))
    last_mtime = config_path.stat().st_mtime_ns
    try:
        while not stop.wait(1):
            try:
                mtime = config_path.stat().st_mtime_ns
                if mtime != last_mtime:
                    updated = load_config(config_path)
                    reconcile(updated)
                    last_mtime = mtime
            except Exception:
                log.exception("config reload rejected; current camera pipelines remain active")
                time.sleep(1)
    finally:
        stop.set()
        for runner in list(runners.values()):
            runner.close()
        secondary.close()
        log.info("discarded %d generated secondary results", discard.generated)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
