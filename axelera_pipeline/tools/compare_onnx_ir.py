#!/usr/bin/env python3
"""Same weights? Run the ONNX detector and the OpenVINO IR on the same frames and match their boxes."""
import sys

import cv2
import numpy as np
import openvino as ov

onnx_path, ir_path, video = sys.argv[1:4]
frames = int(sys.argv[4]) if len(sys.argv) > 4 else 50
core = ov.Core()
models = [core.compile_model(core.read_model(p), "CPU") for p in (onnx_path, ir_path)]
for name, m in zip(("onnx", "ir"), models):
    print(name, [(o.get_any_name(), o.get_partial_shape()) for o in m.outputs])


def letterbox(img, size=640):
    h, w = img.shape[:2]
    s = min(size / h, size / w)
    nh, nw = round(h * s), round(w * s)
    canvas = np.full((size, size, 3), 114, np.uint8)
    top, left = (size - nh) // 2, (size - nw) // 2
    canvas[top:top + nh, left:left + nw] = cv2.resize(img, (nw, nh))
    return canvas[:, :, ::-1].transpose(2, 0, 1)[None].astype(np.float32) / 255


def iou(a, b):
    w = max(0, min(a[2], b[2]) - max(a[0], b[0]))
    h = max(0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = w * h
    return inter / ((a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter + 1e-9)


cap = cv2.VideoCapture(video)
total = matched = 0
conf_diff = []
for _ in range(frames):
    ok, img = cap.read()
    if not ok:
        break
    x = letterbox(img)
    a, b = (m(x)[m.outputs[0]] for m in models)
    if a.shape != b.shape:
        sys.exit(f"output shapes differ: {a.shape} vs {b.shape}")
    ba, bb = (o.reshape(-1, o.shape[-1]) for o in (a, b))
    ba, bb = ba[ba[:, 4] >= 0.25], bb[bb[:, 4] >= 0.25]  # rows: x1 y1 x2 y2 score class
    total += max(len(ba), len(bb))
    used = set()
    for p in ba:
        best = max(((iou(p, q), j) for j, q in enumerate(bb) if j not in used and int(q[5]) == int(p[5])), default=(0, -1))
        if best[0] >= 0.9:
            used.add(best[1])
            matched += 1
            conf_diff.append(abs(p[4] - bb[best[1]][4]))
print(f"boxes (conf>=0.25): {total}, matched (same class, IoU>=0.9): {matched} ({100 * matched / max(1, total):.1f}%), "
      f"max conf diff {max(conf_diff, default=0):.4f}")
