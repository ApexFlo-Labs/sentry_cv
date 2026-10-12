#!/usr/bin/env bash
set -eu

test -e /dev/dri/renderD128 || { echo "Missing Intel render device: /dev/dri/renderD128" >&2; exit 1; }
test -e /dev/accel/accel0 || { echo "Missing Intel NPU device: /dev/accel/accel0" >&2; exit 1; }
for element in urisourcebin decodebin3 vapostproc vah264dec vah265dec gvadetect appsink; do
  gst-inspect-1.0 "$element" >/dev/null || { echo "Missing GStreamer element: $element" >&2; exit 1; }
done
python3 -c 'import cv2, gi, numpy, openvino; from gstgva import VideoFrame'
python3 -c 'import openvino as ov; devices=ov.Core().available_devices; assert any(d == "NPU" or d.startswith("NPU.") for d in devices), f"NPU unavailable: {devices}"'
test -f models/traffic-v11/vehicle_yolo26n_fp16.xml || { echo "Run preflight from the sentry_cv repository root" >&2; exit 1; }
test -f models/traffic-v11/openvino/license_plate.xml || { echo "Missing plate model" >&2; exit 1; }
test -f models/traffic-v11/openvino/ocr.xml || { echo "Missing OCR model" >&2; exit 1; }
test -f models/traffic-v11/face/openvino/scrfd_500m.onnx || { echo "Missing face model" >&2; exit 1; }
test -f models/surveillance/transreid_ssl_int8.xml || { echo "Missing body ReID model" >&2; exit 1; }
test -f models/surveillance/gaitbase_int8.xml || { echo "Missing gait model" >&2; exit 1; }
test -f models/traffic-v11/openvino/smoke_fire.xml || { echo "Missing smoke/fire model" >&2; exit 1; }
test -f models/scene/openvino/siglip2-base-patch16-224.xml || { echo "Missing scene model" >&2; exit 1; }
echo "Preflight passed: VA decode, DL Streamer and Python bindings are available."
