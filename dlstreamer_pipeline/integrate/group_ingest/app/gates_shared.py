"""Quality gates taken from sentry_cv main (runtime_v6, commit 2dd99d2), shared by every camera.

Same rules and thresholds as main; every size limit is in camera pixels, the same for every camera
(a uniform floor, no camera is treated differently). The secondary branch sees a 640x360 copy of each
frame, so there the limits are scaled by 640 / camera width.

  vehicle_dedup  main pipeline/detection_quality.py deduplicate_vehicles: a second road-vehicle box
                 overlapping a kept one (IoU >= 0.55, or >= 85 % of it inside) is removed, whatever its class
  face_size      main detectors/backends/openvino_face.py: faces narrower than 28 px get no embedding
  plate_crop     main detection_quality.plate_crop_is_usable with worker.json: plate >= 16x5 px,
                 width/height 0.8-12, sharpness check off (min_sharpness 0.0); a failed plate is not read
  body_quality   main v6_events.py: ReID only for person crops >= 64 px high, 32 px wide, quality >= 0.2

gvapython loads its module file once per element, so the counters live here, in a module imported
normally (one copy per process). A "gates:" line every 10 s gives the per-second rates of what each gate saw and removed.
"""
import os
import threading
import time

ROAD_VEHICLES = {"bicycle", "bus", "car", "minivan", "motorcycle", "rickshaw", "truck"}
DEDUP_IOU, DEDUP_CONTAINED = 0.55, 0.85
FACE_MIN_PX = int(os.getenv("GATE_FACE_MIN_PX", "28"))
PLATE_MIN_W, PLATE_MIN_H = 16, 5
PLATE_ASPECT = (0.8, 12.0)
PLATE_MIN_SHARPNESS = float(os.getenv("GATE_PLATE_MIN_SHARPNESS", "0.0"))
BODY_MIN_H, BODY_MIN_W, BODY_MIN_QUALITY = 64, 32, 0.2
ENABLED = set(os.getenv("GATES", "vehicle_dedup,face_size,plate_crop,body_quality").split(","))
SAVE_DIR = os.getenv("GATE_SAVE_DIR", "")  # set: the first SAVE_MAX rejected plate and body crops are saved there, to look at
SAVE_MAX = 40
_saved = {"plate_crop": 0, "body_quality": 0}


def save_rejected(gate, crop, camera_id):
    if not SAVE_DIR or crop is None or crop.size == 0 or _saved[gate] >= SAVE_MAX:
        return
    import cv2
    _saved[gate] += 1
    os.makedirs(SAVE_DIR, exist_ok=True)
    cv2.imwrite(os.path.join(SAVE_DIR, f"{gate}_{_saved[gate]:02d}_{camera_id}_{crop.shape[1]}x{crop.shape[0]}.jpg"), crop)

_lock = threading.Lock()
_count = {k: 0 for k in ("vehicles", "vehicle_dedup", "faces", "face_size", "plates", "plate_crop", "bodies", "body_quality")}
_last = [time.time(), dict(_count)]


def add(**kwargs):
    with _lock:
        for k, v in kwargs.items():
            _count[k] += v
        now = time.time()
        if now - _last[0] < 10:
            return
        span, before = now - _last[0], _last[1]
        rate = {k: (_count[k] - before[k]) / span for k in _count}
        _last[0], _last[1] = now, dict(_count)
    print("gates: " + " ".join(f"{k}={v:.2f}" for k, v in rate.items()) + f" enabled={','.join(sorted(ENABLED))}", flush=True)


def _overlap(a, b):
    """IoU and the share of the smaller box inside the other; boxes are (x1, y1, x2, y2)."""
    w = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    h = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = w * h
    area_a = max(0.0, a[2] - a[0]) * max(0.0, a[3] - a[1])
    area_b = max(0.0, b[2] - b[0]) * max(0.0, b[3] - b[1])
    union, smaller = area_a + area_b - inter, min(area_a, area_b)
    return (inter / union if union > 0 else 0.0), (inter / smaller if smaller > 0 else 0.0)


def duplicate_vehicles(boxes):
    """boxes: list of (label, confidence, (x1, y1, x2, y2)). Returns the indexes to remove (main's rule:
    highest confidence first, persons never removed, the class may differ)."""
    order = sorted((i for i, b in enumerate(boxes) if b[0] in ROAD_VEHICLES), key=lambda i: (-boxes[i][1], i))
    kept, removed = [], []
    for i in order:
        if any(iou >= DEDUP_IOU or inside >= DEDUP_CONTAINED
               for iou, inside in (_overlap(boxes[i][2], boxes[k][2]) for k in kept)):
            removed.append(i)
        else:
            kept.append(i)
    return removed


def plate_usable(crop, scale):
    """Main's plate_crop_is_usable; crop is from the 640x360 copy, scale = 640 / camera width."""
    if crop is None or crop.size == 0:
        return False
    height, width = crop.shape[:2]
    if width / scale < PLATE_MIN_W or height / scale < PLATE_MIN_H:
        return False
    if not PLATE_ASPECT[0] <= width / max(height, 1) <= PLATE_ASPECT[1]:
        return False
    if PLATE_MIN_SHARPNESS <= 0.0:
        return True
    import cv2
    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY) if crop.ndim == 3 else crop
    return float(cv2.Laplacian(gray, cv2.CV_64F).var()) >= PLATE_MIN_SHARPNESS


def body_usable(crop, scale):
    """Main's body gate. Size and the area term are in camera pixels; sharpness is measured on the
    640x360 copy (main measures it at full size, so this term reads somewhat higher here)."""
    if crop is None or crop.size == 0:
        return False
    height, width = crop.shape[:2]
    if height / scale < BODY_MIN_H or width / scale < BODY_MIN_W:
        return False
    import cv2
    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    area = min(1.0, height * width / (scale * scale) / 120000.0)
    sharpness = min(1.0, float(cv2.Laplacian(gray, cv2.CV_64F).var()) / 800.0)
    exposure = max(0.0, 1.0 - abs(float(gray.mean()) - 127.5) / 127.5)
    return 0.4 * area + 0.4 * sharpness + 0.2 * exposure >= BODY_MIN_QUALITY


def gate_secondary(pipeline, camera_scale):
    """Adds the plate and body gates to the secondary pipeline, without changing its file:
    its plate detector's results and its ReID crops pass through the gates first.
    camera_scale(camera_id) -> 640 / camera width. Call once, after the pipeline is made."""
    state = {"scale": 1.0, "camera": ""}
    process, detect_plate, person_crops = pipeline._process, pipeline.models.detect_plate, pipeline._person_crops

    def gated_process(result):
        # _process runs under sec_shared's lock, so one camera at a time
        state["scale"], state["camera"] = camera_scale(result.camera_id), result.camera_id
        return process(result)

    def gated_detect_plate(crop):
        plates = detect_plate(crop)
        if "plate_crop" not in ENABLED:
            add(plates=len(plates))
            return plates
        kept = []
        for p in plates:
            plate = crop[p["bbox"][1]:p["bbox"][3], p["bbox"][0]:p["bbox"][2]]
            if plate_usable(plate, state["scale"]):
                kept.append(p)
            else:
                save_rejected("plate_crop", plate, state["camera"])
        add(plates=len(plates), plate_crop=len(plates) - len(kept))
        return kept

    def gated_person_crops(frame, people):
        crops, track_ids = person_crops(frame, people)
        if "body_quality" not in ENABLED:
            add(bodies=len(crops))
            return crops, track_ids
        keep = []
        for i, c in enumerate(crops):
            if body_usable(c, state["scale"]):
                keep.append(i)
            else:
                save_rejected("body_quality", c, state["camera"])
        add(bodies=len(crops), body_quality=len(crops) - len(keep))
        return [crops[i] for i in keep], [track_ids[i] for i in keep]

    pipeline._process = gated_process
    pipeline.models.detect_plate = gated_detect_plate
    pipeline._person_crops = gated_person_crops
