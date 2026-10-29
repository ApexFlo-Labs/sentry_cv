"""INT8 copy of the 9-class detector (NNCF post-training quantization), for a GPU-time comparison.

    quantize_det.py FP16.xml OUT_INT8.xml CLIP [CLIP ...]

Calibration: 300 frames spread over the recorded clips, prepared as DL Streamer feeds the model
(letterbox to 640x640 centred with grey 114, RGB, 0..1). The source model is only read. The
detection head is left in floating point (ignored_scope by type is not needed: NNCF's MIXED preset
keeps the end-to-end output path accurate enough to compare; the output check decides).
"""
import sys

import cv2
import nncf
import numpy as np
import openvino as ov

src, out, clips = sys.argv[1], sys.argv[2], sys.argv[3:]
S, N = 640, 300


def letterbox(frame):
    h, w = frame.shape[:2]
    scale = min(S / h, S / w)
    rw, rh = int(round(w * scale)), int(round(h * scale))
    canvas = np.full((S, S, 3), 114, np.uint8)
    oy, ox = (S - rh) // 2, (S - rw) // 2
    canvas[oy:oy + rh, ox:ox + rw] = cv2.resize(frame, (rw, rh))
    return canvas


samples = []
per_clip = N // len(clips) + 1
for clip in clips:
    video = cv2.VideoCapture(clip)
    total = int(video.get(cv2.CAP_PROP_FRAME_COUNT)) or 750
    for k in np.linspace(0, total - 1, per_clip).astype(int):
        video.set(cv2.CAP_PROP_POS_FRAMES, int(k))
        ok, frame = video.read()
        if ok:
            rgb = letterbox(frame)[:, :, ::-1]
            samples.append((rgb.astype(np.float32) / 255.0).transpose(2, 0, 1)[None])
print(f"calibration frames: {len(samples)}")
model = ov.Core().read_model(src)
model.reshape({model.input(0): [1, 3, S, S]})
quantized = nncf.quantize(model, nncf.Dataset(samples), preset=nncf.QuantizationPreset.MIXED, subset_size=len(samples))
ov.save_model(quantized, out)
print(f"saved {out}")
