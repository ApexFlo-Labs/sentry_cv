#!/usr/bin/env python3
"""Face vector check: her image's adaface-ir101-v18.1 vectors vs V8's reference face path on the same face.

Runs INSIDE the image (OpenVINO 2026.2). For every saved face package (evidence = wider original-frame face crop,
vector = what her image sent): V8's reference code (detectors/backends/openvino_face.py, sentry_cv b645c04,
unchanged) detects the face in the crop with SCRFD, aligns it to the 112x112 template, and embeds it with the same
AdaFace INT8 model. Cosine(her vector, reference vector) is reported, plus cosine between different packages
for scale. Usage (in container): face_reference_check.py <faces dir> <out json>
"""
import glob
import json
import os
import sys

import cv2
import numpy as np

sys.path.insert(0, "/ref")
os.environ.setdefault("FACE_DETECTOR_DEVICE", "CPU")
os.environ.setdefault("FACE_EMBEDDER_DEVICE", "NPU")
from detectors.backends.openvino_face import OpenVINOFaceExtractor  # noqa: E402

M = "/opt/dls-integrate/home/chandana_optimization/group_ingest/secmodels/traffic-v11/face/openvino"
POSE = "/opt/dls-integrate/home/chandana_optimization/integrate/models/best_a0_fp16.xml"
faces_dir, out = sys.argv[1], sys.argv[2]
ref = OpenVINOFaceExtractor(f"{M}/scrfd_500m.onnx", f"{M}/adaface_ir101_int8.xml", POSE)
ref.detector_threshold = 0.3  # the crop holds one face; find it even when small

rows, hers = [], []
for js in sorted(glob.glob(os.path.join(faces_dir, "*.json"))):
    meta = json.load(open(js))
    vector = np.asarray(meta["embedding"]["vector"], np.float32)
    crop = cv2.imread(js[:-5] + ".jpg")
    row = {"embedding_id": meta["embedding"]["embedding_id"], "camera": meta["evidence"].get("camera_id"),
           "crop": list(crop.shape[:2]), "preprocessing_id": meta["embedding"]["preprocessing_id"],
           "model_id": meta["embedding"]["model_id"], "model_version": meta["embedding"]["model_version"]}
    found = ref._detect(crop)
    if not found:
        row["reference"] = "no face found in the crop"
        rows.append(row)
        continue
    h, w = crop.shape[:2]
    box, landmarks, score = min(found, key=lambda f: np.hypot((f[0][0] + f[0][2]) / 2 - w / 2, (f[0][1] + f[0][3]) / 2 - h / 2))
    reference = ref._embed(ref._align(crop, landmarks))
    reference = reference / np.linalg.norm(reference)
    row.update(cosine=round(float(vector @ reference), 4), detector_score=round(score, 3),
               face_px=int(box[2] - box[0]))
    rows.append(row)
    hers.append(vector)

cos = [r["cosine"] for r in rows if "cosine" in r]
between = [float(hers[i] @ hers[j]) for i in range(len(hers)) for j in range(i + 1, len(hers))]
summary = {"faces": len(rows), "compared": len(cos),
           "cosine_her_vs_reference": {"median": float(np.median(cos)) if cos else None, "min": min(cos, default=None),
                                       "max": max(cos, default=None), "count_below_0_8": sum(c < 0.8 for c in cos)},
           "cosine_between_her_packages": {"median": float(np.median(between)) if between else None,
                                           "max": max(between, default=None)},
           "labels": sorted({(r["model_id"], r["model_version"], r["preprocessing_id"]) for r in rows}),
           "rows": rows}
json.dump(summary, open(out, "w"), indent=1, default=str)
print(json.dumps({k: v for k, v in summary.items() if k != "rows"}, default=str))
