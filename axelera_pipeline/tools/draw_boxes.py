#!/usr/bin/env python3
"""Draw one frame's saved boxes on the video frame, so a person can check them by eye."""
import json
import sys

import cv2

NAMES = "bus car minivan motorcycle rickshaw truck bicycle person others".split()
video, dump, frame_no, out = sys.argv[1], sys.argv[2], int(sys.argv[3]), sys.argv[4]
cap = cv2.VideoCapture(video)
cap.set(cv2.CAP_PROP_POS_FRAMES, frame_no)
ok, img = cap.read()
if not ok:
    sys.exit(f"could not read frame {frame_no} of {video}")
with open(dump) as f:
    boxes = [b for b in json.loads(f.readlines()[frame_no])["boxes"] if b[4] >= 0.30]
for x1, y1, x2, y2, score, cls in boxes:
    cv2.rectangle(img, (int(x1), int(y1)), (int(x2), int(y2)), (0, 255, 0), 2)
    cv2.putText(img, f"{NAMES[int(cls)]} {score:.2f}", (int(x1), max(12, int(y1) - 4)), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (0, 255, 255), 1)
cv2.imwrite(out, img)
print(f"{out}: {len(boxes)} boxes")
