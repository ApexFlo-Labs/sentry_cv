"""Face changes tested in the face-pipeline session (~/Documents/dls-face-trace-2026-11-20, README), user decision
2026-11-24: both in the image, no switch.

1. WhenetPose: head pose for the camera process's quality check with WHENet FP16 (replaces SixDRepNet A0, as the C
   pose step in the engine does). Input as the WHENet demo / cluster/whenet_site.py: face box widened x +-20 %,
   y +-10 % (clamped), stretched to 224x224, RGB 0..255 (normalisation inside the model). Output yaw, roll, pitch (deg).
2. ClearFaceGate: on the face the best-face window chose, before it is sent: AdaFace on the aligned 112x112 face and on
   its mirror image, cosine of the two (cluster/flip_test.py); with the face detector's confidence, ranked against this
   camera's recent chosen faces; kept if the mean of the two percentile ranks is in the top 60 % (cluster/group_visual.py
   gate(), keep=0.6). Measured there: grouping error 22 % -> 8-10 % on the site identity crops.
   Alignment is the reference's (trace_face.py similarity + aligned_input: least squares to the 5-point template,
   bilinear, zero outside the person box), on the camera process's frame pixels.
"""
import collections
import os

import numpy as np

TPL = np.array([38.2946, 51.6963, 73.5318, 51.5014, 56.0252, 71.7366, 41.5493, 92.3655, 70.7299, 92.2041]).reshape(5, 2)


def whenet_crop(frame, bbox):
    """Face box widened x +-20 %, y +-10 %, clamped to the frame (as cluster/whenet_site.py inp())."""
    H, W = frame.shape[:2]
    x1, y1, x2, y2 = [float(v) for v in bbox]
    w, h = x2 - x1, y2 - y1
    x1, x2 = max(0.0, x1 - 0.2 * w), min(float(W), x2 + 0.2 * w)
    y1, y2 = max(0.0, y1 - 0.1 * h), min(float(H), y2 + 0.1 * h)
    return frame[int(y1):int(y2), int(x1):int(x2)]


class WhenetPose:
    """estimate(bgr crop from whenet_crop) -> (pitch, yaw, roll) in degrees, as the SixDRepNet estimator it replaces."""

    def __init__(self, model_path, device=None, telemetry=None):
        import openvino as ov
        device = device or os.getenv("HEAD_POSE_DEVICE", "NPU")
        config = {"PERFORMANCE_HINT": "LATENCY"}
        if device.startswith("NPU"):
            config["INFERENCE_PRECISION_HINT"] = "f16"
        self.model = ov.Core().compile_model(model_path, device, config)
        self.telemetry = telemetry

    def warmup(self):
        self.estimate(np.zeros((64, 64, 3), np.uint8))

    def estimate(self, crop):
        import cv2
        if crop is None or crop.size == 0:
            raise ValueError("empty head-pose crop")
        rgb = cv2.cvtColor(cv2.resize(crop, (224, 224), interpolation=cv2.INTER_LINEAR), cv2.COLOR_BGR2RGB)
        out = np.asarray(self.model([rgb.transpose(2, 0, 1)[None].astype(np.float32)])[0]).reshape(-1)
        yaw, roll, pitch = float(out[0]), float(out[1]), float(out[2])
        if not all(np.isfinite([yaw, roll, pitch])):
            raise ValueError("head pose not finite")
        return pitch, yaw, roll


def similarity(lm):
    p = np.asarray(lm, float).reshape(5, 2)
    mp, mt = p.mean(0), TPL.mean(0)
    a_, b_ = p - mp, TPL - mt
    den = (a_ ** 2).sum()
    a = (a_ * b_).sum() / den
    bb = (a_[:, 0] * b_[:, 1] - a_[:, 1] * b_[:, 0]).sum() / den
    return np.array([[a, -bb, mt[0] - (a * mp[0] - bb * mp[1])], [bb, a, mt[1] - (bb * mp[0] + a * mp[1])]])


def aligned_face(crop, bounds, lm, person):
    """112x112 BGR aligned face from a frame crop (its frame position = bounds); lm and person in frame pixels."""
    import cv2
    ox, oy = bounds[0], bounds[1]
    pts = np.asarray(lm, float).reshape(5, 2) - (ox, oy)
    M = similarity(pts.reshape(-1))
    inv = cv2.invertAffineTransform(M)
    H, W = crop.shape[:2]
    bx1, by1 = max(0, int(person[0]) - ox), max(0, int(person[1]) - oy)
    bx2, by2 = min(W, int(person[2]) - ox), min(H, int(person[3]) - oy)
    out = np.zeros((112, 112, 3))
    ys, xs = np.mgrid[0:112, 0:112]
    sx = inv[0, 0] * xs + inv[0, 1] * ys + inv[0, 2]
    sy = inv[1, 0] * xs + inv[1, 1] * ys + inv[1, 2]
    x0, y0 = np.floor(sx).astype(int), np.floor(sy).astype(int)
    fx, fy = sx - x0, sy - y0
    for dy in (0, 1):
        for dx in (0, 1):
            xx, yy = x0 + dx, y0 + dy
            w = (fx if dx else 1 - fx) * (fy if dy else 1 - fy)
            ok = (xx >= bx1) & (xx < bx2) & (yy >= by1) & (yy < by2) & (w != 0)
            out[ok] += w[ok, None] * crop[yy[ok], xx[ok]]
    return np.clip(np.rint(out), 0, 255).astype(np.uint8)


class ClearFaceGate:
    """Per camera (one per camera process). keep(candidate) -> True/False for the face the best-face window chose."""

    KEEP = 0.6        # top 60 % (face-pipeline session's gate)
    HISTORY = 300     # recent chosen faces of this camera the ranks are taken against
    WARMUP = 20       # until this many faces were seen, every face is kept (no ranking yet)

    def __init__(self, adaface, side):
        self.adaface, self.side = adaface, side  # side: face box -> (landmarks, detector confidence, person box)
        self.hist = collections.deque(maxlen=self.HISTORY)
        self.counts = collections.Counter()

    def _emb(self, img):
        t = ((img / 255.0 - 0.5) / 0.5).transpose(2, 0, 1)[None].astype(np.float32)
        e = np.asarray(self.adaface([t])["out"], dtype=np.float64).reshape(-1)
        return e / max(1e-12, float(np.linalg.norm(e)))

    def keep(self, candidate):
        info = self.side.get(tuple(candidate.get("face_box") or ()))
        if info is None or info[0] is None or candidate.get("image") is None:
            self.counts["no_landmarks_kept"] += 1
            return True
        lm, conf, person = info
        face = aligned_face(candidate["image"], candidate["bounds"], lm, person)
        flip = float(self._emb(face) @ self._emb(face[:, ::-1].copy()))
        if len(self.hist) < self.WARMUP:
            keep = True
            self.counts["warmup_kept"] += 1
        else:
            flips = np.array([h[0] for h in self.hist])
            confs = np.array([h[1] for h in self.hist])
            rank = ((flips < flip).mean() + (confs < conf).mean()) / 2
            keep = rank >= 1 - self.KEEP
            self.counts["kept" if keep else "dropped"] += 1
        self.hist.append((flip, conf))
        candidate["mirror_cos"], candidate["detector_confidence"] = flip, conf
        decided = sum(self.counts.values())
        if decided % 50 == 0:
            import time
            print(f"{time.strftime('%T')} clear-face gate: {dict(self.counts)}; mirror median "
                  f"{float(np.median([h[0] for h in self.hist])):.2f}", flush=True)
        return keep
