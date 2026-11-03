#!/usr/bin/env python3
"""Run the FP16 detector on the iGPU and save boxes in original frame pixels, one JSON line per frame."""
import json
import sys

import cv2
import numpy as np
import openvino as ov

model_path, video, frames, out = sys.argv[1:5]
model = ov.Core().compile_model(model_path, sys.argv[5] if len(sys.argv) > 5 else "GPU")
output = model.outputs[0]
cap = cv2.VideoCapture(video)
count = total = 0
with open(out, "w") as f:
    while count < int(frames):
        ok, img = cap.read()
        if not ok:
            break
        h, w = img.shape[:2]
        s = min(640 / h, 640 / w)
        nh, nw = round(h * s), round(w * s)
        top, left = (640 - nh) // 2, (640 - nw) // 2
        canvas = np.full((640, 640, 3), 114, np.uint8)
        canvas[top:top + nh, left:left + nw] = cv2.resize(img, (nw, nh))
        x = canvas[:, :, ::-1].transpose(2, 0, 1)[None].astype(np.float32) / 255
        rows = model(x)[output].reshape(-1, 6)
        found = [[(r[0] - left) / s, (r[1] - top) / s, (r[2] - left) / s, (r[3] - top) / s, r[4], r[5]]
                 for r in rows[rows[:, 4] >= 0.25].tolist()]
        f.write(json.dumps({"frame": count, "boxes": found}) + "\n")
        count += 1
        total += len(found)
print(f"{model_path}: {count} frames, {total} boxes -> {out}")
