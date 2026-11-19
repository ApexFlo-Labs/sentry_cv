# T2 — full DL Streamer pipeline (plan, 2026-11-16)

User decision 2026-11-16: build the whole pipeline in DL Streamer (not step by step on top of C18 Python).
T1 tests on .64 stopped at the user's request; results kept in `t1_campaign/results_64/`, report
`report/T1_RESULTS_2026-11-16.pdf`. .64 restored (k3s, services, turbo on) and T1 data/image removed.

## What v8/C18 does today (checked in code, runtime_v8)
- In the DL Streamer graph: decode → fps gate → gvamotiondetect → gvadetect vehicle/person (GPU) → gvatrack →
  face gvadetect `scrfd_top1x_u8` (GPU, person ROIs) → face gvaclassify `face_align_u8` (NPU, align+AdaFace in one
  model) → appsink `vasink`. Fire: own 2 FPS branch, NPU (`surface_stream.py:1140-1250`).
- Pulled into Python per frame: full 1080p → BGR (`primary_selective_bgr`), then Python crops and pushes each crop
  into separate `appsrc → gvainference → appsink` pipelines (`dls_secondary.py`): plate detector 224 letterbox,
  OCR 128×64, body TransReID 128×256 (PIL bilinear, RGB), SigLIP2 224, gait montage 264×320. Python waits on each.
  Then v8_events event rules (presence/zone/line/identity/plate), JPEG evidence, delivery.

## T2 target (one graph per camera, branches after the tee; Python gets metadata + small JPEGs only)
```
src → decode (VA) → fps gate → motion gate → gvadetect vehicle/person → gvatrack → sentinelstate (due regions)
  ├ plate:  gvadetect license_plate on plate_due (NPU) → gvaclassify OCR raw on license_plate (NPU)
  ├ face:   existing scrfd_top1x_u8 + face_align_u8 chain on face_due persons (separate models, user rule)
  ├ body:   gvaclassify TransReID on body_due persons (NPU)
  ├ scene:  gvainference SigLIP2 full frame at scene interval (GPU)
  ├ gait:   phase 2 (needs silhouettes; keep v8 path only for walking persons until an element exists)
  ├ fire:   unchanged 2 FPS NPU branch
  └ evidence: JPEG encoded in-graph only for frames/crops that become events
appsink(s) → Python: v8_events rules on metadata (no full-frame conversion)
```

## Phases (each measured; nothing called "done" until measured)
- **A. Graph-only capacity (local, .92):** the full graph above with N cameras in one process, Python only reading
  metadata. Answers: how many cameras does the box take when Python never touches pixels? Compare with C18
  (5@5, 3@8, 2@10 all apps).
- **B. Integration into the v8 runtime (image T2):** surface_stream consumes branch metadata instead of pulling
  pixels; v8_events unchanged where possible; evidence from in-graph JPEG.
- **C. Contract parity** (`exact_profile_model_weights_preprocessing...`): in-graph crops vs current Python crops for
  plate/OCR, TransReID, SigLIP2 (F14 pixel parity). Any profile that does not match stays on the old path and is
  reported.
- **D. .64 campaign:** same T1 test set (functional, recovery, frontiers, vehicle, live), T2 vs T1/C18.

## Known risks (from earlier measurements)
F1 VA pre-processor exhaustion with many VA branches; F2 nireq ≥ branch count; F14 VA region crop ≠ full-frame
pixels; body/scene preprocessing differs from in-graph resize (needs parity); gait needs a new element.

## Phase A results (this machine .92, turbo OFF for these runs, people video MOT17, all model branches, 8 FPS)
Script `tests/t2_phaseA/graph_capacity.py` (+ `run.sh`, `sweep.sh`), log `tests/t2_phaseA/results/results.log`.
Not included yet: Python event rules, evidence JPEG, delivery, gait, face-pose gate.

| Cameras ×8 | Body every frame | Body once per second (v8 rule) |
|---|---|---|
| 3,4,5,6 | PASS (6: slowest 7.97) | — |
| 8 | FAIL 7.54, window 0.875, NPU 81 % | **PASS 7.92, window 0.95, NPU 67 %** |
| 10 | FAIL 6.47, NPU 87 % | FAIL 7.53, window 0.84, NPU 84 % |
| 12 | FAIL 5.51, NPU 93 % | — |
CPU 22–29 % in all runs. C18/T1 limit for the same workload on .64: 3 cameras.
Bottleneck in the graph-only test = NPU (face embed, plate, OCR, body, fire all on NPU). GPU sampler not working yet.
Turbo was set to 1 on .92 for these runs — restore with `echo 0 | sudo tee /sys/devices/system/cpu/intel_pstate/no_turbo`.

## Phase B1 (image localhost/sentinel-cv:v8-t2-b1, local) — RESULT: functional, but SLOWER than T1 → not adopted as-is
Patch: `image/apply_t2_patch.py` (on T1 overlay → `image/overlay_t2/`), build `image/t2_local/`. Tests `tests/t2_b1/`.
- In-graph plate detector + OCR feeding the unchanged v8 OcrStabilizer; lazy full-frame conversion; body grouped 1/s.
- 1×8 all apps, C18 harness settings (warm-up 20 s, steady 120 s): ANPR PASS (118 plate_read, 54 plate_outcome);
  people PASS (242 person_feature_sample). All event types produced, no fatal lines.
- 4 people cameras ×8, this machine turbo off: **T1 PASS 7.63–7.72 FPS; T2-B1 FAIL 6.33–6.42 FPS.**
  Python ms per finished frame: total 122 (T1) vs 143 (B1); event policy 62 vs 97; conversion 43 vs 52; gait 29 vs 46;
  in-graph plate metadata read 22 ms (B1 only).
- Why: (1) lazy conversion is synchronous, T1's C13 prefetch overlapped it; (2) gstgva region/tensor walk is slow;
  (3) ~120 ms of per-frame Python (event rules, gait MOG on 1080p, body/face crops) remains in ONE process for all cameras.
- Pixels still needed per frame on people video: 67 % of frames (gait every 3rd, face every 3rd on another phase,
  body + image-quality sample 1/s). ANPR: 88 % (plate sharpness = adaptive plate gate, and plate_read source frame [K]).
- Next: test process split (v8 already has SENTINEL_SHARED_DLSTREAMER_GROUP_SIZE; opt27 used 5, C18 uses 50 = one process),
  then gait / body / face crops in-graph, cheap plate metadata reader.

## Phase B2 (localhost/sentinel-cv:v8-t2-b2): full-frame BGR conversion inside the graph
Patch `image/apply_t2b2_patch.py` (SENTINEL_T2_GRAPH_BGR=1): same elements as v8's converter
(vapostproc → BGRx → videoconvert → BGR) placed before vasink. Metadata (regions, track IDs) survives unchanged
(`tests/t2_b2/meta_survival.py`). Python ms/frame 122 → 84, but capacity unchanged:
4×8 people 7.55–7.74 (T1 7.63–7.72), 5×8 5.91–6.10. Gait worker thread (SENTINEL_GAIT_WORKER=1): no gain (7.41–7.54 / 5.63–5.91).

## Profiling (py-spy on the camera process, B2, 5×8 people) — `tests/runs/prof_*_5x8.txt`
- Python lock held only 35 % of wall time → Python lock is NOT saturated. 25 % of lock time = gstgva RegionOfInterest.__init__.
- Camera-thread wall time: 30.6 % waiting on shared secondary model lanes (of that: body 69 %, plate 23 %, gait 4 %,
  scene 4 %); 17.1 % gait MOG2 on 1080p (CPU, lock released); 16.2 % waiting for next frame from the graph.
- Each secondary (body, plate, scene, gait) = ONE inference request shared by all cameras → cameras queue.

## Contract constraint found (affects "everything in DL Streamer")
v8 body preprocessing is Pillow bilinear (antialias) by design ("identical canonical Pillow bilinear"); gait silhouettes
come from MOG2 on the full frame; face alignment is inside face_align_u8. Contract requires exact profile preprocessing.
Moving these into GVA (OpenCV resize) changes vectors → needs either an exact-preprocessing custom element or a
measured parity result + user sign-off (Management gallery compatibility).

## Phase B3 (localhost/sentinel-cv:v8-t2-b3): parallel secondary lanes (execution only, contract-safe)
`dls_secondary.py`: SENTINEL_DLS_BODY_LANES / PLATE / SCENE / GAIT lanes (cap 4, default 1 = C18).

## Status 2026-11-16 (end of session) — read this first to continue
- Bottleneck isolation (B3, 5×8 people, this machine turbo off): SX PASS 7.98, SIF PASS 7.95, SIB PASS 7.97,
  SIFB FAIL 7.39–7.60, SIFBG FAIL 6.83–7.16, SA FAIL ~6.0. Limit = identity work done serially per camera thread.
  Python lock switch-interval 0.5/20 ms: no effect. Lanes (body 3, plate 2): no effect.
- User decision: build EXACT elements (contract unchanged).
- DONE exact: `elements/common/pillow_resize.hpp` (bit-exact vs Pillow 11.3.0: 3000 random + 300 real, 0 diffs);
  `elements/common/body_embed.cpp`; plugin `elements/sentinelbody/libgstsentinelbody.so`
  (in pipeline: 199/199 vectors cos 1.0 vs v8 NPU body path, `tests/parity/sentinelbody_pipeline_result.txt`).
- Finding: v8's body path on CPU is not run-to-run repeatable (31/153 crops differ, cos ≥0.991); on NPU (production)
  it is repeatable and our element matches it exactly.
- Image `localhost/sentinel-cv:v8-t2-b4` (patch `image/apply_t2b4_patch.py`, build `image/t2b4_local`):
  1×8 PASS 7.98, 1115 in-graph body vectors used, 0 box mismatches, all identity profiles produced.
  5×8 / 4×8 capacity run was started (logs `tests/runs/b4_people_SA5x8.log`, `b4_people_SA4x8.log`) — check result.
- NEXT: gait element (MOG2 + clean_person_regions + opengait_pretreat in C++ with OpenCV 4.14.0 built like the image:
  baseline SSE3, dispatch to AVX512_SKX, pthreads; source in `tools/opencv/opencv-4.14.0`, not built yet);
  same cadence rule (every 3rd frame with a person). Then face pose (SixDRepNet) + plate/OCR exact elements,
  evidence JPEG, then full test set on .64.
- Turbo on .92 is still OFF (no_turbo=1) for tests: restore with `echo 0 | sudo tee /sys/devices/system/cpu/intel_pstate/no_turbo`.
- Remind user to remove `/etc/sudoers.d/90-admin1-tests` on .64.

## B4 capacity (body in graph): 5×8 FAIL 6.36–6.70 (B3 5.9–6.2); 4×8 FAIL 7.58–7.76 (just under 7.60).
## B5: exact in-graph gait silhouettes (sentinelgait)
- OpenCV 4.14.0 static build like the image's cv2 (IPP 2026.0.0, SSE3 baseline, AVX512 dispatch, pthreads):
  `tools/opencv/install`. C++ port `elements/common/gait_silhouette.hpp`, plugin `elements/sentinelgait/`.
- Parity (tests/parity/gait/result.txt): 200 gait frames, 0 foreground mismatches, 1489/1489 silhouettes identical.
- Patch `image/apply_t2b5_patch.py` (+ person_reid.py precomputed_masks), image `localhost/sentinel-cv:v8-t2-b5`.
- 1×8 PASS 7.97; outputs ≈ B4 (gait 42/43, body 88/87, face 26/28); 2277 silhouettes used, 0 missing.
- Note: a leftover container from an interrupted run held port 18080 and made one run invalid; always
  `podman ps` before a run.

## B5 capacity: 5×8 FAIL 5.97–6.45; 4×8 FAIL 7.52–7.59 (no gain vs B4).
## B6 (in-flight limit, stage queues, scene spread) — `image/apply_t2b6_patch.py`, image v8-t2-b6
- v8 admits max 3 frames in flight per camera (DlsMotionPolicyGate maximum_pending=3); extra are rejected before
  admission. SENTINEL_DLS_MAX_PENDING: 3 → 6.04–6.27; 6 → 6.35–6.54; 12 → 6.40–6.82 (rejections 18); 20 → 6.52–6.88 (0).
- Scene scheduling in v8: each camera independent, no slots (only 1 s start stagger). B6 adds per-camera phase
  (SENTINEL_SCENE_SPREAD=1, crc32(camera) phase of the interval for the first sample, v8 rule after).
- Graph-only test with BGR + gait + body elements: 5×8 PASS in every combination → graph stages are not the limit.
## ROOT CAUSE FOUND (B6 p20 run): input side starves
- Camera threads 58 % waiting for the next frame; graph stage counters show nothing piling up.
- Replay sources managed only 22–26 FPS of 30 (TCP back-pressure, send stalls up to 13 s): frames are not read/decoded
  in real time. In the runtime, every decoded frame (30 FPS/camera) passes Python pad probes in the decoder thread
  (stamp_decoded, fpsgate DlsPtsRateGate, firefpsgate) → each needs the Python lock.
## B7: native exact rate gates (sentinelrategate) — `elements/sentinelrategate`, `image/apply_t2b7_patch.py`, image v8-t2-b7
- Parity: identical accepted frames vs DlsPtsRateGate in 5 scenarios (tests/parity/rategate/result.txt).
- Remaining per-decoded-frame Python probe: stamp_decoded (edge received time) — next if needed.
- B7 result: 5×8 FAIL 6.67–7.08 (sources still 24.5–25.7 FPS).
## B8: native decode stamp (sentinelstamp, in the sentinelrategate plugin) — `image/apply_t2b8_patch.py`, image v8-t2-b8
- Stamp attached natively at decoder src; Python records after the gates (primary records into clock in PTS order on
  one thread; fire path read-only). UTC sanity PASS (3091 checked, 0 invalid, 0 future). 1×8 PASS.
- 5×8 TCP: FAIL 7.00–7.27. Progression 5.90 → 6.36 → 6.67 → 7.00 (slowest camera).
## RTSP check (local mediamtx `tests/rtsp5`, 5 × MOT17 looped, rtsp://127.0.0.1:18565/camN)
- B8 RTSP 1×8 PASS 8.00; 5×8 ~3.5–4 FPS: v8 RtspVideoLink adds a Python probe on the rtspsrc pad = per RTP PACKET.
## B9: native RTSP packet trace — overlay_t2b9/rtsp_source.py (SENTINEL_T2_NATIVE_RTSP_TRACE=1), image v8-t2-b9
- Same diagnostic stage ("source.video") via v8's existing NativePadProgress; no Python per packet.
- 5×8 RTSP: 7.04–7.17 (from ~4). This is a PRODUCT fix (real cameras are RTSP).
- Still: camera threads ~51 % waiting for frames from the graph → next suspect gvamotiondetect (not in graph-only test).
- Graph-only with gvamotiondetect + BGR + gait + body: 5×8 and 6×8 PASS → motion detect not the cause.
- GStreamer latency tracer on runtime (B9 RTSP 5×8): videoconvert p50 74 ms (largest native stage) → B10:
  `videoconvert n-threads` (SENTINEL_T2_CONVERT_THREADS) — 5×8 RTSP 6.88–7.14, no gain (tracer inflated it).
- Remaining Python model waits (B9 profile): plate detector 72 %, scene 19 %, gait model 9 % of the lane waits.
## B11: exact in-graph plate detection + OCR (sentinelplate) — `elements/sentinelplate`, `image/apply_t2b11_patch.py`
- Letterbox/resize via OpenCV 4.14 build (resize parity 2000/2000 exact, tests/parity/resize).
- Plate model must be fed like DL Streamer: u8 NCHW RGB + on-device f32 convert + scale 255
  (tests/parity/plate/ppp_probe.py: 60/60 exact; CPU float scaling gave fp16-level differences).
- Parity (tests/parity/plate/result.txt): 1091 vehicles, 664 plates — 0 differences (presence, boxes, conf, OCR logits).
- Python keeps plate_crop_is_usable and OcrStabilizer (should_ocr / note_ocr_submit / observe) via consume_packet;
  OCR device = OCR_DEVICE (default MULTI:GPU,NPU like v8).
- B11 ANPR 1×8 (same conditions): B11 plate_read 105 / plate_outcome 77 vs T1 97 / 96; other events equal.
  Cause: v8 async OCR has a 4-slot queue and silently DROPS crops when busy; B11 reads every detected plate in-frame.
  Per-read OCR is identical (parity); more reads reach the unchanged stabilizer → behaviour change to report.
- B11 5×8 RTSP: 7.14–7.38 and 7.12–7.52 (two runs). Profile: camera threads 65 % waiting for frames, Python model waits 3 %.
  Limit is now native: NPU 73 %, GPU 70 %. sentinelbody / sentinelplate run NPU inference synchronously (1 request).
- NEXT: async multi-request inference inside sentinelbody / sentinelplate (output unchanged), then measure.
## B12: parallel NPU requests within a frame (sentinelbody / sentinelplate) — parity unchanged (0 differences)
- 5×8 RTSP on .92: 7.19–7.56 (worst window 0.81). User chose: test on .64 now.

## .64 T2 campaign (started 2026-11-16 10:37 UTC)
- Image built on .64 FROM exact C18: `localhost/sentinel-cv:v8-t2-full-dls` id sha256:79b0c0fd5963…
  (build context `image/build64_t2`, SHA256SUMS verified on .64). T2 defaults baked: MAX_PENDING=12,
  STAGE_QUEUES=1, SCENE_SPREAD=1 and all T2 flags on.
- Pre-checks: baked unit/policy/validator checks PASS; agent API 12/12 PASS.
- Campaign dir .64:~/Documents/sentry-validation-20261116-t2 (run-id remote64-t2, ledger-b.jsonl), local scripts
  `t2_campaign/` (plan = final T1 plan with image/path/run-id switched, 77 cases), sources from .92 (`t2_campaign/sources`).
- Isolation on (k3s, apexfabric-control, user services stopped; turbo off) — restore with `sudo ./isolate.sh off` on .64.

## .64 T2 main run results (image sha256:14978d1b, 2026-11-16)
Max passing cameras, T1(≈C18) -> T2: S@5 18->20, S@8 13->14, S@10 11->11, SA@5 5->6, SA@8 3->4, SA@10 2->4.
Close to limit (to repeat): SA-4x10 win 0.903, S-20x5 win 0.919, vehicle-SA-4x8-gate0 win 0.901 (repeat A1 0.929 pass).
Role budget: all 5 pass (T1 failed 5identity-5fps, 2identity-8fps, 1identity-10fps).
Vehicle: 4x8 passes gate1+gate0 (T1: only gate1); 5x8 fails both (T1 too). Mixed 8x8 fails (T1 too, T2 better 4.64-5.25 vs 4.29-4.70).
Frozen-six SA-6x5 fails 3.76-3.97 win 0.54 (T1 2.24-2.72 win 0.24).
Live: 15/16 cameras pass 1x8; traffic1 fails live (7.68 win 0.54) and replay (gate1 7.91 win 0.85, gate0 6.87 win 0.51). No C18 traffic1 baseline existed -> c18ref-replay-traffic1 cases queued.
prefetch-pixel-parity: 235 frames, 0 pixel differences; "fail" only because T2 never takes the prefetch path (prefetched_frames=0) -> not applicable.
Follow-up (followup.sh on .64): 5 recovery cases on port 18391 + 2 C18 traffic1 refs, after repeats + 1 h soak.

## Repeats, hour, traffic1 C18 reference (2026-11-16)
- C18 replay traffic1 1x8: gate1 5.95 win 0.45, gate0 5.82 win 0.45 (C18 report: live traffic1 6.00 FAIL). T2: 7.91/6.87 -> traffic1 is a pre-existing limit, T2 better.
- Repeats: vehicle 4x8 gate1 3/3 pass; vehicle 4x8 gate0 2/3 (A2 0.872) -> marginal; vehicle 5x8 0/4; frozen-six 0/2; S-14x8 repeat FAIL 0.858 (first 0.946) -> S@8 stable limit 13, 14 marginal.
- One hour SA 4x8: avg 7.976 (99.7%) all cams, worst window 0.896-0.908 -> FAIL by a hair. C18 ran its hour at 3x8 (PASS 7.925-7.936). Queued T2 3x8 hour (c18match).
- Recovery cases first rerun hit FileExistsError (old output dirs) -> moved to *.port18381, rerunning (runner4.log).

## Campaign closed (2026-11-17)
- Recovery reruns: reconnect, decoder hang, GIL hang, bad first source PASS; missing-PTS FAIL = not applicable (fault on prefetch path T2 doesn't use) -> T2 version owed.
- 1 h all apps 3x8 (C18 point): PASS 7.976, worst window 0.966 (C18 7.93-7.94). 4x8: FAIL by a hair (0.896/0.899).
- Live mixed 6x5 PASS (C18 FAIL 1.57-4.76).
- .64 restored (isolate off: k3s, apexfabric-control, sentry-cv, sentry-cv-ui, tracker-miner active; no_turbo 0). .92 sources stopped, turbo restored.
- Results: t2_campaign/results_64/ (no JPEG/video). Report: report/T2_RESULTS_2026-11-17.pdf (build_t2_pdf.py).
- Open: T2 missing-PTS test; remaining in-graph elements (face angle, scene SigLIP, gait model, evidence JPEG); sign-offs (OCR no-drop, scene spread, body 0.9 s); face track-averaging decision.

## Annotated videos + evidence copy (2026-11-17)
- Evidence JPEGs copied: t2_campaign/results_64/artifacts (12,807 JPEG + metadata.json).
- tests/annotate/annotate.py: same models as runtime (YOLO GPU, zero-term track, sentinelstate, SCRFD, plate NPU, OCR NPU + runtime decode_logits,
  fire NPU with runtime 'balanced' rule 0.65 / 5 in 3 s / clear 10 s). Output tests/annotate/out/*.mp4 (H.264).
  Note: gvaclassify OCR tensors are not readable via gstgva regions in this DLS -> OCR run in Python exactly like runtime.
- FINDING (C18 and T2): runtime fire branch 'vapostproc add-borders=true ! BGRx,320x320' gives PAR 16:9 -> fire model input is STRETCHED,
  not letterboxed as fire_rgb_model_proc.json intends. Adding pixel-aspect-ratio=1/1 gives letterbox (rows 70..249). Accuracy impact unmeasured; needs sign-off.
- Checked and NOT an issue: fire input colour order is correct (diff 3.2 vs 15.8 swapped); the swapped buffer seen after gvadetect is post-inference in-place preproc.
- traffic1: ~45 vehicles and ~25 plate OCRs per frame, mostly conf 0.2-0.4 -> explains its cost; raw OCR garbage like TS17E77777 / TT1EEEEE.

## Fire input study + B14 (2026-11-17)
- fire_compare (tests/annotate/fire_compare/results.jsonl), runtime fire branch, stretch (current) vs letterbox (PAR 1/1), 12 videos:
  fire clips: alarm start/clear times identical in all 4; letterbox lowers fire-class hits on landscape clips (0.64->0.37, 0.30->0.11, 0.25->0.09),
  smoke similar (>=0.65: 0.98->0.90); portrait fire.webm slightly better. No-fire: letterbox removes weak hits on camera01/ch16/MOT/ANPR,
  BUT creates a NEW FALSE ALARM on ch9 (hospital lobby, fire 0.60-0.67 for ~3 s at 27-30 s, alarm at 49 s). -> keep stretched input (default off).
- BUG (C18 and T2): hazards_from_sample keeps fire boxes in 320x320 model space; v8_events._hazards normalises by source frame size ->
  geometry-scoped fire only sees top-left ~0.17 x 0.30 of frame. Contract example uses geometry fire ("fire-monitor" on "parking-zone").
  Harness zone is 0.01..0.99 so tests could not catch it.
- B14 (image/apply_t2b14_patch.py): map fire boxes to source pixels (always); SENTINEL_FIRE_LETTERBOX switch (default 0). Local images:
  v8-t2-full-local (= .64 T2 files on T1 base) and v8-t2-b14-local. Zone test: tests/zone_probe (probe copy with ZONE_POLY env).

## CORRECTION (2026-11-17): the "fire zone bug" was NOT a bug
- Fire packets carry the 320x320 fire frame (surface_stream: frame=_map_bgr_any(sample) / _geometry_only_frame(sample)), so v8_events
  normalises 320-space boxes by 320. With the stretched input, normalised coords == source-frame coords -> zone checks are correct.
- B14 broke it: zone test (tests/zone_probe, fire-clear fixture, 1x5) full-frame zone: T2 3 suspected/3 cleared, B14 0/0.
  Patch renamed image/apply_t2b14_patch.REJECTED.py; v8-t2-b14-local image removed. Bottom/corner zones: 0 alarms on both
  (alarms on this clip come from mid-frame smoke; fire class rarely >=0.65) -> consistent with correct zone handling.
- Consequence: SENTINEL_FIRE_LETTERBOX must NOT be turned on without also changing zone normalisation (pads would shift coords).
- Side observations: fire evidence image (context_frame) is the 320x320 stretched fire frame, not full resolution.
  One local (.92) run of the unchanged T2 image hit a 15 s stall in primarydet (GPU vehicle/person gvadetect, 3 frames in, 0 out);
  watchdog fenced and replaced the worker as designed. Not seen in any of the 92 .64 runs. Watch item.
