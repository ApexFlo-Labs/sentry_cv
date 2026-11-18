# DL Streamer pipeline + V8 rules (integrated)

Our DL Streamer pipeline (GPU decode, detectors, tracker, faces, plates) feeding Kiranmai's V8 event code
(`v8code/`, her files unchanged, snapshot of commit `b645c04`). The output is V8 packages over the delivery-agent
contract (`contract/`).

**Default detector: Kiranmai's QAFT INT8 vehicle/person model** (`models/vehicle_qaft_int8_b1.xml`, from branch
`sentry_cv_multi`, `model_updates/2026-11-05-qaft-int8/build-context/model_static_b1`, SHA-256 unchanged).

## Results (Core Ultra 9 285H, 20 cameras x 10 fps + fire 2 fps, 300 s steady state)

Engine alone (no V8) does 240/s, which is the ceiling. All runs below have V8's full rules running, with
0 schema/contract errors.

| Configuration | Total fps | Detector input drops | Median latency |
|---|---|---|---|
| FP16 detector, GPU JPEG on demand | 196.1 | 45 /s | 382 ms |
| + package images via shared memory (`PKG_SHM=1`) | 199.5 | 41 /s | 380 ms |
| + region decode of crops (`REGION_DECODE=1`) | 204.3 | 37 /s | 364 ms |
| FP16 + both | 205.5 | 35 /s | 357 ms |
| QAFT INT8, without region decode / package shared memory | 202.5 | 38 /s | 364 ms |
| **QAFT INT8 + both (`run_best20.sh`, default)** | **209.4** | 31.5 /s | 349 ms |
| NNCF INT8 (post-training quantisation of the FP16 model) + both, for comparison | 235.5 | 4.7 /s | 191 ms |

### Why the drop from 240, and what the changes do
- With V8 running, the GPU stays 99% busy but takes about 30% longer per frame. CPU load, scheduling, power/heat
  and JPEG encoding were each tested and ruled out (`loadgen.py`). Only memory traffic on the shared RAM
  reproduced the slowdown. Leading cause: the GPU waits on memory. The memory-controller counters (root) were
  not measured.
- `REGION_DECODE=1`: when her `_crop` cuts a crop from a frame that has not been decoded yet, only that box
  (plus one 16-pixel block around it) is decoded from the GPU JPEG with libjpeg-turbo (`tjcrop.py`). The
  pixels are identical to a full decode; this is checked at run time (`region_checked` / `region_mismatch` in
  PIXSTATS). Full 1080p decodes drop from about 26,500 to 4,900 per 5 minutes.
- `PKG_SHM=1`: package JPEG bytes go from the camera worker to the handoff process through one shared-memory
  buffer per camera, instead of being pickled through the pipe. The multipart body is sent in pieces
  (byte-identical to hers). Her client and scheduler are unchanged; only the connection objects are wrapped
  (`pkgshm.py`).
- QAFT INT8: 96 of 102 convolutions run in INT8 on the GPU and are as fast as NNCF's. But 52 FakeQuantize
  steps cannot be merged into the convolutions and run on their own (0.45 ms per frame), plus 17 reorders.
  Per frame: FP16 3.28 ms, QAFT 3.12 ms, NNCF 2.76 ms (`tools_int8/qrt.py`). Re-exporting QAFT with
  quantisers only on convolution inputs and weights should close most of this gap (not tested).

### Accuracy notes
- No labelled set for these 9 classes has been run yet, so neither INT8 model has a measured accuracy.
- Compared with FP16 on 400 frames (`tools_int8/cmp3.py`, `missan.py`), NNCF INT8 finds the same objects.
  All 444 differences are the same box and class, with a score just below 0.3; above 0.6 confidence the
  two models agree on 99.8% of detections.
- QAFT is a fine-tuned model, so it is expected to differ from FP16: 94.5% the same, person 81%, truck 75%.
- Kiranmai's own report marks QAFT as "not production-qualified" (GPU vs CPU confidence differs by up to
  0.199).

## Setup

The code finds everything under `~/chandana_optimization` (= `$HOME/chandana_optimization`):

```
~/chandana_optimization/integrate/                      <- this folder (without group_ingest/)
~/chandana_optimization/group_ingest/                   <- this folder's group_ingest/ (secondary branch: plates + OCR,
                                                            strip_meta, model-procs, test RTSP config)
~/chandana_optimization/group_ingest/edge_app/          <- the repo's edge_app/ (secondary code: plates + OCR, loaded by sec_shared.py)
~/chandana_optimization/group_ingest/models/            <- + smoke_fire.xml/.bin from the repo's models/traffic-v11/openvino
~/chandana_optimization/group_ingest/secmodels/{traffic-v11,surveillance,scene}  <- the repo's models/ (LFS)
```

`setup_layout.sh` builds this layout from a checkout and compiles the C parts (`gatesc/build.sh` builds
`cgates.so`, `group_ingest/app/build_strip_meta.sh` builds `strip_meta.so`):

```bash
git lfs pull                                        # real model files, not LFS pointers
bash dlstreamer_pipeline/integrate/setup_layout.sh  # ROOT=/other/folder to put it elsewhere (then HOME=<parent of ROOT>)
```

Not code, so not in git. Provide these yourself:
- `~/chandana_optimization/sentrycv/venv`: Python venv with `requirements-engine-venv.txt` (numpy, opencv-python-headless,
  pillow). The engine runs `/usr/bin/python3` and adds this venv's site-packages.
- `~/chandana_optimization/rtsp/mediamtx`: mediamtx v1.21.3 (test RTSP server; `run_integrate.sh` streams the clips through it).
- `~/chandana_optimization/chandana_optimization_videos/`: the test clips `plate_clip_35-45min.mp4`, `PCR2_18h.mp4`,
  `PCR2_10h.mp4`, `NVR_ch14.mp4`.
- DL Streamer 2026.2 in `/opt/intel/dlstreamer` (GStreamer 1.28), OpenVINO 2026.2 (system Python), `ffmpeg`.

The static SigLIP model is generated from the dynamic one on first start. Run `bash ufd/build_export.sh` only if you
use the frame exporter (`EXPORT_EVERY` / `JPEG_GPU_RATE`, test only).

Clean-room check: this folder was set up with `setup_layout.sh` into an empty folder on the test box, using only these
git contents (with LFS) plus the three items above, and then run with `HOME=<that folder>`.

## Run

```bash
./run_best20.sh                 # final configuration, 20 cameras, 420 s (120 s warm-up + 300 s steady)
CAMS=4 DUR=240 ./run_best20.sh  # quick check
VEH_MODEL= ./run_best20.sh      # same, with the FP16 detector
```

Results go to `run/<timestamp>/`: `integrate.log` (STEADY fps, PIXSTATS, handoff counters),
`state/queues.txt` (queue fill/drops), and the mock delivery agent's packages (`mock_agent.py`, saves 1 in 20).

## Switches (all off unless set; `run_best20.sh` sets the final ones)

| Switch | Effect |
|---|---|
| `VEH_MODEL` | primary detector XML (default in `run_best20.sh`: QAFT INT8) |
| `PIX_JPEG=1 PIX_JPEG_OD=1 PIX_ON_DEMAND=1 PIX_LAZY=1` | frames for V8 are JPEG-encoded on the GPU media engine, only when V8 asks for one |
| `REGION_DECODE=1` | crop-only JPEG decode (see above) |
| `PKG_SHM=1` | package images via shared memory (see above) |
| `HANDOFF_PROCESS=1 HANDOFF_QUEUE=64` | her handoff scheduler in its own process, 64-package queue |
| `CFACES=1 POLICY_OURS=1` | faces from our C path; identity 0.7/s per camera (body every 3rd, gait every one, scene every 7th) |
| `EVENTS_IDLE=1 NO_PIX=1 NO_IDENTITY=1` | diagnosis only (V8 parts off) |

## Known issues
- Every run leaves 2 orphan `multiprocessing` processes (resource tracker + one spawn child, parent PID 1).
  Kill them after each run; the root cause is not fixed yet.
- 0–6 "frame no longer held by the engine" errors per 5-minute run when V8 asks for a frame the engine has
  already released (8 frames are held).
- Absolute paths: see Layout.
