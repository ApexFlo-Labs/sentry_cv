"""INT8 detector with the detection head kept in floating point (fewer changed boxes than plain INT8).

    quantize_det2.py FP16.xml OUT.xml CLIP [CLIP ...]

Same calibration preparation as quantize_det.py (letterbox 640 centred, grey 114, RGB, 0..1), but
frames from every clip given, full bias correction, and the last block of the network (the detection
head: the highest "model.N" index in the layer names, as Ultralytics exports name them) left in FP16.
"""
import re
import sys

import cv2
import nncf
import numpy as np
import openvino as ov

src, out, clips = sys.argv[1], sys.argv[2], sys.argv[3:]
S, N = 640, 600


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
            samples.append((letterbox(frame)[:, :, ::-1].astype(np.float32) / 255.0).transpose(2, 0, 1)[None])
model = ov.Core().read_model(src)
model.reshape({model.input(0): [1, 3, S, S]})
blocks = [int(m) for op in model.get_ops() for m in re.findall(r"model\.(\d+)", op.get_friendly_name())]
if not blocks:
    sys.exit("no 'model.N' layer names found - cannot locate the detection head")
head = max(blocks)
print(f"calibration frames: {len(samples)} from {len(clips)} clips | detection head = model.{head}, kept in FP16")
quantized = nncf.quantize(model, nncf.Dataset(samples), preset=nncf.QuantizationPreset.MIXED,
                          subset_size=len(samples), fast_bias_correction=False,
                          ignored_scope=nncf.IgnoredScope(patterns=[rf".*model\.{head}[./].*"], validate=False))
ov.save_model(quantized, out)
print(f"saved {out}")
