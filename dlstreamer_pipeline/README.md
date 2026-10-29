# DL Streamer pipeline

One GStreamer / DL Streamer pipeline for all cameras. On the Core Ultra 9 285H box it runs 20 cameras at
10 fps with every model, using the FP16 detector: 240 inferences/s (200 primary + 40 fire/smoke), GPU
compute ~98%, whole-system CPU ~37%, pipeline memory ~1.9 GB. Capacity tops out at ~254/s.
Report: DL Streamer Pipeline.pdf.

## How it works

- Decode on the GPU (VA-API). Frames stay in GPU memory for primary detection (zero-copy).
- Each camera is capped at 10 fps right after decode; extra frames are dropped before any inference.
- One detector instance (`model-instance-id`) is shared by all cameras.
- Boxes are decoded by DL Streamer's C++ post-processing and stay as frame metadata. No Python runs on
  the main path.
- Tracker, then face detection (SCRFD, decode inside the model) on person boxes every 43rd frame, then
  face alignment + AdaFace in one model on the NPU.
- Fire/smoke branch: 2 fps per camera, scaled to 320x320 on the GPU, then the model on the GPU.
- Secondary branch: 1 fps per camera (7 of 10 frames used = 0.7 fps), boxes read in Python, frame scaled
  to 640x360 on the GPU, then `edge_app/secondary.py` (plates + OCR, ReID, gait, scene on the NPU),
  unchanged.

`pipeline_2cameras.txt` is the exact pipeline for 2 cameras; the same block repeats for each camera.

## Files

| File | Purpose |
| --- | --- |
| run_cascade.sh | builds and runs the pipeline; prints fps, per-stream fps, CPU, NPU, GPU engines, RAM, secondary rates |
| secondary_branch.py, sec_shared.py | gvapython entry + shared state for the secondary branch (one model set, one worker) |
| sec_summary.py | secondary rates from a run log |
| v11_model_proc.json | 9-class detector labels, RGB input |
| fire_rgb_model_proc.json | fire/smoke, RGB input |
| scrfd_top1_proc.json | face detector (decode + top-1 face inside the model) |
| tools/prepare_v11.py | fixes the traffic-v11 detector (vehicle_yolo26n_fp16_dynamic) to 1x3x640x640 |
| tools/prepare_top1.py, tools/prepare_align.py | build the face detector with in-model decode and the alignment + AdaFace model |
| tools/quantize_det.py, tools/quantize_det2.py | INT8 detector builds (tests only; not used) |
| tools/compare_det.py, tools/det_variant_check.sh | box-by-box output check of two detector builds |
| tools/gpu_busy.py | GPU engine busy % for one process |

## Models

Not committed (binary). On the box they are in `~/chandana_optimization/cascade/models/`
(also `~/chandana_optimization/dlstreamer_pipeline_share/models/`): `vehicle_v11_static`,
`scrfd_top1x_u8`, `face_align_u8`. The tools above rebuild them from `models/traffic-v11/` and the face
models. The secondary models are the repo's `models/` (read from `/home/admin1/sentry_cv/models`).

## Run (on the box, RTSP streams cam01..cam10 on port 8654)

```
cd ~/chandana_optimization/cascade
env DET_MODEL=$PWD/models/vehicle_v11_static.xml DET_PROC_FILE=$PWD/v11_model_proc.json DET_SHARED=1 CAP=10 \
    COUNT=0 NIREQ=8 QMAX=8 PRE=opencv ADA_DEV=NPU SCRFD_DEV=GPU ALIGN=1 \
    FIRE_DEV=GPU FIRE_FPS=2 FIRE_PROC_FILE=$PWD/fire_rgb_model_proc.json FIRE_SCALE=1 FIRE_COUNT=1 \
    SEC=1 FINT=43 ./run_cascade.sh 20 150 faces
```

`PRINT_ONLY=1` prints the pipeline without running it. Paths currently point to `~/chandana_optimization`
on the box (scripts, models, venv for cv2/PIL); they need to be made configurable during integration.

## Open items for integration

- Publish results (now discarded in `fakesink`): gvametaconvert / gvametapublish or an appsink callback.
- Per-camera reconnect: a dropped camera does not come back yet (the other cameras keep running).
- Restart if the pipeline process exits (all cameras are in one process).
- Long soak test; fire/smoke and face output checks with the 9-class detector.
- Secondary step runs ~72 ms per frame (~98% busy at 13.4 frames/s).
