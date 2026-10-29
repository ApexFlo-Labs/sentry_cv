"""Shared part of secondary_branch.py: his secondary analytics (plates + OCR, body ReID, gait, scene), run unchanged inside our pipeline.

gvapython class Secondary, one instance per camera, on a branch that gets 1 frame/s per camera:
7 of every 10 are used, as his input_fps 0.7. The frame arrives as 640x360 BGRx (the GPU shrinks it,
the same pixels as his 640x640 letterbox without the borders). Boxes come from the detector's
normalized coordinates, read by class Stash before the GPU shrinks the frame (the shrink drops the
box metadata) and handed over by camera key and frame timestamp. His SecondaryPipeline._process does the work, with his models and settings;
one lock serialises it across cameras, as his single secondary worker does. Faces are left to our
own face chain. Prints per-second rates every 10 s ("secondary:" lines, read by sec_summary.py).
gvapython loads its module file separately for every element, so everything that must be shared
across elements and cameras (box hand-over, lock, one model set, counters) lives here, in a module
imported normally (Python keeps one copy per process).
"""
import glob
import os
import sys
import threading
import time

import numpy as np

# DL Streamer's setup script replaces PYTHONPATH, so the pipeline's Python does not see cv2/PIL;
# our venv's packages are added at the end (the numpy already loaded by DL Streamer stays in use)
sys.path.extend(glob.glob(os.path.expanduser("~/chandana_optimization/sentrycv/venv/lib/python3*/site-packages"))[:1])
# the secondary code (edge_app/secondary.py): this repo's own copy when the folder sits in the repo,
# else the copy in ~/chandana_optimization/sunil_code used for the measurements
_repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _repo if os.path.isdir(os.path.join(_repo, "edge_app")) else os.path.expanduser("~/chandana_optimization/sunil_code"))
from edge_app.pipeline import DetectionResult  # noqa: E402
from edge_app.secondary import SecondaryPipeline  # noqa: E402

M = "/home/admin1/sentry_cv/models"
CONFIG = {
    "plate_model": f"{M}/traffic-v11/openvino/license_plate.xml",
    "ocr_model": f"{M}/traffic-v11/openvino/ocr.xml",
    "face_detector_model": f"{M}/traffic-v11/face/openvino/scrfd_500m.onnx",
    "face_embedding_model": f"{M}/traffic-v11/face/openvino/adaface_ir101_int8.xml",
    "body_reid_model": f"{M}/surveillance/transreid_ssl_int8.xml",
    "gait_model": f"{M}/surveillance/gaitbase_int8.xml",
    "scene_model": f"{M}/scene/openvino/siglip2-base-patch16-224.xml",
    "plate_device": "NPU", "ocr_device": "NPU", "face_detector_device": "NPU",
    "face_embedding_device": "NPU", "body_reid_device": "NPU", "gait_device": "NPU", "scene_device": "NPU",
    "face_enabled": False, "body_reid_enabled": True, "gait_enabled": True, "scene_enabled": True,
    "plate_size": 224, "plate_confidence": 0.35, "ocr_confidence": 0.4,
    "person_interval": 3, "plate_interval": 3, "track_iou_threshold": 0.1, "track_hold_frames": 3,
    "scene_interval": 7, "queue_size": 32, "cache_dir": os.path.expanduser("~/chandana_optimization/ov_cache"),
}
_lock = threading.Lock()
_pipeline = None
_stash = {}  # camera key -> {frame timestamp: boxes (normalized)}
_count = {"empty": 0, "cams": set(), "frames": 0, "plates": 0, "anpr": 0, "reid": 0, "gait": 0, "scene": 0, "ms": 0.0}
_last = [time.time(), dict(_count)]


def _report():
    now = time.time()
    if now - _last[0] < 10:
        return
    span, before = now - _last[0], _last[1]
    d = {k: _count[k] - before[k] for k in _count if k != "cams"}
    print(f"secondary: frames={d['frames'] / span:.2f} plates={d['plates'] / span:.2f} anpr={d['anpr'] / span:.2f} "
          f"reid={d['reid'] / span:.2f} gait={d['gait'] / span:.2f} scene={d['scene'] / span:.2f} "
          f"ms={d['ms'] / max(1, d['frames']):.1f} empty={d['empty'] / span:.2f} cams={len(_count['cams'])}", flush=True)
    _count["cams"] = set()
    _last[0], _last[1] = now, {k: v for k, v in _count.items() if k != "cams"} | {"cams": set()}


def _pts(frame):
    """Frame timestamp (the GStreamer buffer's PTS; the shrink keeps it)."""
    buffer = frame.buffer() if hasattr(frame, "buffer") else getattr(frame, "_VideoFrame__buffer")
    return buffer.pts


class Stash:
    """Before the shrink: keeps this frame's boxes (normalized) under the camera key and timestamp."""

    def __init__(self, key):
        self.boxes = _stash.setdefault(str(key), {})

    def process(self, frame):
        self.boxes[_pts(frame)] = [(region.label(), float(region.confidence()), region.normalized_rect())
                                 for region in frame.regions()]
        while len(self.boxes) > 20:
            self.boxes.pop(next(iter(self.boxes)))
        return True


class Secondary:
    def __init__(self, key):
        global _pipeline
        with _lock:
            if _pipeline is None:
                _pipeline = SecondaryPipeline(CONFIG, consume=lambda payload: None)
        self.key = str(key)
        self.camera = f"camera-{int(key) + 1:02d}"
        self.boxes = _stash.setdefault(self.key, {})
        self.n = 0

    def process(self, frame):
        self.n += 1
        if self.n % 10 >= 7:  # 1 frame/s in, 0.7 frame/s used
            return True
        info = frame.video_info()
        width, height = info.width, info.height
        detections = []
        for label, confidence, (x, y, w, h) in self.boxes.pop(_pts(frame), []):
            detections.append({"label": label, "confidence": confidence,
                               "bbox": [int(x * width), int(y * height), int((x + w) * width), int((y + h) * height)]})
        if not detections:
            _count["empty"] += 1  # frames reaching the branch with no boxes (would show lost metadata)
        with frame.data() as image:
            bgr = np.ascontiguousarray(image[:, :, :3])
        result = DetectionResult(camera_id=self.camera, timestamp=time.time(), width=width, height=height,
                                 detections=detections, frame=bgr, pixel_format="BGR")
        with _lock:
            started = time.perf_counter()
            payload = _pipeline._process(result)
            _count["ms"] += (time.perf_counter() - started) * 1000
            _count["frames"] += 1
            _count["cams"].add(self.key)
            _count["plates"] += len(payload["plates"])
            _count["anpr"] += sum(1 for p in payload["plates"] if p["text"])
            _count["reid"] += len(payload["body_reid"])
            _count["gait"] += len(payload["gait"])
            _count["scene"] += payload["scene"] is not None
            _report()
        return True
