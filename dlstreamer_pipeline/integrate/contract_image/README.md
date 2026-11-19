# Chandana's DL Streamer integrate pipeline as a V8-contract image

Image: `localhost/sentinel-cv-dls-integrate:v1`, pushed as `ghcr.io/apexflo-labs/sentinel-cv-dls-integrate:<tag>`
(linked to this repo through the `org.opencontainers.image.source` label; separate from the `sentinel-cv-runtime` builds).
Source: this repo (ApexFlo-Labs/sentry_cv, branch `dlstreamer-pipeline`) at the commit checked out:
`dlstreamer_pipeline/integrate/` + `edge_app/` + `models/{traffic-v11,surveillance,scene}`.
Contract: `runtime/image_schema/sentry-v8-contract.yaml` (sentry_cv b645c04, identical to her `v8code/image_schema` copy).

## How it is put together

```
container (uid 10001, read-only root, writable /tmp/sentinel, /plans, /run/sentinel-delivery)
 └─ dls_entrypoint.py  = V8 solution supervisor from sentry_cv b645c04 (runtime/, files unchanged) + settings below
      - /configs/desired_state.json: validate whole revision -> apply; invalid -> keep last valid, report not ready
      - GET /healthz /readyz /metrics on :8080 (V8's own code)
      - starts the camera worker = worker_root/services/worker/stream_fleet_openvino.py (shim)
 └─ shim: env -i + DL Streamer env + her switches (as her run scripts) -> exec her integrate9.py
 └─ her engine (integrate9.py + event_worker_shm.py + model_server + handoff_host, her models), patched copies only
```

Her files are copied unchanged from the commit; `apply_contract_patch.py` applies the contract changes to the copies in
the build context (every edit must match exactly once, or the build stops). Her own `runtime_supervisor.py` (d65908e) is
in the image but not used: the V8 supervisor runs instead (it also handles per-camera changes and live add/remove).

## What was changed, and why (contract reference)

| # | Change | Where | Contract |
|---|---|---|---|
| 1 | Supervisor = V8's own (desired-state reload, readiness, health, metrics) | `runtime/` (b645c04, unchanged) | desiredState, runtimeEndpoints |
| 2 | A revision with the same deployment is applied camera by camera in the running engine: settings live (her `V8EventPipeline.reconfigure()`), fps/source = only that camera's source rebuilt + its event process restarted, profiles = only its event process restarted, camera removed = it stops alone (branch kept idle), camera added = idle branch reused or a new branch built into the running graph. Only a changed deployment restarts the worker (stop old, then start new; failed start -> last valid restarted) | `dls_entrypoint.py`, patch (REVISION_CODE), handoff_host/model_server edits | desiredState.apply, cameraIsolation |
| 3 | Cameras from the supervisor's validated `CAMERAS_FILE` (not her own desired-state load) | patch | desiredState.apply |
| 4 | Ready file per camera on first frame; stop file -> stop and drain | patch + `contract_glue.py` | readiness, startup admission |
| 5 | Child processes die with the worker (no orphans after a restart) | `contract_glue.child` | one runtime per deployment |
| 6 | V8's decoder/supervisor status files + V8 CameraTelemetry per camera; requested vs delivered fps and engine drops as gauges | `contract_glue.CameraStatus` | capacityResponse, cameraHealth |
| 7 | `tcp://` sources (tcpclientsrc, as V8); non-RTSP reachability check | patch | adapter source types |
| 8 | Camera down at start, or added while unreachable: reported unavailable and retried; its idle branch's errors are not fatal | patch | runtimeAvailability, cameraIsolation |
| 9 | Camera sources: RTSP sets up only video tracks, over TCP (site report 2026-11-19: cameras with audio/metadata tracks failed "not-linked"); TLS without CA matching (rtsps/https), user decision 2026-11-18 | `contract_glue.camera_source_setup` | adapter source types |
| 16 | No core files (RLIMIT_CORE 0): crashes filled the host disk with 5-6 GB cores through apport | shim | resources |
| 10 | Shared memory as V8: /dev/shm when it holds every camera's blocks, else files in /tmp/sentinel/shm | `contract_glue.install_shared_memory` | resources (writable mounts) |
| 11 | OpenVINO caches in /tmp/sentinel, one shared cache with compiles taking turns (file lock) | `contract_glue.child`, shim | resources |
| 12 | A dead camera event process is restarted alone (max 5); near-zero output for 60 s (GPU hang) stops the worker; a worker that exits is restarted with the last valid revision (5 s .. 300 s backoff) | patch, `dls_entrypoint.py` | cameraIsolation, recovery |
| 13 | (not changed) Scene keeps her every-7th rule (~1 sample per 10 s per camera) over the desired-state `sample_interval_seconds`, user decision 2026-11-18 | — | desiredState.controls (deliberate deviation) |
| 14 | Secondary branch camera list generated from the real cameras | shim | up to 50 cameras |
| 15 | Ubuntu's GStreamer 1.24 Python bindings removed (they shadowed DL Streamer's 1.28 ones) | Dockerfile | — |

Not changed: her models, her policy (face every 43rd frame, body/gait 0.7 frames/s, scene every 7th identity frame,
plates 1 read OCR >= 0.40), her V8 code copy, her handoff scheduler (only its dispatch loop re-reads its connections).

## Build

```
git lfs pull
dlstreamer_pipeline/integrate/contract_image/build.sh     # TAG=v1; WORK_DIR=~/Documents/dls-contract-image-work
```
The checkout must be clean in the source paths (commit first). Build context, base-image libraries and `IMAGE_ID`
go to `WORK_DIR`. Base: `ghcr.io/kiranmaibattu-cyber/sentinel-cv-runtime@sha256:f82000d8…` (v8-r2), used only for
OS, DL Streamer 2026.2, OpenVINO 2026.2, GPU/NPU user space and uid 10001; C parts are compiled against its libraries.

## Tests

`tests/` (run on a box with podman, GPU and NPU; sources and the capture agent come from a sentry_cv checkout at
`~/Documents/sentry_cv`): `contract_tests.py`, `revision_per_camera_test.py`, `camera_types_test.py`,
`desired_inputs_test.py`, `delivery_faults_test.py` (+ `fault_agent.py`), `face_collect.py` +
`face_reference_check.py` + `face_compare.py`, `semantics_check.py`, `limits_test.py`. Results: `RESULTS.md`.
