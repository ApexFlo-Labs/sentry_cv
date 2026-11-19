# History check before fixing (2026-11-17)

Every open problem from `T2_DESIGN_REVIEW_2026-11-17.md` checked against what was already done. Sources:
`sentry_cv/OPTIMIZATION_HANDOFF.md` (Codex log), `~/.codex/sessions`, `dlstreamer_architecture_research_2026-11-12/SENTRY_TARGET_ARCHITECTURE_LOW_LEVEL.md`
(spec v3, "the plan"), `T2_FULL_DLSTREAMER_PLAN.md` (my T2 log), `IMAGE_TESTS/reports`.

## Things I re-tested today that were already settled (mistakes, not to repeat)
| Re-test today | Already done | Result then | Result today |
|---|---|---|---|
| More videoconvert threads (`SENTINEL_T2_CONVERT_THREADS=4`) | T2 B10 (2026-11-16) | no gain | no gain (5 cams 6.7-7.1) |
| One detector per camera | V6 DEV27 (`SENTINEL_V6_DEV27_SHARED_INFERENCE_ARCHITECTURE.md`); `CL_OUT_OF_RESOURCES` | rejected, shared chosen | worse (6.2 of 8 FPS) |
| Scene search on NPU (planned, stopped before run) | C7 (handoff 2026-11-10 21:41) | oneDNN `CL_OUT_OF_RESOURCES`, not adopted | not run |

## Per problem

| # | Problem (measured 2026-11-17) | Already tried (source) | Not yet tried | Plan |
|---|---|---|---|---|
| 1 | Full 1080p BGR every frame, CPU videoconvert ~60 ms | VA region crop: pixel parity FAILED, max diff 249/40 (handoff §19, spec F14). C13 prefetch (overlap) adopted in C-line. C5 binding-aware pixels: only scene/fire-only cams skip; any subject app keeps full pixels (conservative). T2 B2 moved conversion into graph; B10 threads no gain | (a) own elements read BGRx directly (pure channel drop, exact) so the CPU videoconvert is not needed; (b) convert only on frames where a due object needs pixels (spec §4, never built) | (a) first, parity = byte-exact |
| 2 | Shared detector stalls all cameras (queue before it waits 0.4 s; its threads 99.7 %) | Per-camera detector (DEV27, today): rejected. GVA request pool 6 (C7): rejected. B3 Python lanes, B6 stage queues: no gain | Intel-documented `queue` directly after `gvadetect`/`gvaclassify` (multi-stream requirement; not in C18 or T2). `scheduling-policy=latency` (spec §6.4, "not tested" in CURRENT_C18_ARCHITECTURE.md) | test both, settings in graph string only |
| 3 | Plan vs build: spec v3 = branches after a tee, main line never waits (F5); T2 = one serial chain before main appsink | B3 lanes / B6 stage queues (in-line) | tee branches with leaky queues + result-by-PTS (bigger change to Python attach) | after 1+2, only if still limited (spec F15: extra queues alone gave no gain) |
| 4 | Detector finds 38 % of MOT17 people | V6 detection-miss diagnosis: small/crowded people missed; lowering 0.30 cutoff NOT recommended without false-event measurement | detector model / input-resolution change | model work, needs user decision; not a pipeline setting |
| 5 | Plate: wrong text with conf >= 0.82 on small plates; 80 px vehicle rule too low | Crop OCR accuracy 86-87 % already known (IMAGE_TESTS reports). 80 px chosen on traffic1+ANPR (spec §6.2 [T]); spec F19: every plate gate change re-run on traffic1+ANPR | plate-width rule (>= 60 px) | implement as option (default off = unchanged), measure on traffic1+ANPR, user signs off (policy) |
| 6 | 15 % duplicate plate reads after track breaks; recovery uses body+position only | spec §8 + build step 7 "recovery signals plate/face inside grace" planned, never built; "policy implementation, not contract change" | plate-text (vehicle) and face (person) recovery | implement step 7, measure duplicates + recovery on ANPR/MOT17 |
| 7 | Scene spread: hash offsets collide (8 cams: 2 pairs within 1 %) | B6 added crc32 phase (mine) | even slots by camera order in the worker | small change, test |
| 8 | Face success ratio 0.0 | none (metric never examined) | — | no change: 0.20 real value is also below 0.40 rule; documented |
| 9 | Fairness / scheduling across cameras | spec §6.4 planned `scheduling-policy=latency`; never tested | same as #2 | with #2 |

.64 is in use by another session (qualification suite); fixes are built and A/B tested on .92 first (same machine, same sources,
turbo state recorded), then .64 when free.

## Fix log

### Fix 1a - own BGRx->BGR element (`elements/sentinelbgr`, recipe `elements/build.sh`) - NOT ADOPTED
- Parity (`tests/parity/bgr/result.txt`): byte-exact vs videoconvert, 150 frames each on MOT17 1080p, ANPR 720p, ch9 1440p HEVC.
- Time: same as videoconvert (1080p 47.4 vs 47.6 ms; 720p 22.0 vs 21.6; 1440p 49.5 vs 50.5).
- Why (`tests/parity/bgr/map_cost.txt`): mapping the vapostproc BGRx buffer alone costs ~29 ms median (the GPU->CPU
  download happens at map); reading a 200x200 box costs the same as reading everything. So the cost is the download, not
  the channel drop, and region reads cannot reduce it. The remaining lever is to not map frames nobody needs (spec §4):
  at most ~33 % of frames on the people video, ~12 % on ANPR (T2 B1 measurement).

### Fix 2 - queue after inference elements + scheduling policy (image `localhost/sentinel-cv:v8-t2-fix2-local`, patch `image/apply_t2fix2_patch.py`) - BUILT, NOT YET MEASURED
- `SENTINEL_T2_POSTDET_QUEUE=N`: non-leaky queue (N buffers) directly after `primarydet` and after the face chain
  (Intel DL Streamer docs: "The queue element must be put directly after the gvadetect element ... required to achieve
  optimal performance in multi-stream deployments"; absent in C18 and T2). Never drops a frame.
- `SENTINEL_GVA_SCHEDULING_POLICY=latency`: earliest-PTS-first across cameras sharing a model instance (spec §6.4; never tested).
- Default off: graph string unchanged unless set.

### Fix 2b - even scene slots (patch `image/apply_t2fix2b_patch.py`) - BUILT, unit-checked
- With `SENTINEL_SCENE_SPREAD=1` (on in T2) camera k of n in a worker gets first-sample offset k/n of the interval
  (8 cams, 10 s: 0, 1.25, ... 8.75 s). Camera not in the map keeps the old crc32 phase.

### Fix 2c - plate-text recovery before a new presence is published (patch `image/apply_t2fix2c_patch.py`) - BUILT, NOT YET MEASURED
- Contract: `presence_recovered` = same presence after qualified camera-local reassociation. So the rejoin must happen
  before the new track is published (object_present after 3-4 hits). While a new vehicle track is unconfirmed, an OCR read
  >= `ocr_emit_min_confidence` equal to the CONFIRMED plate of exactly one lost vehicle presence (inside its end grace)
  rejoins the track (same mechanics as body/position reassociation; presence_recovered next frame). Never when an active
  vehicle holds the same text. `SENTINEL_PLATE_RECOVERY=1`, default off.
- Face-based person recovery (spec §8): NOT built. Needs an AdaFace match threshold (spec leaves it [A]); measurable on
  LFW pairs (13,233 images, pairs.txt present). Expected gain is small: in the 3-4 frame window before publication the
  face model runs about once (every 3rd frame) and ~20 % of people show a face (check 3).

### Fix 5 - plate width: no code needed
- `PLATE_MIN_WIDTH` already feeds `sentinelplate min-width` (default 16). Test 60 vs 16 on traffic1 + ANPR (spec F19).

### Waiting
.92 must stay light while another session runs a .64 campaign until ~03:00-04:00 UTC 2026-11-18; rootless podman here
cannot pin containers (`--cpuset-cpus` -> exit 126). Queued tests (none started):
1. SA 5x8 / 6x8 people: POSTDET_QUEUE=4; + scheduling latency.  (capacity, .64 preferred)
2. ANPR + traffic1 1x8 SVX: PLATE_RECOVERY 0/1 (duplicates, presence_recovered, plate_read); PLATE_MIN_WIDTH 16/60.
3. 8-camera scene-only run: scene sample spacing with even slots.

### Fix 3 - gait on the PLAIN model (user rule: no code inside models) - BUILT + PARITY PASS
- Was `gaitbase_dlstreamer_montage.xml` (sentry_cv/scripts/build_gait_dlstreamer_wrapper.py adds gather/reshape/transpose/
  divide in front of GaitBase). Now plain `gaitbase_int8.xml` via the production `OpenVINOGaitExtractor`, one compiled model
  shared by all cameras, new infer request per call (patch `image/apply_t2fix3_patch.py`; `SENTINEL_GAIT_PLAIN=0` = old).
- Parity (`tests/parity/gait_plain/result.txt`): montage vs plain on 40 sequences, NPU: 40/40 bit-identical; shared model,
  16 concurrent calls identical to direct calls.

### Fix 4 - face on PLAIN models, steps separate (user rule) - BUILT + PARITY PASS
- Was graph chain `scrfd_top1x_u8` (decode + top-1 + 1.6x square baked in) -> `face_align_u8` (2nd SCRFD + least-squares
  align + GridSample warp + AdaFace merged in one model). Now the production face path `OpenVINOFaceExtractor`: plain
  `scrfd_500m.onnx` (production letterbox/blob/decode/NMS) -> plain SixDRepNet `best_a0_fp16` pose gate ->
  `cv2.estimateAffinePartial2D` alignment -> plain `adaface_ir101_int8`. Same FaceSamplePipeline scheduling and gates.
  Patches `image/apply_t2fix4_patch.py` (`SENTINEL_FACE_PLAIN=0` = old chain) and `apply_t2fix4b_patch.py` (the three
  face models compiled once per process, not per camera; new infer request per call).
- Parity (`tests/parity/face_plain/result.txt`): patched vs original production extractor on MOT17 ground-truth people:
  MOT17-09 119/119 faces identical, MOT17-11 115/115 identical (boxes, embeddings, pose, quality); 12 frames in 12 threads
  identical. (MOT17-02 sample: 3/3, few faces pass the production pose gate there.)
- Not yet measured: capacity cost of running face in Python instead of the graph (queued with the other capacity tests).

### Fix2 test run on .64 (started 2026-11-18 00:19 UTC, after the other session released .64)
- Image `localhost/sentinel-cv:v8-t2-fix2` built ON .64 = `v8-t2-full-dls` + the same 5 patched files (sha256 match .92;
  .92 T2 sources byte-identical to the .64 build sources). Plain models present in the image.
- Runner `t2_campaign/fix2_runner.py` (on .64: `~/Documents/sentry-validation-20261116-t2`), ledger `ledger-fix2.jsonl`,
  same guarded harness + isolation (turbo off, k3s/apexfabric stopped; `isolate.sh off` runs automatically at the end).
  Only change to the harness template: `--min-free-disk-mib 3072` (was 6144; .64 /home has 5.7 GB free).
- Cases, each once: SA 5x8 old face/gait + queue 4; + scheduling latency; SA 4x8 plain face/gait; SA 6x8 with the best
  queue setting (only if a 5x8 passes); SV 1x8 ANPR + traffic1: recovery 0 / 1 / plate width 60; S 8x8 scene slots.
- Scorers: `tests/fix2/score_plates.py` (check-5 duplicate definition), `tests/fix2/score_scene.py` (phase of each camera
  inside the scene interval). Old T2 baseline (frontier-S-15x8): interval 2.07 s, closest two cameras 0.3 % apart, ideal 6.7 %.
- traffic1 gave 0 plate reads in the earlier step-1 runs, so plate recovery can only be judged on ANPR.

### Fix2 results (.64, isolated, 8 FPS)
| case | result | camera FPS | worst 10 s window |
|---|---|---|---|
| T2 SA 5x8 (earlier, for reference) | fail | ~6.9 | - |
| SA 5x8, old face/gait + queue after inference (4) | fail | 7.09-7.19 | 66 % |
| SA 5x8, + scheduling latency | fail | 7.20-7.38 | 67 % |
| SA 4x8, plain face + plain gait | NOT MEASURED: every camera crashed at start (my bug: telemetry read `gait_extractor.actual_device`, which the plain extractor lacks; fixed with a fallback, rerun as f2b) | - | - |
| SV 1x8 ANPR, recovery off | pass | 7.99 | 93 % |
| SV 1x8 ANPR, recovery on | pass | 8.00 | 93 % |
| SV 1x8 ANPR, plate min width 60 | fail on one completed-work window (89.7 % vs 90 %; mean 97.9 %), FPS fine | 7.99 | 93 % |
- Queue + scheduling: small gain (+0.3-0.4 FPS per camera), not enough; the shared detector stays the limit. 6x8 skipped.
- Plate recovery on ANPR: 26 rejoins; new vehicle presences 202 -> 186; presence_recovered 152 -> 191; plate reads 105 -> 108;
  duplicates (same text, other presence, <= 8 s) 15 -> 17, i.e. NOT reduced. Reason: all duplicates are two LIVE tracks on one
  vehicle (see review check 5 correction), which recovery correctly refuses.

- Plate width 60 (vs default 16) on ANPR: plate reads 105 -> 35, distinct texts 22 -> 9. Lost texts include plates read many
  times at 16 (AP12N7704 x12, TS09EG6531 x10, TS09PA1619 x9, AP29AG1358 x6, TS09UA0713 x5), i.e. real plates below 60 px are
  readable in this 720p video. Check 4 (shrunken sharp photos) overstated the need. DECISION: keep 16; do not adopt 60.
  16 px does give some short/partial texts (AP296, AP12N14, TS09UA26) - no ground truth for this video, not counted.

- traffic1 (1 cam, all three pass at 7.99-8.00 FPS): recovery off/on: 0 plate reads either way (331/330 'unreadable'
  outcomes; same as the earlier step-1 runs), new presences 597 -> 591, presence_recovered 224 = 224 -> no effect (no plate
  text to match). Width 60: 3 full-format reads (TL28T1742, AP29BW9462, AP29VW1440), 86 'unreadable'. Why 60 gives reads where
  16 gives none is NOT explained: an outcome does not block later reads (v8_events.py:1708) and the plate-due gate counts are
  the same (17,263 vs 17,681 due); per-plate OCR detail is not in the run output. Small numbers; not used for any decision.
- Plate-width conclusion: one fixed width does not suit both cameras (ANPR loses ~2/3 of reads at 60, traffic1 gains 3).
  Default stays 16. A per-camera rule would belong in the adaptive policy (it already observes plate widths) - not built.
- Plate recovery conclusion: works as designed (26 rejoins on ANPR, fewer new presences), harmless on traffic1, but does not
  fix the duplicate reads (those are two live tracks on one vehicle). Keep as option, default off, pending user sign-off.

- Scene slots (S 8x8, pass, 7.97-7.98 FPS): FIX2b did NOT give even spacing. Phases 0.07, 0.12, 0.18, 0.21, 0.52, 0.55, 0.79,
  0.88 of the 2.04 s interval; closest pair 2.3 %, widest gap 31.5 % (even = 12.5 %). No pair within 1 % (old hash: 1 of 15).
  Cause (code): the slot offset was taken from each camera's own start time (cameras start at different moments), and every
  later sample was timed from the actual sample frame, so frame jitter (up to 125 ms at 8 FPS) accumulated.
- FIX2d (`image/apply_t2fix2d_patch.py`, applied to `image/t2fix2/v8_events.py`): interval samples on a shared wall-clock grid
  t = (m + slot) x interval (frame time is wall clock, same for all cameras); next due time advances by whole intervals; an
  event scene within half an interval replaces the grid sample. Simulation (`tests/fix2/grid_sim.py`, 8 cams, random starts,
  8 FPS with jitter): gaps 11.5-13 % (ideal 12.5 %), interval unchanged (2.041 s). Queued on .64 as image v8-t2-fix2c, case
  f2c-S-8x8-grid.

- Plain face + plain gait start check (image v8-t2-fix2b, SA 1x8 people): PASS 7.97 FPS, no errors. Outputs vs the T2 graph
  chain on the same source (prof-B-SA-1x8-traced): gait 41 vs 40, body 90 vs 88, scene 59 vs 59, face **10 vs 27**.
- Why fewer faces (`tests/face_metric/face_size_rule.py`, result `face_size_rule_result.txt`; MOT17 ground-truth people, every
  3rd frame): the chain model `scrfd_top1x_u8` outputs the face box enlarged 1.6x (square, baked in by prepare_align.py) and the
  bridge uses THAT box as face width and as pose-crop base. MOT17-02: plain accepts 31 faces (>= 28 px real and pose ok);
  chain-style accepts 61 (same faces, pose on the wider crop) + 53 (real width < 28 px, passes 28 px only because of 1.6x)
  = 114 (3.7x). MOT17-09: 154 vs 175 + 112 = 287. Half the faces on MOT17-02 are under 28 px (median 27.5).
  So the chain broke the configured 28 px face-size rule (FACE_MIN_SIZE=28) and judged head angle on a wider crop. Which
  crop gives the true angle is NOT known (no head-pose ground truth here). Plain path follows the rules as written.
  DECISION FOR USER: keep 28 px real width (fewer, larger faces) or lower FACE_MIN_SIZE; pose crop base unverified.

- Plain face/gait capacity (v8-t2-fix2b): SA 4x8 PASS, 7.97 FPS all cams, worst window 93 % (T2: 5 of 6 passes). No cost.
- Scene grid run f2c-S-8x8-grid (image v8-t2-fix2c = FIX2d): FAIL on scene coverage (needs >= 54 samples in 120 s steady per
  camera, got 43; whole run 52-57 per camera vs 71-72 before). FPS 7.97-7.98. Phases (scored against the CONFIGURED 2.0 s):
  0.04, 0.16, 0.28, 0.41, 0.54, 0.665, 0.785, 0.91 = exactly even and stable (p10-p90 width ~0.05). The FIX2b run, scored the
  same way, drifts over almost the whole interval (p10-p90 0.06-0.95 per camera): FIX2b slots did not hold at all.
  NOTE: my first scorer used the measured median interval as the modulus, which smears phases; fixed (score_scene.py takes
  the nominal interval). The FIX2b numbers above (0.07..0.88) came from that flawed scorer.
- Cause of lost samples (code): scene-only cameras get pixels only when `_periodic_scene_due` (v8_events.py:579) says so,
  which still used "interval since last sample" (later than the grid time); at the grid time the frame had no pixels,
  `_scene` returned, and FIX2d had already advanced the due time -> slot lost.
- FIX2e (`image/apply_t2fix2e_patch.py`, on top of 2d): pixel-due uses the grid time; due advances only after a sample.
  Simulation with pixel gating (`tests/fix2/grid_sim2.py`): 2d 45-49 samples/cam (reproduces the failure), 2e 71/cam with
  even phases. Queued on .64: image v8-t2-fix2f, case f2f-S-8x8-grid.

- FIX2e verified on .64 (image v8-t2-fix2f, f2f-S-8x8-grid): PASS incl. scene coverage, 7.97-7.99 FPS, worst window 95 %.
  73-74 samples per camera (before: 71-72), phases 0.03, 0.16, 0.28, 0.41, 0.53, 0.66, 0.78, 0.91 of 2.0 s; gaps 11.7-13.3 %
  (ideal 12.5 %); next sample of any camera median gap to next sample 0.25 s (= 2.0 s / 8); FIX2b run: median 0.19 s, many < 0.25).
  ADOPT: scene samples are now evenly spread across cameras on a shared clock (user question: "divide interval so all scene
  search won't happen at a time" -> yes, now true and measured).
- Final config SA 5x8 (v8-t2-fix2c: plain face + plain gait + queue 4 + scheduling latency): PASS, 7.97-7.98 FPS, worst window
  92.5 %, coverage pass, per camera face 10-12 / gait 41-42 / body 89-90 / scene 59-60 (= 1-camera levels). T2 failed 5x8 (~6.9).
  Caveat: part of the gain is less face work (fewer faces pass the 28 px rule in the plain path). Queued: 6x8, 5x8 plain-only.

- Final config SA 6x8 (f2f, plain + queue 4 + latency): FAIL on one window only. Camera averages 7.76-7.94 (>= 7.6 ok), worst
  10 s window 69 %. T2 at 6 cams: 6.4-6.6 average. -> all apps at 8 FPS: 5 cameras reliable, 6 at the edge. 7x8 not run.
- Attribution SA 5x8 plain face/gait ONLY (no queue, no latency policy): steady window 7.97-7.98 FPS, worst window 95 %,
  outputs complete -> the 5-camera gain comes mainly from the plain face/gait path (graph face chain removed from the GPU
  graph), not from the queue/scheduling settings. The run is formally FAIL: the native graph worker stopped sending
  heartbeats ~6 s after start and the watchdog replaced it after 60 s (exit -9 at 01:17:57, 67 s after container start,
  55 s before the measured window); startup took 102.6 s instead of ~35 s. Memory normal (13 % RAM).
  This is the only worker kill in ~190 .64 cases across all ledgers (T2, profiling, fix2). Cause NOT known.
  Queued: 10 starts of SA 5x8 plain (fix2h_runner.py) to measure how often it happens.

- Startup stall frequency (fix2h, 10 starts of SA 5x8 plain, image v8-t2-fix2f): 10 of 10 PASS, 0 stalls, all cameras active
  after 34.4-35.4 s. Total on plain image: 1 stall in 15 all-apps starts; never in ~190 earlier cases. Cause unknown; the
  runtime watchdog recovered it automatically (camera outage ~1 min at startup). Keep watching in later campaigns.
- .64 restored after every chain (turbo on, k3s/apexfabric/sentry-cv/tracker active, no test containers).

### Summary of fix2 work (2026-11-18)
| item | result | decision |
|---|---|---|
| all apps, 8 FPS, max cameras | T2: 3-4 -> now 5 (6 at the edge: one window 69 %) | plain face/gait is the main gain |
| queue after inference + latency policy | +0.3-0.4 FPS/cam on T2 path; small | keep as option (needs sign-off) |
| plain gait model (user rule) | outputs equal (41 vs 40), parity bit-identical | adopt |
| plain face models (user rule) | ~1/3 of face samples: the old chain broke the 28 px face rule via its 1.6x box | adopt; USER: keep 28 px or lower |
| scene spacing across cameras | now exactly even on a shared clock, coverage pass | adopt (FIX2e) |
| plate-text recovery | 26 rejoins on ANPR; duplicates not reduced (they are 2 live tracks) | option, default off, sign-off |
| plate width 60 px | ANPR loses ~2/3 of reads | do not adopt; keep 16 |
| duplicate plate reads | cause = two live tracks on one vehicle (not track breaks) | open: detector/tracker double box |

### Incident 2026-11-17 19:14 UTC - .92 root disk full (0 MB)
- Writers in the previous 4 h: GNOME tracker index ~2.6 GB, IMAGE_TESTS ~1.1 GB (other session), mine ~0.36 GB.
- Freed (mine only): 57 stopped probe containers, superseded local images v8-t2-b1..b11 + dangling layers, gzip of my
  gst-latency logs, MOT17 replay intermediates (regenerable). Free space ~2 GB afterwards.
- People sources 19101-19111 died at 19:14 and were restarted 19:16:37 UTC (people11.pid mtime; separate fleet, same fixture). The other session invalidated its two cases in that window.
