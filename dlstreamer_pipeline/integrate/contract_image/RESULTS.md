# Results: sentinel-cv-dls-integrate:v1 (2026-11-18, on .92)

Results below were measured on images built from commit 7732340 (until 5322afcff3da); the image from this commit (her 4d563b5 + this folder) is re-tested at the end. All tests on .92 (Core Ultra, GPU+NPU),
podman, sources replayed locally (`marked-people1080p30.mp4`), sentry_cv's schema-validating capture agent.
Turbo was ON (not isolated): fps numbers here are smoke checks, not capacity results.

## Contract checks

| Test | Result | Evidence |
|---|---|---|
| Deployment shape: uid 10001, read-only root, writable /tmp/sentinel and /plans only | PASS | contract-run2 C0 |
| 1 camera, all apps (SA), 10 fps, 120 s: every window >= 90 % | PASS 9.97 fps, worst window 99.4 % | smoke6.out |
| Scene samples follow the desired interval | Deliberately NOT followed: her every-7th rule kept (user decision 2026-11-18). With a 2 s request: 12 samples instead of 55 in 120 s (smoke6.out). Image ec8d440eb534 (scene rule restored) was built but not re-tested, by request. | smoke6.out |
| Camera 2 outage 30 s: camera 1 unaffected, camera 2 unavailable -> back with new stream session, no restart | PASS (cam 1 9.98 fps, worst window 98.6 %) | outage2.out |
| Camera down at start: worker starts, other camera healthy, ready | FAIL before fix (engine FATAL, restart loop), PASS after | contract-run1-FAILED..., contract-run2 C1 |
| Down camera comes up later -> joins without restart | PASS | C2 |
| New revision -> applied, events carry the new config_revision, no old processes left | FAIL with V8's start-new-first (single-client sources), PASS with stop-then-start | contract-run1-FAILED..., C3 |
| Lower revision / schema-invalid revision -> rejected, last valid keeps running | PASS | C4, C5 |
| Valid revision whose worker cannot start -> last valid restarted | PASS (480 packages with revision 2 afterwards) | C5b |
| Next valid revision after rejects | PASS | C6 |
| SIGTERM -> worker stops via the stop file, exit 0 | PASS (1.7 s) | C7 |
| Every package valid against the contract schemas (agent validation) | PASS 1596/1596 accepted | contract-run2/agent-status.json |
| /healthz, /readyz, /metrics; per-camera state, reason, last_reliable_at | PASS (V8 supervisor code) | probe runs |

## Open items / findings

1. **Frames no longer held:** 26–29 "lazy pixels: frame no longer held by the engine" errors in the first seconds after
   start (warm-up); her code drops that frame's events (nothing wrong is sent). Her README also lists it.
2. **During a rejected start**, the attempted revision sent one `camera_health` (unavailable) per camera with its
   revision number before the rollback.
3. **Revision change gap:** stop-then-start means no events for the restart time (~5–15 s).
4. **/dev/shm:** each camera reserves an 84 MB shared-memory block (about 2 MB used at 1080p). Default container
   /dev/shm is 64 MB; check at high camera counts (in Kubernetes: an emptyDir with medium Memory at /dev/shm).
5. **Many track_lost / presence_ended_unknown events** (about 1 per object_present) with her tracker setting
   (`zero-term-imageless`): quality, not format. To check on .64 with the accuracy fixtures.
6. **Face embeddings:** only 3–4 per camera per run (face every 43rd frame, her policy). The match between her
   C alignment + AdaFace and the `adaface-ir101-v18.1` profile preprocessing is not measured.
7. **Capacity not measured** on .64 (isolated, turbo off) yet; her 20 x 10 fps claim used her own policy and a
   total-fps rule.
8. **Real delivery agent** not used (capture agent: schema validation + simulated durable ack).

## Re-test of image ec8d440eb534 (her every-7th scene rule restored), 2026-11-18

| Test | Result |
|---|---|
| Contract cases C0–C7 (`tests/contract-run3`) | 10/10 PASS; 0 packages rejected by the agent; no FATAL/Traceback |
| Desired-state inputs on one running container (`tests/inputs-run1`): revisions 1–4 with scene interval 2 s, 30 s, 60 s, 2 s + changed zone names | 4/4 PASS: each applied (6–15 s), both cameras healthy, packages carry the new config_revision only, 0 engine errors, 0 of 5299 packages rejected |
| Scene spacing measured per revision | 2 s requested -> every 10 s (her rule); 30 s -> 30 s; 60 s -> 60 s |

## Open items tested, image ec8d440eb534 (2026-11-18, .92)

### 1. Delivery agent under faults (`tests/faults-run2`, `delivery_faults_test.py` + `fault_agent.py`)
No real delivery-agent implementation exists here (the sidecar is separate); a schema-validating stand-in replied
429, 503 and "no reply" (hang > 30 s) while her engine ran 2 cameras.
| Check | Result |
|---|---|
| Retries resend the exact same package (36 faulted packages) | PASS, 0 changed bytes |
| Retry delays 250/500/1000/2000/4000 ms, max 6 attempts | PASS (measured 0.25/0.5/1.0/2.0/4.0 s) |
| At most 4 handoffs in flight | PASS (exactly 4 held open during the hang phase; raw agent counter first said 8 = test-agent artefact, see results.json note) |
| Given-up packages reported, never accepted later | PASS (24 exhausted, counted in scheduler status) |
| Cameras stay healthy while delivery fails | PASS (70 samples) |
| Delivery resumes; no 409 / invalid / fatal | PASS (675 accepted in the last phase) |
First run (`faults-run1-INVALID-agent-bug`) invalid: bug in the test agent.

### 2. Face vectors vs the contract profile (`tests/faces-run1`, `face_reference_check.py`)
57 faces from the ChokePoint video. V8's reference face code (b645c04 `openvino_face.py`, unchanged) recomputed each
vector from the evidence crop: cosine her vs reference median 0.981, min 0.922 (different packages: median 0.01).
Labels identical to V8 (AdaFace-IR101-INT8 / adaface_ir101_int8-v1 / aligned112-rgb-v1). Exact preprocessing IDs are
still an open item in the contract itself (openBeforeRelease).

### 3. Event meaning (`semantics_check.py` on 4192 observations) and ground truth (MOT17, `tests/accuracy-people`)
Contract rules S1–S8 (presence ids/sessions, no zone_exit on track loss, nothing after presence_ended_unknown,
entry/exit pairing, no zero count while down, new session only after healthy, plate bbox/evidence): all PASS.
MOT17-02 marked fixture, same probe settings and scorer as T2 (1 cam, 8 fps, SA):
| Measure | her image | T2 |
|---|---|---|
| person box precision | 0.79 | 0.82 |
| people found per loop | 0.38 | 0.43 |
| presence ids per real person | 10.8 | 8.6 |
| presence purity | 1.00 | 0.73 |
| people count MAE (>=50 % visible) | 5.75 | 5.25 |
| line crossings matched (of 96) | 24 | 6 |
Throughput 7.98/8 fps. The many track_lost come from track splits (10.8 ids per person), not from wrong events.

### 4. Deployment limits (`tests/limits-*`, --cpus 12 --memory 14g, default /dev/shm 64 MB, read-only)
| Cameras (8 fps default) | Result |
|---|---|
| 8 | all healthy, 7.95 fps each, 4.9 GB, /dev/shm 17 MB, 16914 packages valid |
| 20 (run 1) | **GPU HANG** (i915 context reset in the shared detector thread `min12:src`) 10 s after start; engine stayed at ~0.5 fps for 4 min, no self-restart (her stall check needs exactly 0 fps); /healthz showed 17 cameras degraded |
| 20 (run 2) | no hang; 134 of 160 fps (per camera 4–10.5, median 7), 8.9 GB, /dev/shm 42.8 MB, 30144 packages valid; all cameras "healthy" (shortfall visible only in the fps gauges, as in V8) |
/dev/shm grows ~2.1 MB per camera: the default 64 MB runs out at about 28 cameras (contract hard cap 50) -> a pod needs a
larger /dev/shm (emptyDir medium: Memory) for > ~25 cameras.

### 5. Camera source types (`tests/camtypes-run3`)
| Source | Runs | Outage 30 s -> back with new stream session, others unaffected |
|---|---|---|
| rtsp:// | PASS | PASS |
| http:// | PASS | PASS |
| tcp:// | PASS | PASS |
| rtsps:// (self-signed certificate) | **FAIL**: rejected by certificate validation (plays with validation off). Not a contract item; V8 has a "legacy-compatible" camera TLS mode that accepts it | — |
Also found: the start-up reachability check opens and closes a TCP connection; a `tcp://` source that serves only one
client (sentry_cv's marker_sender) is then refused. RTSP/HTTP cameras are not affected.

## Final image ba4f35e1cc62 (2026-11-18 evening, after user feedback)

Changes since ec8d440eb534:
- Camera TLS without CA matching (user decision): rtspsrc tls-validation-flags=0, souphttpsrc ssl-strict=false.
- Shared memory as V8: /dev/shm when it holds every camera's blocks + 8 MiB, else files in /tmp/sentinel/shm
  (SENTINEL_SHARED_MEMORY_DIR overrides). Her per-camera reservation is ~100 MB virtual / ~2 MB used.
- OpenVINO cache of the camera processes in /tmp/sentinel/ov_cache/events (was /tmp/ov_cache: outside the contract's
  writable mounts), one shared folder, compiles take turns (file lock): fixes the 32-camera cache race without a cache
  copy per camera (per-camera copies cost ~5 GB of RAM at 32 cameras on a RAM-backed /tmp/sentinel).
- TMPDIR=/tmp/sentinel/tmp.
- One camera's event process dying restarts only that process (max 5; her code stopped every camera).
- Stall check: output < 5 % of requested fps for 60 s also stops the worker (GPU hang case).
- Launcher restarts a worker that exits on its own (last valid revision, backoff 5 s -> 300 s); quiet during revision
  changes/stops; a first revision that never started is still retried.

Deployment shape used by all final tests (as a read-only Kubernetes pod): read-only root and /tmp, writable
/tmp/sentinel, /plans and a 64 MB /dev/shm, uid 10001.

| Test (image ba4f35e1cc62) | Result |
|---|---|
| Contract C0–C9 (`tests/contract-final`): shape, camera down at start, late join, revisions, rejects, fallback, one camera process killed, worker killed, stop | 12/12 PASS, 0 invalid packages |
| Camera types (`tests/camtypes-final`): rtsp, rtsps (self-signed), http, https (self-signed), tcp; 30 s outage each | 10/10 PASS |
| 32 cameras, 14 GiB / 12 CPUs, default /dev/shm (`tests/limits-32cam`) | shared memory OK (56 MB in /tmp/sentinel/shm, <1 MB /dev/shm); 9.5 GB RAM; 2700 threads; **GPU hangs** in the shared detector (i915, `minNN:src`) after ~20 s -> stall check -> worker restarted 3 times |
| 32 cameras, no CPU/memory limit (`tests/limits-32cam-nolimits`) | 2 GPU hangs, recovered by restarts, then stable ~110 main + 63 fire fps (of 256 requested). Package counts invalid in this run (test agent did not start: socket path too long) |

Findings still open:
- GPU hang of her shared GPU detector (DL Streamer va-surface-sharing, one model instance for all cameras) on .92:
  1 of 2 runs at 20 cameras, every run at 32. The image now recovers by itself; the cause is in her engine / DL Streamer /
  i915 driver, not in the contract layer. Needs a test on the target box (.64) and possibly a driver/DLS update.
- Thread count ~85 per camera (one Python event process per camera): podman's default --pids-limit 2048 stops at
  ~24 cameras; Kubernetes pods normally have no limit.
- Memory ~0.3 GB per camera: about 30 cameras fit the contract's 14 GiB limit.
- Her worker segfaults (-11) while shutting down on a revision change (packages not yet acknowledged are lost, which the
  contract's crash rules allow).
- Start-up reachability check consumes a connection of single-client tcp:// sources.

## Per-camera revisions, image a16042a8f4d0 (2026-11-19, .92)

User decision: a new desired-state revision must touch only the cameras it changes. Before, every revision restarted
the whole worker (all cameras). Now (`dls_entrypoint.py` + `apply_contract_patch.py` REVISION_CODE): same deployment
and same camera list -> the running engine applies each camera's change itself, all changes of one revision in one pass:
- zones / lines / bindings / scope / object types / scene interval: live, her `V8EventPipeline.reconfigure()` (V8's own
  live path); nothing restarts. A camera whose zones/lines changed reports `degraded / view_changed` for a moment (V8 code,
  contract `sceneViewChanged`); its video does not stop.
- fps or source: only that camera's source is rebuilt and its event process restarted (new stream session).
- embedding profiles: only that camera's event process restarted.
- camera added / removed / reordered, deployment changed: whole worker restart (as before).

`tests/revision-run3` (`revision_per_camera_test.py`, 3 cameras, read-only container): **6/6 PASS**
| Step | Worker | Event processes restarted | Sources rebuilt | Other cameras | Packages after |
|---|---|---|---|---|---|
| R2 zone of camera 1 | same pid | none | none | healthy | revision 2, 0 invalid |
| R3 fps of camera 2 (10 -> 5) | same | camera 2 | camera 2 | healthy | revision 3 |
| R4 camera 3 drops gait profile | same | camera 3 | none | healthy | revision 4 |
| R5 one revision: zone camera 1 + fps camera 2 | same | camera 2 | camera 2 | camera 3 healthy | revision 5 |
| R6 invalid revision | same | none | none | healthy | revision 5 kept |
| R7 4th camera added | restarted (expected) | all | — | 4 healthy | revision 6 |
Each applied in 2 s (R7: 6 s). 0 engine errors; 20 known "frame no longer held" warm-up messages after R7's restart (open item 1).
Earlier runs: `revision-run1-INVALID-duplicate-use-case-ids` (test copied use-case ids; image rejected it correctly),
`revision-run2-TEST-BUGS` (log slicing, view_changed counted as failure, old start-up log counted).
The shutdown segfault now only happens when cameras are added/removed.

## Re-test of image a16042a8f4d0 (per-camera revisions), 2026-11-19, .92
| Test | Result |
|---|---|
| Contract C0–C9 (`tests/contract-a1604`) | 11/12 as run; C3 "failed" only because the test still required a worker restart for a zone-name change. The run shows the new rule working (same processes, packages carry revision 2); C3 updated to the new rule in contract_tests.py |
| Camera types (`tests/camtypes-a1604`) | 10/10 PASS |
| Per-camera revisions (`tests/revision-run3`) | 6/6 PASS (section above) |
| Face vectors vs V8 reference (`tests/faces-a1604`, `face_check_a1604.json`) | 53/53 compared, cosine median 0.976, min 0.928, none below 0.8; labels identical to V8 |

### Faces: her image vs T2 (`v8-t2-full-local`), same harness, same ChokePoint P1E_S2_C2 video, 2 cameras, 420 s (`face_compare.py`)
| | her image | T2 |
|---|---|---|
| person presences | 173 | 186 |
| face vectors sent | 53 | 134 |
| presences with at least one face | 52 (30 %) | 132 (71 %) |
| faces per presence that got one | 1.02 | 1.02 |
Her image sends about 40 % of T2's faces. Vectors are right; the count is the gap. Likely cause: face detection on 1 frame in 43
(`FACE_INTERVAL=43`, her setting; not passed through by the worker shim, so not changeable without a rebuild). Not proven yet.

## Live camera add / remove, image ad8415b981b5 (2026-11-19, .92)
User decision: adding and removing cameras must happen live without disturbing the other cameras; no fixed number of
spare slots. Now only a changed deployment restarts the worker. How (apply_contract_patch.py REVISION_CODE + handoff_host /
model_server edits, dls_entrypoint.py):
- remove: that camera's source and event process stop; its metrics and ready files are deleted; /readyz and /healthz
  drop it (plan); its GStreamer branch stays idle (no source, like a camera that is down) and is free for reuse.
- add: a free idle branch is reused, or one new branch is built into the running graph (her `branch_parts`, same
  shared model instances: det-shared, scrfdlm-shared, fire-shared; C head-pose/AdaFace models are loaded once), with its
  own shared memory, C reader, handoff connection (sent to the running handoff process; her scheduler's dispatch loop
  re-reads its connections each pass) and model-server reply channel; then its event process starts.
- If an add cannot be done live (e.g. /dev/shm full), the worker restarts with the new revision (FATAL logged).

`tests/revision-live-add1`: **9/9 PASS** (same worker pid throughout R2–R10)
| Step | Result |
|---|---|
| R2–R6 (zone, fps, profile, two cameras in one revision, invalid) | as before, PASS |
| R7 camera 4 added | new branch 3 built; healthy, packages with revision 6 in 4 s; no other camera restarted/rebuilt/unhealthy |
| R8 camera 1 removed | gone from /readyz, /healthz and packages; others untouched; branch 0 idle |
| R9 camera 5 added | idle branch 0 reused; healthy, packages; others untouched |
| R10 one revision: camera 3 removed + camera 1 back + zone of camera 2 | all applied in one pass (~1 s after the supervisor); camera 1 on new branch 4; camera 2 degraded/view_changed only (V8) |
0 engine errors, 0 invalid packages; 5 known "frame no longer held" warm-up messages from the added camera (open item 1).
Re-test of ad8415b981b5: camera types `tests/camtypes-ad84` 10/10 PASS; contract `tests/contract-ad84` 11/12 - only C5b,
which still expected V8's rollback when every camera of a new revision is unreachable. User decision 2026-11-19 (option a):
such a revision stays applied; its cameras are reported unavailable and keep retrying (camera health, not an invalid
revision). C5b rewritten to check that, plus that the cameras rejoin by themselves once their sources start; rerun in
`tests/contract-ad84-r2`.

## Image 5322afcff3da (2026-11-19) - added camera that cannot connect is reported unavailable and retried
| Test | Result |
|---|---|
| Contract C0–C9 incl. new C5b (`tests/contract-5322`) | **12/12 PASS** |
| Per-camera changes + live add/remove (`tests/revision-5322`) | **9/9 PASS**, same worker throughout |
Tagged for push: ghcr.io/kiranmaibattu-cyber/sentinel-cv-dls-integrate:v1-2026-11-19 (separate package, user choice).
Open before production: face count ~40 % of T2 (FACE_INTERVAL=43 likely), GPU hang of her shared detector at 20–32 cams on .92
(auto-recovers), ~30 cams in 14Gi, no .64 run, no real delivery agent.

## Image from this repo: her 4d563b5 (plate evidence fix, her runtime work) + contract_image, 2026-11-19, .92
Built with `contract_image/build.sh` from commit 12c13e2 (b3ba3b94048e) and, with only the OCI source/revision labels
added, from b5dd408 (be38cf854b63, pushed as `ghcr.io/apexflo-labs/sentinel-cv-dls-integrate:v1-2026-11-19-b5dd408`,
digest sha256:6fe263f5a3b06f1b5b54cb7677c82be236c968f909d71a480af94e46588221c4).
| Test | Result |
|---|---|
| Contract C0–C9 incl. C5b (all cameras unreachable -> applied, unavailable, retrying, rejoin) | **12/12 PASS** |
| Per-camera changes + live add/remove (`revision_per_camera_test.py`, R2–R10) | **9/9 PASS**, same worker throughout |
| Camera types rtsp / rtsps / http / https / tcp, outage and rejoin | **10/10 PASS** (run on be38cf854b63) |
Not re-run on this commit: faces (vectors 53/53 vs V8 reference on 5322afcff3da; count ~40 % of T2), delivery faults,
limits. Her plate fix is in the image but was not measured here (no plate fixture in these tests).
Open before production: face count vs T2, GPU hang of the shared detector at 20–32 cameras on .92 (auto-recovers),
~30 cameras in 14Gi, no run on .64, no real delivery agent.
