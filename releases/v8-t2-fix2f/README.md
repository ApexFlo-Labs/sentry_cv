# v8-t2-fix2f

Test image `localhost/sentinel-cv:v8-t2-fix2f` (only on .64, not pushed to a registry). Built on T2 (full DL Streamer
graph, image `v8-t2-full-dls`), which is built on C18 (`v8-c18-plate-admission`).

## What is in it (vs T2)
- Face on PLAIN models as separate steps (scrfd_500m GPU -> SixDRepNet best_a0_fp16 NPU -> alignment in Python ->
  adaface_ir101_int8 NPU), one compiled copy per worker. Old graph chain only with `SENTINEL_FACE_PLAIN=0`.
- Gait on the PLAIN gaitbase_int8 model (silhouettes still from the `sentinelgait` element), one shared copy.
  Old montage wrapper only with `SENTINEL_GAIT_PLAIN=0`.
- Scene search: each camera samples at its own slot on a shared wall-clock grid (k/n of the interval); pixels are
  requested at the grid time; the next slot advances only after a sample is taken.
- Optional, default OFF: `SENTINEL_T2_POSTDET_QUEUE=N` (queue after the detector), `SENTINEL_GVA_SCHEDULING_POLICY=latency`,
  `SENTINEL_PLATE_RECOVERY=1` (plate-text rejoin of an unconfirmed vehicle track to one lost presence).

## Measured on .64 (isolated, 8 FPS, people video, harness v8_capacity_probe + run_guarded_case)
- All apps: 5 cameras PASS (T2: 3-4). 6 cameras: averages 7.76-7.94 FPS but one 10 s window 69 % -> fail.
- The gain comes mainly from the plain face/gait path (5x8 plain alone: 7.97-7.98 FPS); queue + latency add ~0.3 FPS/cam.
- Scene, 8 cameras: phases evenly spaced (gaps 11.7-13.3 % of the interval, ideal 12.5 %), 73-74 samples/camera, pass.
- Face samples about 1/3 of T2: the old chain's 1.6x box let faces under the 28 px rule through
  (tests/face_metric/face_size_rule.py). User decision pending: keep 28 px or lower FACE_MIN_SIZE.
- Plate width 60 px: loses ~2/3 of ANPR reads -> not adopted (default 16). Plate recovery: works, but duplicates are
  two live tracks on one vehicle, so it does not remove them.
- One graph-worker stall at startup in 15 starts (watchdog recovered it in ~60 s); 10 later starts clean. Cause unknown.
- Runtime unit tests (runtime_v8/tests): 374 passed, 2 skipped, 0 failed, same as before these files changed.

Full detail: `reports/HISTORY_CHECK_2026-11-17.md` (fix log and results), `reports/T2_DESIGN_REVIEW_2026-11-17.md`
(stage review, checks 1-7, section 6 = architecture now).

## Folders
- `reports/` design review, history/fix log, T2 build log, profiling findings
- `patches/` the patch scripts that produced the fix2 changes (the final code is already in runtime_v8)
- `tests/` scorers, parity checks (face/gait plain), tracking/counting eval, plate width eval, scene grid simulations
- `campaign/` .64 runners (guarded harness, isolation script, host sampler)

## Not included
Models, video fixtures, run artifacts, compiled .so files. The native build recipe (`native/t2_elements/build.sh`) uses
local paths for the OpenCV static build and image SDK headers, which are not in the repo.
