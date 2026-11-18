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

## Desired-state changes while running, and /readyz (contract)

`sentry-v8-contract.yaml` requires: a strictly increasing revision; validate the whole revision, then apply it
atomically; an invalid revision keeps the last valid one and reports not ready. `runtime_supervisor.py` does this around
`integrate9.py`, with the same behaviour as Kiranmai's `solution_image_entrypoint.py`:

- Every 2 s (`DESIRED_STATE_RELOAD_INTERVAL`) it reads the desired-state file. A changed file is copied, the copy is
  validated whole with her `DesiredStateValidator`, and its revision must be higher than the active one.
- **Same cameras, sources, fps, apps and embedding profiles** (her `_hot_reload_signature`, in `desired_runtime.py`):
  applied live. The engine sends each camera's new config to its V8 worker, which calls her
  `V8EventPipeline.reconfigure()`. Zones, lines, bindings, object types and scene interval change without a restart.
- **Anything else:** the engine is stopped cleanly and started with the new revision. If it does not start, the last
  valid revision is started again.
- **Rejected** (invalid file or revision not higher): the running revision stays, and `/readyz` returns 503 until a valid
  revision arrives.
- An engine that exits on its own is restarted with the active revision. The engine runs in its own process group, so
  nothing is left behind when it stops.
- Endpoints on `APEX_API_HOST:APEX_API_PORT` (default `0.0.0.0:8080`, as in `runtimeEndpoints`):
  - `GET /readyz`: 200 or 503, with `ready`, `worker_running`, active revision, reload state, last error and
    `per_camera_state`. Not-ready means the engine is not running, the engine heartbeat is silent, or the last revision
    was rejected. It does not require every camera to be healthy.
  - Per camera: `healthy` / `degraded` (`worker_starting`, `stale_frames`) / `unavailable` (`stream_disconnected`,
    `heartbeat_silence`, `worker_not_running`), with `last_reliable_at`, `as_of`, `retrying`, reconnects, retry backoff, and
    the requested and measured decoded / inference / events fps (10 s window).
  - `GET /healthz`: always 200 while the process runs. Same details, plus recent events and `capacity`: requested vs
    resolved aggregate fps, cameras below their requested fps, and `runtime_pressure`. This is the contract's
    `capacityResponse`; the requested fps is never changed.
  - `GET /metrics`: Prometheus text with her names (`apexfabric_runtime_ready/cameras/revision`,
    `sentinel_camera_decoded_fps`, `_inference_fps`, `_last_frame_age_seconds`, `_reconnects_total`), plus requested and
    resolved fps, pressure, camera state, and desired-state applied / hot / rejected / restart counts.

Run it with `SCRIPT=runtime_supervisor.py` in `run_integrate.sh`. Without the supervisor, `integrate9.py` behaves as
before: it reads the desired state once.

Tests:
- `run_reload_test.sh` (4 cameras, `rt_driver.py`): new zone (hot), broken JSON, lower revision, valid again,
  fps change (restart), camera removed (restart). **8/8 passed**: hot changes applied by all workers with no restart,
  and her rules used the new zone; rejected files left the old revision running with `/readyz` 503; the fps change
  restarted the engine in 6 s with camera 1 at 5.0 events/s; 0 contract errors; no orphaned processes.
- `run_sup20.sh` (20 cameras, one hot zone change in the middle): applied by 20/20 workers with no restart;
  208.4 fps, the same as without the supervisor; 0 contract errors.
- `run_health_test.sh` (4 cameras, `health_driver.py`): **7/7 passed**: all fields and `/metrics` present; capacity 40/40 fps,
  pressure none; a camera's feed cut gives `unavailable/stream_disconnected` while `/readyz` stays 200; the camera
  comes back healthy; an engine paused 8 s gives `/readyz` 503 with `heartbeat_silence`, then 200 again.
- `run_pressure20.sh` (20 cameras): `/healthz` reports 200 fps requested against about 170 resolved, with
  `runtime_pressure: high` and the cameras below 10 fps; `/readyz` stays 200.

Still open against the contract:
- `embeddingRules` asks for the exact face preprocessing for `adaface-ir101-v18.1`. Our one-SCRFD + C alignment
  reaches cosine 0.942 median against her path (0.961 on faces she would embed); this needs her decision.
- Container image (`resources`: uid 10001, mounts, devices) is not built yet. The test box has no Docker.

Fixed:
- "frame no longer held": the engine now holds 16 GPU frames per camera (`PIX_HELD`, was 8). 4 cameras: 0 errors (was
  9-37 per camera). 20 cameras: 207.5 fps (unchanged), 2 errors, both in the first 5 s after start-up, 0 in steady
  state.
- Read-only code folder (`readOnlyRootFilesystem`): every write goes to a setting (`RUN_ROOT`, `INTEGRATE_SECRETS`,
  `OV_CACHE_DIR`, `CV_CACHE_DIR`, `GST_REGISTRY`, `APEXFABRIC_STATE_ROOT`). Tested with the whole code folder read-only:
  20 cameras, 207.5 fps, 0 write failures, 0 contract errors; the static SigLIP model was created in `CV_CACHE_DIR`.
- Blank plate evidence frames (`REGION_DECODE=1`): her ANPR code keeps `packet.frame.copy()` as the plate frame, and a
  copy of the lazy placeholder was all black. The placeholder is now an array type whose `copy()` returns the real
  pixels (`event_worker_shm.py`, `_LazyFrame`). 20 cameras: 0 black of 1,705 plate frames (was 158 of 170).
- Plate boxes on the wrong vehicle: plates from the plate branch were matched to a vehicle in a later frame by box
  overlap (accepted down to 0.2). The branch now sends the frame it used (`pts`), and the worker finds the same
  vehicle on that frame and its track id, then places the plate on that track now. 4 cameras: 100% matched by track,
  boxes off the plate 8.7% -> 5.7% (plates older than 0.3 s: 70% -> 0%); the rest are mostly plate-model boxes.

Full run with both fixes (20 cameras under `runtime_supervisor.py`, 15 min measured, every package saved):
205.2 fps total, 7.5-9.1 fps per camera, latency median 356 ms, 140,638 packages, 0 contract errors, `/readyz`
healthy throughout. All 97,725 evidence images decode and none is blank; all 12,168 crops whose source frame was
saved match it pixel for pixel (offset 0,0).

Known, not pipeline faults:
- Plate OCR reads few plates: the plate branch works on 640x360, where plates on these wide views are 6-25 px.
- Fire/smoke: all 122 alarms in the full run are "smoke" on hazy clips (score 0.75-0.87), with no smoke in view. The
  fire branch input matches the model (320 letterbox, RGB, 0-1); to be checked with the model owner.
- About one "frame no longer held" per 1.5 min at 20 cameras (10 in 15 min); each skips one frame's events.
- The engine can crash on shutdown after all results are delivered (seen since 17 Nov); results are not affected.

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
- Run without the supervisor, every run leaves 2 orphan `multiprocessing` processes (resource tracker + one spawn
  child, parent PID 1); kill them after each run. Under `runtime_supervisor.py` they are stopped with the engine's
  process group.
- 0–6 "frame no longer held by the engine" errors per 5-minute run when V8 asks for a frame the engine has
  already released (8 frames are held).
- Absolute paths: see Layout.
