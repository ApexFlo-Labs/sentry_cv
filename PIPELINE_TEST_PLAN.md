# Sentry V8 forward test plan and edge-case matrix

Prepared 2026-11-05; updated 2026-11-06 after the TLS and queue/reconnect incidents. This is the cumulative regression catalog. It includes historical partial execution and explicit missing coverage. It adds explicit expected and forbidden behavior to the earlier broad [isolated plan](ISOLATED_VALIDATION_PLAN.md); it does not rewrite that historical campaign or claim new test results.

**282 named cases in 28 areas. No tests are executed by creating this plan.** The cases are scenarios, not 282 separate long-running jobs: one well-instrumented fixture can cover several IDs. Results must identify the exact assertions it covered. No finite list certifies every possible camera or environment.

Historical isolated-campaign reference: `ghcr.io/kiranmaibattu-cyber/sentinel-cv-runtime:intel-285h-2026.11.05-v8-qaft-int8-tlsfix2`, registry digest `sha256:b5a5dc68db4cba9e2b18ec28c61175686189161b25f605b298c1241dbc4957fc`, config ID `dbaeacc92973fc5d1e41521e3200cd64151aaa53a472404a79f87833bade3ef5`. Desired state remains V8/8.0, remote wire 7.0, local agent 1.0. The user's default TLS choice is **legacy-compatible**: encryption without certificate verification; strict mode is optional. Historical policy filenames/profile names containing V6/V18.1 do not select an old wire contract.

Machine-readable case definitions: [PIPELINE_TEST_MATRIX.json](PIPELINE_TEST_MATRIX.json). Historical execution ledger: [PIPELINE_TEST_LEDGER.csv](PIPELINE_TEST_LEDGER.csv). The ledger preserves the earlier campaign’s actual verdicts and partial coverage. Every NEW candidate’s incident results begin `NOT_RUN`; historical evidence remains reference material, not a fresh pass. No row is automatically marked PASS because96 tests or an earlier hour passed.

Capacity is explicitly tested at **5, 8 and 10 FPS per camera**, with scene search on every camera and named uniform/mixed use-case bundles. See [capacity protocol](CAPACITY_TEST_PROTOCOL.md), [profile definitions](CAPACITY_TEST_MATRIX.json) and [empty capacity results table](CAPACITY_RESULTS_TEMPLATE.csv). No camera limit or use-case budget is prefilled.

## Required pipeline test suites — complete index

This is the concrete required scope for the current Sentry V8 image: shared DL Streamer execution, VA decode, built-in motion, native GVA tracking, camera-local policy and complete packages to the delivery-agent API. The 28 suites below map to all 282 defined cases in section 6. This review expands existing cases for subject-crop semantic vectors, binding ownership changes and scene-trigger filtering; it does not invent additional event types. Current normative definitions are in this plan/JSON; original historical CSV fields remain an execution snapshot. Neither a listed suite nor an enabled app means that test has passed. Missing fixtures and open defects remain in the incident index and historical coverage.

| Suite / exact case range | Tests required |
|---|---|
| **Image and release** — `REL-01`…`REL-08` (8) | Exact digest pulled; baked imports; effective environment; embedded/canonical schemas; model/proc/plugin/policy hashes; clean build/pull; immutable tags; harness failures cannot count as image passes. |
| **Kubernetes and startup** — `DEP-01`…`DEP-10` (10) | Actual non-root Pod/mount/UID/device layout; absent/denied GPU or NPU; HTTP alive with failed worker; bad first source; all sources unavailable; read-only root; cgroup limits; cold/warm starts; graceful stop; two deployments on one box. |
| **Streams and VA decode** — `NET-01`…`NET-09` (9) | RTSP/RTSPS H264 and HEVC; TCP MPEG-TS H264; unsupported TCP codec; five consumers of one URL; bad DNS/port/path; 401 authentication; HTTP capability mismatch; network gaps, malformed media, resolution changes; encoded credentials/IPv6; actual VA path without CPU fallback. |
| **TLS modes and certificates** — `TLS-01`…`TLS-12` (12) | Legacy default without CA; self-signed, expired and hostname-mismatched certificates; strict valid/untrusted/mounted-CA cases; invalid mode/CA; projected CA rotation; TLS versions/SNI; failed shared leader; FFmpeg versus GStreamer; real native video, not just a successful TLS handshake. |
| **Desired state and live reconfiguration** — `CFG-01`…`CFG-12` (12) | 1/8/16/50 cameras and over-limit rejection; default/explicit/mixed/fractional FPS; malformed config; missing/unreadable URL Secrets; actual URL switch after revision; projected mount rotation; stale/duplicate/rapid revisions; add/remove cameras; reused/multiple/no zones; enable/disable app; geometry change while occupied. |
| **Every model and device** — `MODEL-01`…`MODEL-10` (10) | QAFT preprocessing/class order/source boxes; declared export parity; real GPU/NPU placement; corrupt/missing files/plugins; batch1 versus request concurrency; inactive batching explicitly unqualified; empty/NaN/wrong-shaped results; exact embedding profiles/dimensions/normalization; versions and precision claims. |
| **Shared inference, queues and retained surfaces** — `FLOW-01`…`FLOW-10` (10) | Late/out-of-order returns; old session/generation; same numeric track IDs across cameras; source surface lifetime; copied/native preprocessing parity; every secondary timeout/cancellation; noisy-neighbor fairness; full admission queues; safe source/group reset; batching/CPU-overlap association and order. |
| **Built-in motion** — `MOT-01`…`MOT-10` (10) | Quiet startup; short motion/wake delay; active-to-quiet release; stationary visible subjects; weak hazard candidates; camera shake/light/rain/shadow/cuts; intentional quiet versus broken inference; active2s/quiet~5s scene behavior; outage/recovery; variable FPS and missing observations. |
| **Native tracking and local recovery** — `TRACK-01`…`TRACK-12` (12) | Real-hit confirmation; short occlusion; below/at/above recovery timeout; ambiguous matches; crossing/overlap/merged detections; reused track IDs; class/confidence jitter; resolution/FPS changes; effective adaptive-to-GVA mapping; camera restart; fixed-detection comparisons; retired association state. |
| **Person and vehicle presence** — `PRES-01`…`PRES-09` (9) | First confirmed event and optional subject-crop SigLIP vector (also without scene_search); binding ownership priority/stability/reselection; unchanged long stay and refresh; lost is not departed; qualified recovery; count-only/identity-only/ANPR-only ownership; no-zone and zone scopes; overlapping bindings; exit/revisit; evidence selection; embedding computation must not manufacture presence events. |
| **Zone membership and transitions** — `ZONE-01`…`ZONE-09` (9) | First appearance already inside; positive entry/exit; boundary/deadband jitter; overlapping polygons and bindings; concave/clipped/invalid geometry; geometry revision while occupied; repeat visits with distinct episodes. |
| **Person and vehicle visible-now counts** — `COUNT-01`…`COUNT-09` (9) | Healthy empty zero; stable increments/decrements; unchanged heartbeat; full frame without zones; separate classes; stale predictions/occlusion/outage; merged/split subjects; overlapping scopes; count-only bindings and revision changes; no cumulative-arrival interpretation. |
| **Presence episodes and dwell facts** — `DWELL-01`…`DWELL-08` (8) | Start from first positive inside observation; observed-inside refresh; milestone evidence only if an exact V8 representation/configuration exists, otherwise that subcase is inapplicable; unknown entry time; brief gap/recovery; long/ambiguous loss; positive exit completion; repeated/simultaneous zone episodes; restart/clock/config changes. Do not invent an independent dwell event absent from V8. |
| **Directional entry/exit crossing** — `LINE-01`…`LINE-08` (8) | Both directions; finite segment versus extension/endpoints; deadband and movement; already on far side; real return after cooldown; gaps/predictions; multiple lines; exact crossing source time/frame/geometry. |
| **Face cascade, pose and AdaFace** — `FACE-01`…`FACE-12` (12) | Visible versus absent face; source-pixel size/confidence/sharpness/exposure boundaries; yaw/pitch/roll and invalid SixDRepNet outputs; original expanded contextual crop; aligned chip provenance; overlapping faces/persons; best-window and improvements; hour-long stay; late result; rejected handoff; view adaptation. |
| **Body ReID** — `BODY-01`…`BODY-08` (8) | Face absent but body usable; original person crop/clipping; quality/size gate boundaries; first and improved samples; similar/overlapping people; out-of-order prefetch; invalid vector; refused/retried delivery; unchanged long stay versus changed appearance. |
| **Gait preparation and embedding** — `GAIT-01`…`GAIT-10` (10) | Stationary/too-short sequence; qualified moving silhouettes; one frame-background update shared across people; every-third-eligible-frame sampling at different FPS; occlusion/mixed masks; loss/recovery; montage equivalence; expired buffers; reconnect/resolution/background reset; improved sequences and pressure. |
| **Vehicle, plate and OCR** — `ANPR-01`…`ANPR-12` (12) | Vehicle and plate crops from the original frame; partial entry; absent/unreadable/low-confidence plate; collection versus emission thresholds; best-window and distinct supporting reads; conflicting text; same-text improvement and correction cooldown; non-Indian text; close vehicles/multiple plates; revisit; late OCR; source-frame/plate-bbox evidence; camera quality differences. |
| **Scene search on every camera** — `SCENE-01`…`SCENE-10` (10) | No detections/tracks; active2s cadence; quiet~5s safety sweeps; full frame/rectangular zone bounds; configured versus unconfigured event triggers, source-event links, scope and cooldown boundaries; duplicate periodic/trigger frame; slow model/pressure; reconnect/PTS reset; matching image/vector; semantic embedding-space compatibility; scene-only versus other bindings. |
| **Fire/smoke temporal branch** — `FIRE-01`…`FIRE-09` (9) | Repeated positives; isolated/weak positives; spatially unrelated candidates; evaluated-negative clear; outage/dropped/unevaluated frames are not negatives; long evaluation gap; reappearance during clear; several regions/restart; independent progress during primary quiet/load. |
| **Evidence and embedding integrity** — `ART-01`…`ART-10` (10) | Complete multipart package; camera/session/frame association; all crop borders/aspect ratios; aligned chip versus UI crop; face/body/gait links; retained best-candidate frame; identical retry bytes; JPEG/model failures; normalized overlays/plate/line geometry; package sizes and simultaneous subjects. |
| **CV-to-agent API boundary** — `AGENT-01`…`AGENT-12` (12) | 201 durable/200 duplicate; lost ACK; 429/503/timeout/slow receiver; terminal errors/malformed ACK; admission refusal without poisoning sent state; priority/coalescing; crash before/after ACK; absent/denied/replaced socket; cutoff accounting; accepted package completeness/size; exactly one delivery owner. No Management storage qualification implied. |
| **Source time and freshness** — `TIME-01`…`TIME-09` (9) | Independent source UTC marker; processing/upload delay; backward/reset/looped PTS; wall-clock change/skew; same-frame model completions; requested/selected FPS; stale backlog despite good FPS; confirmation-window delay; source scene interval versus arrival interval. |
| **Health, metrics and useful logs** — `OBS-01`…`OBS-10` (10) | /healthz,/readyz,/metrics and absent /healthy; worker/scheduler versus per-camera readiness; source outage/stale counters; unwritable/full metrics storage; Kubernetes CPU/RAM scope; actual versus unavailable GPU/NPU metrics; queue/model/stage timings; credential-safe context; summary parity; CV handoff versus agent outbox ownership. |
| **Failure isolation and resource lifetime** — `RES-01`…`RES-10` (10) | Finite queues/surfaces; subject churn; dense scenes; slow/stuck secondary; camera/group/container death; shutdown during decode/inference/JPEG/handoff; CPU/RAM/disk/PID pressure; temperature/power;10min busy pre-soak; staged60min selected-workload stress. |
| **Capacity and use-case budgets** — `PERF-01`…`PERF-16` (16) | Equivalent old/new frames and workload; historical quiet and busy cases separated;5/8/10FPS frontiers; scene on every camera; resolutions/densities/mixed roles; overload and recovery; fairness; repeated boundary/headroom; fixed-count additional-app budget; actual positive work; full latency/resource series; demand=sum(camera FPS). |
| **Deployment-like final validation** — `SITE-01`…`SITE-06` (6) | Independent representative source servers; real config changes reflected in output; semantic positive/negative sign-off; release compatibility/limitations/rollback; smoke through agreed agent API; explicit missing/blocked coverage. |
| **Adaptive gates actually applied** — `ADAPT-01`…`ADAPT-12` (12) | Cold-start default; different views/cameras; independent per-use-case modes; lower/upper bounds; sample minimum/hysteresis/hold; image quality versus queue pressure; resolution/FPS/time normalization; missing/stale/NaN metrics; actual native consumer; absent modalities; pressure/recovery; fixed versus adaptive replay comparison. |

For every confidence, pixel size, blur/exposure, angle, overlap, motion vote, stable-hit, recovery, debounce, candidate-window, consensus, improvement, cooldown, heartbeat, sequence-length, queue-size, timeout and retention gate: use the independently frozen approved effective value and test below/at/above it. Exercise both required and forbidden outputs. Repeat supported adaptive extrema and transitions; never relax gates to improve a benchmark.

Every use-case fixture must cover first qualification, absent/poor input, unchanged long stay, meaningful improvement/change, temporary loss, ambiguous recovery, confirmed exit where applicable, accepted/refused handoff and cleanup. Every resulting image/vector must have the right camera, session, presence/episode, frame/time, geometry and profile. Person/vehicle inputs must be tested separately where thresholds/classes differ. Face/body/gait are independent samples associated locally; cross-camera named identity remains Management work.

Review explicit contract oracles before crediting existing tests. In particular, V8 `runtimeEndpoints.readiness`/`runtimeAvailability` define operational readiness independently of every camera being available. The r2 health tests include a fresh-camera prerequisite. `DEP-06`/`OBS-01` therefore require an explicit contract comparison for the all-sources-unavailable case; the current test expectation alone is not a normative oracle or proof of V8 conformance. No contract/runtime change is made by recording this discrepancy.

Execution is layered: pure deterministic boundary/semantic cases; the same behavior through baked native image and representative sources; deployment-like mounts/devices/permissions; combined failures/load; then capacity frontier and the selected stress point. Fixed incident regressions run together on each frozen candidate. Capacity is reported per FPS/profile/resolution/activity, with scene JPEG+vector on every camera. The search starts at1 and increases through useful counts to the first failure, then refines; config acceptance for50 cameras is not a performance promise.

## Permanent incident regressions: required for each candidate

Current published repair reference: `ghcr.io/kiranmaibattu-cyber/sentinel-cv-runtime:v8-r2`, registry digest `sha256:e8baec1ad054312f0081deb4bf6d34c7c8cc3bfb38516a3582d3d2fed32b4629`, config ID `e5ce6194581be39b0732ec4b8a486cfbea22bb32e76b82338bc1df92bc473bf7`. Its [scoped repair evidence](releases/v8-r2/REPAIR_RESULTS.json) does not qualify every scenario below. The user’s queue reference means the earlier repaired incident, not a newly reported v8-r2 failure.

**Fixing the latest reported issue is insufficient. Every new image must rerun the accumulated regressions on that same frozen image.** A build must exist before its baked code can be tested; building a candidate is separate from qualifying it. No runtime, model, production gate or contract is changed by this checklist.

For each incident, retain the original sanitized error, exact deployed image identity, failed/invalid attempts, minimal reproducer, independently specified required/forbidden behavior, ownership/fault boundary, and evidence needed to prove recovery. Link actual tests and missing fixtures in `incident_regressions` in the JSON matrix. These 28 overlapping incident families organize the existing 282 scenarios; they are not 28 additional executed tests.

1. Freeze the candidate config digest, model/schema/policy hashes, fixture versions and commands. Execute baked runtime code, without mounting replacement implementation code or silently changing dependencies.
2. Rerun the fixed regression set together: TLS/default/strict/auth behavior, repeated source reconnect, full admission queues, native/GIL hang escalation, readiness/freshness, diagnostics and complete event/evidence/vector handoff. A TLS-only pass cannot qualify a reconnect repair, and a queue-only pass cannot establish TLS compatibility.
3. Combine TLS video, two separate interruptions, healthy-peer checks and API artifact checks in the same run. Exercise blocked native inference separately from ordinary camera loss and delivery refusal: these are different queues and fault domains. Add a loaded slow/blocked-secondary fixture; the current primary-callback hang test does not substitute for it.
4. Verify new-epoch frames and complete output after EACH recovery. State-transition acceptance, process existence, historical counters, a telemetry heartbeat or an empty delivery queue is insufficient. Check source timestamps, camera/session/frame associations, evidence bytes and agent acknowledgements.
5. Preserve explicit `FAIL`, `BLOCKED`, `NOT_RUN` and invalid-setup outcomes. Missing tests, unexercised positive paths, stale-image results and known unfixed defects cannot become a production-qualified pass. A scoped debug/candidate report must list what was actually checked and remaining gaps.
6. Run a bounded load smoke on every candidate. For changes to scheduling, models, motion, conversion or recovery, repeat the relevant 5/8/10-FPS frontier and stability measurements. Capacity claims require their actual tested camera/use-case bundle, scene search on every camera, per-camera latency/freshness and resource evidence. A short smoke is not a maximum-capacity or sustained-operation claim.
7. Save results, logs/hashes, exclusions, failed attempts and remaining work in the chronological handoff and candidate evidence directory. Carry incident definitions forward, not previous PASS verdicts. Preserve the historical CSV values when editing or regenerating the catalog; the old ignored `.publish` generator is not the current source of truth.

`IMPLEMENTED_SCOPED` below means saved fixtures/evidence cover a limited repaired scope. It does not mean every listed assertion or deployment variant is covered. `PARTIAL` and `OPEN_TEST_GAP` explicitly require further fixtures or fixes. Full qualification remains blocked while required cases lack passing evidence.

| ID | Reported failure family | Saved coverage status | Remaining scope |
|---|---|---|---|
| INC-001 | HTTP server alive but worker/config/cameras never start | PARTIAL | Actual Kubernetes mount/device/startup parity remains required; schema validation alone is insufficient. |
| INC-002 | RTSPS trust regression and TLS/authentication misclassification | IMPLEMENTED_SCOPED | Connection-only TLS fixtures must be combined with real native video; all transport/certificate variants are not implied by one successful endpoint. |
| INC-003 | Accepted input scheme fails or silently uses another decoder path | PARTIAL | HTTP/HTTPS validator/native-path mismatch is still open; codec/resolution claims need their own fixtures. |
| INC-004 | Reconnect returns success with disconnected RTP pad or broken VA renegotiation | IMPLEMENTED_SCOPED | Live v8-r2 H264 RTSPS scope is saved; repeat codec/input variants claimed by the next candidate. |
| INC-005 | Full native admission queue deadlocks teardown or mixes old work | IMPLEMENTED_SCOPED | Rerun saved fixtures on the new candidate; preserve the documented fault scope. |
| INC-006 | OCR/shared secondary queue saturation starves cameras or never times out | PARTIAL | Saved unit coverage is not a loaded native OCR/secondary fairness test. A deterministic slow/blocked secondary fixture is still required. |
| INC-007 | Native/model/GIL stall survives because the worker process is alive | IMPLEMENTED_SCOPED | Rerun saved fixtures on the new candidate; preserve the documented fault scope. |
| INC-008 | Delivery handoff backlog/rejection/retry hides missing output | PARTIAL | Native admission, OCR queues and delivery queues are separate. Real-agent disk durability/Management remain outside the requested API boundary. |
| INC-009 | Events and vectors arrive without their promised snapshots | IMPLEMENTED_SCOPED | API capture is not a downstream Management image-retrieval test. Subject-crop SigLIP on presence without scene_search is explicitly required by PRES-08 and needs its own saved semantic/native fixtures. |
| INC-010 | Refused face/body/plate admission incorrectly advances sent-quality/dedup state | OPEN_TEST_GAP | Known isolated-campaign failure is not repaired by TLS/reconnect changes. Save a direct refusal-then-accept executable reproducer before closure. |
| INC-011 | Camera/session/presence/frame association breaks across recovery or late completion | PARTIAL | Current recovery/session evidence does not prove all identity-switch/ambiguity cases. |
| INC-012 | Crops use resized/wrong frame, wrong scaling, or confuse face evidence and model chip | PARTIAL | Native source-marker and pixel checks at every claimed input geometry still required. |
| INC-013 | Retired subject/gait/surface state accumulates or survives beyond ownership | PARTIAL | Known historical retention/memory findings remain open; short sparse-video smoke does not establish a leak fix. |
| INC-014 | Count/presence/zone/dwell/crossing semantics change or emit false transitions | PARTIAL | Use deterministic positive/negative/milestone timelines, not just event totals. |
| INC-015 | ANPR emits premature/conflicting plate reads or omits unreadable outcomes | PARTIAL | Calibrated text accuracy is separate; emission timing/outcomes still need labelled semantic fixtures. |
| INC-016 | Scene evidence/embeddings disappear or sampling changes after motion gating | PARTIAL | A quiet five-second safety sweep is not unconditional two-second sampling or busy-camera capacity. |
| INC-017 | Ready/healthy/FPS remain positive while processing is stale or failed | IMPLEMENTED_SCOPED | Telemetry refresh alone is never evidence of source/model progress; unsupported utilization must stay unavailable rather than fabricated. |
| INC-018 | Observation time is future UTC or processing/upload time replaces source time | OPEN_TEST_GAP | Known native 7.5–11-second future timestamp finding is not closed by source-clock unit tests. Preserve independent marked-source reproducer. |
| INC-019 | Accepted revision/Secret change does not switch actual camera input | OPEN_TEST_GAP | Known isolated-campaign Secret/revision failure remains open; API revision alone cannot close it. |
| INC-020 | Telemetry write failure crashes analytics or blocks healthy peers | OPEN_TEST_GAP | Reporting-liveness unit test is not the read-only/full-storage reproducer; prior camera-thread PermissionError failures remain open. |
| INC-021 | Nested diagnostics leak credentials or benign HTTP disconnect looks like CV failure | IMPLEMENTED_SCOPED | Cover native error paths as well as formatter tests; never enable unredacted broad GST_DEBUG on credentialed sources. |
| INC-022 | Model export/classes/dimensions/backend precision differ from the declared profile | PARTIAL | Known QAFT/export and inactive dynamic-route findings remain visible. Common checkpoint/name is not sufficient proof of equivalent outputs. |
| INC-023 | Native startup/membership/shutdown crashes masquerade as source/capacity limits | PARTIAL | Controlled blocked-callback recovery does not reproduce every native initialization or driver crash. |
| INC-024 | Camera-count/FPS/capacity or useful latency claim hides missed scenes and growing backlog | PARTIAL | Run bounded per-image load smoke; full frontier/soak required for capacity/sustained claims or scheduling/optimization changes. Old16/20 quiet-camera results are not busy8/10FPS qualification. |

The JSON index includes exact case IDs, saved test paths, required assertions and evidence levels. A linked test is a starting point, not proof that every assertion has been executed. Known timestamp, Secret/revision, rejected-sample state, telemetry storage and HTTP-native findings remain open; this update does not repair them.

### Evidence audit and runnable fixtures

The [evidence checker](scripts/isolated_validation/check_regression_gate.py) creates a new checklist with all incident results `NOT_RUN`. Its audit rejects missing/failed results, mismatched image config IDs, missing behavioral assertions, missing native/deployment/load evidence and changed evidence hashes. It returns exit code 2 unless every required incident has complete passing records. This is an evidence-completeness check, not an automatic test executor, a guarantee that reports are truthful, or a CI/registry push restriction.

Use a local immutable config ID or published registry digest for the candidate. Choose a NEW output directory per run; do not overwrite old evidence. Example initialization/audit:

```bash
python3 scripts/isolated_validation/check_regression_gate.py \
  --image sha256:e5ce6194581be39b0732ec4b8a486cfbea22bb32e76b82338bc1df92bc473bf7 \
  --init optimization_evidence/CANDIDATE-RUN/incident-results.json

# After executing fixtures and recording their actual assertions/evidence:
python3 scripts/isolated_validation/check_regression_gate.py \
  --image sha256:e5ce6194581be39b0732ec4b8a486cfbea22bb32e76b82338bc1df92bc473bf7 \
  --check optimization_evidence/CANDIDATE-RUN/incident-results.json \
  --summary optimization_evidence/CANDIDATE-RUN/gate-summary.json
```

Each result requires `id`, `image_config_id`, `status`, `run_id`, timezone-aware `executed_at`, exact executed `command`, `assertions_passed`, and `evidence` entries containing `path`, SHA-256 and `level` (`unit`, `native`, `deployment` or `load`). Paths may be relative to the results file. Retain assertion-level output from trusted fixtures; never fill the assertion list simply because a generic harness returned PASS. The checker itself has [offline tests](scripts/isolated_validation/test_regression_gate.py).

Existing bounded actual-image fixtures, to run during candidate validation (not launched by editing this document):

```bash
# TLS negotiation/trust/auth fixtures; does not establish video decoding by itself.
python3 scripts/isolated_validation/camera_tls_qualification.py \
  --image sha256:e5ce6194581be39b0732ec4b8a486cfbea22bb32e76b82338bc1df92bc473bf7 \
  --output optimization_evidence/CANDIDATE-RUN/tls

# Actual live-video recovery, two consumers, repeated TLS interruption and peer checks.
python3 scripts/isolated_validation/v8_recovery_image_validation.py \
  --image sha256:e5ce6194581be39b0732ec4b8a486cfbea22bb32e76b82338bc1df92bc473bf7 \
  --mode reconnect --port 18080 --output optimization_evidence/CANDIDATE-RUN/reconnect

# Separate injected native callback hang and parent/group replacement check.
python3 scripts/isolated_validation/v8_recovery_image_validation.py \
  --image sha256:e5ce6194581be39b0732ec4b8a486cfbea22bb32e76b82338bc1df92bc473bf7 \
  --mode native-hang --port 18080 --output optimization_evidence/CANDIDATE-RUN/native-hang
```

These native fixtures require Intel devices, available test streams and free fixture ports. Execute sequentially and record contention/prerequisites; unavailable sources mean BLOCKED, not image success/failure. Their capture API verifies package schemas, hashes and associations; it does not establish real-agent disk durability or Management storage. It is the user-requested delivery boundary. Unit, policy, deployment and load evidence must be recorded separately; these commands are not the entire 282-case suite. The existing isolated-host campaign and its remote host remain deferred.

## 1. What must change in our testing

The release verdict has five independent parts: input/deployment compatibility; use-case semantics; artifact/association/time correctness; failure/recovery/delivery behavior; and useful sustained performance. A schema pass, a received JPEG or a healthy camera cannot substitute for the other parts. An enabled use case with no qualifying observations has not exercised its positive output path.

The expected result must come from the approved requirement/policy and a separately prepared fixture oracle. Do not call the same production function to calculate the expected answer. Where the policy document, current code and user's expectation disagree, record a decision/blocker before calling the code correct. This checklist changes no contract, model or production gate.

For every use case, explicitly assert all five: **what to emit, when to emit, how often, which camera/presence/episode/frame it belongs to, and what must remain absent.** Every promised image/vector must be checked as part of that same result.

## 2. Priority and run strategy

- **P0:** correctness/compatibility gate for a claimed or enabled production capability. Unexplained failure blocks that capability's production qualification. A candid development candidate can still be published with limited scope and open failures, as before; publication is not production approval.
- **P1:** expanded quality-of-execution, stability, stress or capability coverage. It becomes a gate whenever the release claims that capacity, deployment mode or sustained operating scope. Inactive experimental dynamic batching must remain disabled/unqualified until its own cases pass.
- **Known issue:** prior evidence of failure, not a new test verdict. The TLS repair did not close inherited timestamp, numerical, state-retirement or admission findings.

Execution order:

1. Verify the frozen image, harness and actual pod layout. Exercise default/strict RTSPS,401/404, missing devices/Secrets and failed-first-camera isolation.
2. Run deterministic observation-to-output timelines for every enabled use case. Include gate boundaries, negative cases, gaps, late jobs and refused delivery.
3. Run native fixtures with the same expected semantics and source markers. Classify upstream missed observations separately from wrong downstream policy. Keep labelled recognition accuracy separate; semantic event logic still needs an oracle.
4. Compare old/new paths on identical bytes, configuration, initial adaptive state and load. List approved intentional differences. UUID values may differ; canonicalize identities without discarding source times, counts, links or state transitions.
5. Test faults and race boundaries. A deliberately failed source/model must have the expected negative outcome; do not let a generic capacity flag override the scenario-specific oracle.
6. Search the 5/8/10 FPS capacity boundary with scene on every camera. Use short2–3minute steady holds after measured warmup, refine neighboring counts, and repeat informative passing points. Stop escalating a branch once it fails; do not repeat an unchanged failure without a new hypothesis or repair.
7. At the selected useful operating point, run a10minute pre-soak. Run one genuine60minute steady stress qualification only after the cheaper gates pass and when sustained qualification is the task. Every candidate needs the cumulative incident regressions and bounded load smoke; small fixes do not require rerunning an hour at every camera count. Full sustained claims require their own sustained evidence.
8. Verify the deployed new digest and actual package bytes in the production-like pod. Under the user's current scope the test boundary ends at the delivery-agent API. Do not request Management credentials or claim its storage/search/identity workflows were tested.

Faults/stress are confined to owned test containers, volumes and processes. Record host contention and actual power/cgroup settings; remote192.168.1.64 is not to be used again until the user explicitly authorizes it. No current tests are launched by this plan.

## 3. Minimum record and outcome rules for every run

Each run records: case IDs and exact assertions; image digest/config ID; model/proc/plugin/schema/policy hashes; code import paths; host/device/driver/container/cgroup inventory; all environment overrides; approved effective gate/profile snapshot; source bytes/server/certificate/auth/codec/resolution/FPS; camera/binding/geometry/revision; test command and fixture hash; independent expected timeline; actual complete packages; raw metrics/logs; start/warmup/steady/stop times; verdict and remaining gaps. Sanitize URLs, passwords, tokens and private keys. Record declared measurement windows and uncertainty.

Allowed outcomes: `PASS`, `FAIL`, `BLOCKED`, `NOT_RUN`, `UNSUPPORTED_EXPECTED`, `INVALID_TEST_SETUP`, `EXCLUDED_RESOURCE_ABORT`. A missing prerequisite, model output, positive event or test import is never an implicit pass. A resource guard overrides any cached healthy/raw-harness PASS. Preserve failed setup attempts separately from the corrected retake. Unsupported cases close only the negative capability check, not the claim of support.

Acceptance thresholds are frozen **before** measurement. Proposed throughput/freshness criteria below retain the earlier campaign's values; they are test criteria, not new contract fields or universally agreed customer SLAs. Do not revise them after seeing a result to make it pass.

| Dimension | Required assertion |
|---|---|
| Event semantics | Exact required and forbidden events/state changes on the fixture timeline; intentional IDs canonicalized, associations retained. |
| Gate boundaries | For every confidence/quality/size/angle/vote/time/improvement/cooldown/retention gate, test just below, exactly at and just above the independently frozen approved boundary. Specify metric units, source versus resized pixels, real observation versus prediction, and inclusive/exclusive comparison. |
| Source timestamps | Use independently known UTC/PTS or markers and retain signed error. No future timestamp beyond measured clock uncertainty; no send-time substitution. Burned-in camera time is not automatically the media capture clock. |
| Active camera FPS | For the claimed steady profile: at least95percent cumulative and90percent in each measured10second window per camera, plus an independently specified upper selection bound on known-rate input. Intentional quiet frames are accounted separately. |
| Freshness | Independently marked source first-PES/frame progression to complete agent acceptance: proposed p95≤2seconds and p99≤5seconds, without growing backlog. Report physical capture/encoding exclusions; do not apply scene criteria blindly to an intentionally held best-candidate event. |
| Scene coverage | Active2second target and roughly5second quiet sweeps are assessed per camera, with source sampling intervals separated from API arrival gaps; skipped/coalesced samples have reasons. |
| Complete delivery | Validate all declared multipart parts, IDs, hashes, dimensions/profiles and common-cutoff ACK accounting. Finite pre-ACK exhaustion is visible failure/release behavior, not automatically loss of accepted media. |
| Resource stability | Live state is bounded by active/pending/within-retention owners. Retire expired arrays, candidates and surfaces. Record numeric RSS/PSS/cgroup growth, FD/thread counts and temperature; no unexplained continuing growth or critical-temperature run called a clean production pass. |
| Isolation | One bad source/config does not block a healthy peer; native process/group failures use their real documented fault domain. No global cross-camera identity is inferred from local IDs. |

## 4. A concrete semantic timeline to test

Use a deterministic fixture with a frozen approved profile (`N` stable real hits, declared debounce/window/cooldown/heartbeat values). The example below is an expected-behavior template, not a new choice of numeric gates or event types.

| Input sequence | Expected result | Explicitly forbidden |
|---|---|---|
| Healthy empty view | Correct visible-now count0 according to initial/change/heartbeat policy; scene sampling still works. | New presence, identity vector or synthetic plate. |
| PersonA appears outside a zone, fewer thanN real hits | Internal candidate only where policy requires confirmation. | Premature confirmed presence or crossing. |
| PersonA reachesN qualifying hits | One confirmed presence with correct first-seen/source association; count changes only after its own approved debounce. | One presence event per frame. |
| Qualified body, no usable face and no sufficient gait sequence | Independent body sample after its selection gate; no face/gait sample. | Mandatory face, fabricated vector or wrong person crop. |
| A positively enters the zone | One zone_entry and the right episode after membership/temporal gates. | Boundary-jitter entries. |
| A remains; samples do not materially improve | Only permitted count/presence/episode refreshes and scene samples. | New identity events simply because a cooldown expired. |
| A is occluded briefly | Approved unobserved/lost state and an explicit observation gap. | Zone_exit or completed dwell. |
| A returns with reliable unambiguous association | Continuing presence/episode plus owned recovery transition as specified. | New unrelated presence or mixing PersonB's feature history. |
| A is positively observed outside | One confirmed exit and correct final episode times. | Exit at delivery time. |
| A revisits later | New zone episode for the separate visit. | Continuous dwell across the outside interval. |
| Agent refuses the selected sample, then accepts a later retry | Correct stable package identity where retried, and qualified sample remains sendable until the required acceptance boundary. | Sent-quality state permanently suppressing an unsent feature. |

Every producer needs similar positive, negative, improvement, loss/recovery and delivery-refusal timelines. For native clips, label which observations actually arrived: a detector miss, wrong policy decision and failed handoff are different results, not interchangeable explanations.

## 5. Highest-risk open cases to run before more optimization

The matrix retains these as open/inherited findings, not completed repairs:

- `MODEL-03`: QAFT export/backend numerical fidelity; `MODEL-06` covers the inactive failing dynamic route separately.
- `TIME-01`, `TIME-06`, `TIME-07`: future UTC, over-target/catch-up selection and stale video despite healthy FPS.
- `AGENT-05`, `FACE-11`, `BODY-07`: refused admission must not contaminate sent-quality/dedup state.
- `GAIT-08`, `RES-02`: retired state/history/image retention and memory growth.
- `CFG-05`, `CFG-06`, `CFG-08`: actual Secret/revision reconciliation, projected mounts and safe membership changes.
- `TRACK-09`, `ADAPT-09`: advertised adaptive controls must reach native GVA consumers.
- `OBS-04`, `OBS-08`: telemetry write failures and nested credential-safe logging.
- `TLS-01`, `TLS-08`, `TLS-11`, `TLS-12`: preserve the repaired client defaults and verify the real production endpoint/pod scope rather than only a trusted local source.

No accuracy workaround, lower scene frequency, hidden CPU fallback or changed production gate is an acceptable way to close these execution cases. Accuracy tests can run as a separately labelled workstream.

## 6. Full case catalog

The final column describes prior evidence only. `Current scoped` is not a fresh PASS and cannot expand a narrow fixture into full deployment qualification. The JSON and CSV retain exact references and scope notes.

| Area | Cases |
|---|---:|
| REL — Image and release provenance | 8 |
| DEP — Kubernetes startup and deployment portability | 10 |
| NET — Transport and media connection edge cases | 9 |
| TLS — RTSPS trust, compatibility and security defaults | 12 |
| CFG — Desired state, binding and Secret changes | 12 |
| MODEL — Model preparation, devices and numerical fidelity | 10 |
| FLOW — Scheduling, frame lifetime and shared execution | 10 |
| MOT — Motion gate and sampling state | 10 |
| TRACK — Tracking, recovery and ambiguity | 12 |
| PRES — Person/vehicle presence and lifecycle | 9 |
| ZONE — Geometry and zone transitions | 9 |
| COUNT — Visible occupancy and count emission | 9 |
| DWELL — Episode timing and observation gaps | 8 |
| LINE — Entry/exit line crossing | 8 |
| FACE — Face pose, selection, crops and embeddings | 12 |
| BODY — Body ReID and person crop behavior | 8 |
| GAIT — Silhouette sequence collection and state cleanup | 10 |
| ANPR — Plate collection, best read and explicit outcomes | 12 |
| SCENE — Scene sampling, scope and evidence | 10 |
| FIRE — Fire/smoke temporal and spatial confirmation | 9 |
| ART — Evidence, embeddings and output associations | 10 |
| AGENT — CV-to-agent delivery and producer state | 12 |
| TIME — Observation time and freshness | 9 |
| OBS — Health, metrics and diagnostic truthfulness | 10 |
| RES — Resources, overload, retirement and recovery | 10 |
| PERF — Capacity and old-versus-new comparisons | 16 |
| SITE — Production parity and release acceptance | 6 |
| ADAPT — Adaptive gates, bounded changes and effective consumers | 12 |

### REL. Image and release provenance

| ID | Priority | Scenario | Must happen | Must not happen | Required evidence | Prior coverage |
|---|---|---|---|---|---|---|
| REL-01 | P0 | Pull the exact registry digest on a clean runtime | Config ID, layer identity, model hashes, contract and policy match the declared release; the pull works with the advertised credentials or anonymous access. | A local GHCR-looking tag is not proof of publication; no silent substitution of another local image. | Registry manifest, image inventory and pull log. | Current partial |
| REL-02 | P0 | Run tests against baked runtime code | Record imported module paths and hashes; native tests load the released image's runtime rather than bind-mounted development code. | No passing test against Opt80 or local source presented as a test of the published Opt79-based image. | Module-path inventory and container mounts. | Not mapped |
| REL-03 | P0 | Inspect effective startup defaults | Verify TLS mode, motion/native flags, devices, batch size, request counts and scene cadence from effective runtime configuration. | No relying on an early Docker ENV that a later layer overrides. | Image environment plus live configuration snapshot. | Current partial |
| REL-04 | P0 | Compare canonical and embedded contracts | V8 desired state8.0, wire 7.0 and agent protocol1.0 agree across packaged schemas, examples, validators and actual payloads. | No stale V5/V6 producer or direct CV uploader selected accidentally. | Hash list, validator results and captured packages. | Current partial |
| REL-05 | P0 | Verify all required model and native artifacts | Every referenced model/proc/plugin/native library exists, loads, has the declared hash and has no unresolved dependency. | No unused model path, missing LFS bytes or host-only library needed for startup. | File manifests, plugin checks and dependency reports. | Current partial |
| REL-06 | P0 | Check immutable tag and version reporting | Published digest, labels, health/version responses and documentation identify the same build. | No overwriting a previously measured tag or using a legacy console version as proof of the active producer. | Registry, labels, API and startup log comparison. | Current partial |
| REL-07 | P1 | Rebuild in a clean build environment | Document pinned inputs and any dependency drift; assign a new digest when bytes differ. | No claiming a later apt/pip rebuild is the already-tested image. | Build inputs, dependency versions and resulting digest. | Not mapped |
| REL-08 | P0 | Verify the test harness itself | Known positive and negative fixture self-checks pass; collection/import/setup failures remain visible and excluded from runtime verdicts. | No invalid RTSP mock, missing mount, zero-output shortcut or resource-aborted run counted as PASS. | Fixture self-checks, runner exit codes and preserved invalid attempts. | Not mapped |

### DEP. Kubernetes startup and deployment portability

| ID | Priority | Scenario | Must happen | Must not happen | Required evidence | Prior coverage |
|---|---|---|---|---|---|---|
| DEP-01 | P0 | Start under the actual pod layout | Non-root CV, URL Secrets, desired-state mount and agent socket work with the intended device/plugin/security configuration. | No privileged disposable-cluster result used to qualify a different production device-plugin configuration. | Pod manifest, UID/GIDs, mounts and device access. | Historical/partial; review |
| DEP-02 | P0 | Missing or inaccessible GPU | Startup reports the missing hardware and affected pipeline explicitly while health remains responsive. | No software model/decode fallback or healthy analytics claim. | Device inventory, placement and failure logs. | Historical/partial; review |
| DEP-03 | P0 | Missing or inaccessible NPU | Affected models have explicit failure/degraded status according to the approved startup policy. | No silent CPU execution or vectors labelled as NPU output. | Per-model actual execution devices and health. | Historical/partial; review |
| DEP-04 | P0 | HTTP server starts but worker fails | Readiness, worker state, applied revision and camera count expose the failure with its initialization exception. | A listening HTTP port must not count as a functioning pipeline. | API timeline and full sanitized startup exception. | Historical/partial; review |
| DEP-05 | P0 | Bad first camera with healthy peers | Healthy peers start and produce output; bad camera remains accurately unavailable; startup ownership is released. | One bad source or invalid TLS configuration must not deadlock the shared graph or falsely mark all cameras healthy. | Per-camera first-frame times, peer packages and coordinator state. | Historical/partial; review |
| DEP-06 | P0 | All cameras unavailable at startup | Health remains available; zero fresh cameras and retry reasons are truthful; readiness follows its documented semantics. | No fabricated zero occupancy or ready analytics solely because the process is alive. | Liveness/readiness and per-camera status timeline. | Historical/partial; review |
| DEP-07 | P0 | Read-only root with required writable mounts | Runtime cache, metrics and temporary evidence use documented writable paths under the real pod security settings. | No hidden dependency on writing the image filesystem or root home. | Mount inventory, write attempts and useful outputs. | Historical/partial; review |
| DEP-08 | P0 | CPU, RAM and PID limits in the pod | Read back actual cgroup limits; bounded startup and useful diagnostics under supported limits. | No trusting Podman inspect or a successful quota-reset command without kernel verification. | cpu.max, cpu.stat, memory.max/current and pids counters. | Historical/partial; review |
| DEP-09 | P1 | Repeated cold/warm start and graceful stop | Worker readiness, model warmup, session creation and teardown complete repeatedly without accumulating resources. | No leftover FFmpeg processes, graph threads, VA surfaces or sockets. | Three-cycle lifecycle/resource traces. | Historical/partial; review |
| DEP-10 | P1 | Two deployments on one box | Each deployment retains its own config, cameras, agent socket and package namespace while shared hardware contention is measured. | No cross-deployment state, secret, socket or acknowledgement leakage. | Deployment-scoped output IDs and process/device inventory. | Historical/partial; review |

### NET. Transport and media connection edge cases

| ID | Priority | Scenario | Must happen | Must not happen | Required evidence | Prior coverage |
|---|---|---|---|---|---|---|
| NET-01 | P0 | RTSP H264 and HEVC | Supported codecs deliver real frames through the declared VA decoder and the native graph. | TCP reachability or FFprobe metadata alone must not count as a decoder pass. | Compressed reception, decoder output and actual placement. | Historical/partial; review |
| NET-02 | P0 | TCP MPEG-TS H264 | Source uses the TCP demux/parser/VA path with bounded reconnecting input and no local-file loop flags. | No treating tcp:// as a file or silently selecting software decode. | Source graph and decoded-frame trace. | Historical/partial; review |
| NET-03 | P1 | TCP codec differs from its declared H264 capability | Reject unsupported codec clearly, or verify a separately implemented capability before advertising it. | No claiming general MPEG-TS HEVC support from RTSP HEVC success. | Parser error, capability declaration and source specification. | Historical/partial; review |
| NET-04 | P0 | Five camera IDs consume one source URL | Each connection has independent camera/session/tracker/policy state; diagnose source-server fan-out limits separately. | No shared identity state or source connection limit misreported as GPU capacity. | Connection counters and per-camera outputs. | Historical/partial; review |
| NET-05 | P0 | Wrong scheme, DNS, address, port or path | Classify resolution, connection, timeout and RTSP404 failures distinctly with bounded retry. | No unsupported scheme accepted as a local file; no generic model-failure diagnosis. | Sanitized error domain, response code and retry timeline. | Historical/partial; review |
| NET-06 | P0 | Credentials and source authentication | Valid credentials work; missing/wrong credentials and expired access produce explicit401/403 without exposing secrets. | No treating401 as certificate failure or logging the URL password/token. | Authentication fixtures and sanitized logs. | Historical/partial; review |
| NET-07 | P0 | HTTP/HTTPS claimed versus actual native support | Every claimed input works through the advertised native path; otherwise validation/capability reporting must reject it consistently. | A validator accepting a source that the mandatory native path rejects is not a supported-input PASS. | Validator result, selected graph and first decoded frame. | Known issue open |
| NET-08 | P1 | Media variants and interrupted input | Exercise long GOP, audio plus video, resolution/aspect changes, corrupt/truncated data, jitter and no-IDR startup; preserve supported behavior or explicit rejection. | No audio buffer interpreted as video, uncontrolled buffering, fabricated frames or endless hidden startup. | Negotiated caps, packet/frame progress and source errors. | Historical/partial; review |
| NET-09 | P1 | URL encoding and address edge cases | Handle the declared URI grammar for escaped username/password characters, trailing Secret newlines and supported IPv6 addresses without changing the endpoint. | No shell/GStreamer injection, double-decoding credentials or URL secrets in diagnostics. | Independent parse/connection fixtures and sanitized logs. | Historical/partial; review |

### TLS. RTSPS trust, compatibility and security defaults

| ID | Priority | Scenario | Must happen | Must not happen | Required evidence | Prior coverage |
|---|---|---|---|---|---|---|
| TLS-01 | P0 | Default image with no mounted camera CA | Legacy-compatible default receives RTSPS video from an untrusted/self-signed server without a site CA. | No accidental return to strict defaults after a library or Dockerfile update. | Effective mode, source validation flags and actual received video. | Current scoped |
| TLS-02 | P0 | Default mode with expired or mismatched certificate | Under the explicitly approved legacy behavior, encrypted transport proceeds without server-identity verification; log that mode clearly. | Do not describe this as authenticated camera identity or a valid certificate. | Mode log and controlled expired/hostname fixtures. | Current scoped |
| TLS-03 | P0 | Strict mode with a publicly trusted server | Strict source receives video with validation enabled and no extra site CA. | No silently turning verification off because a camera uses RTSPS. | Validation flags and video packets/frames. | Current partial |
| TLS-04 | P0 | Strict mode with untrusted signing CA | Reject with unknown-CA diagnostics and the correct camera/session IDs; peers remain usable. | No certificate rejection hidden solely as UnsupportedSurfaceSource or empty inference instance. | Certificate flags, bus error, retry reason and peer progress. | Current scoped |
| TLS-05 | P0 | Strict mode with mounted site CA | Load public PEMs before source start and retain system roots; supported server progresses to RTSP/video. | No replacing normal public trust or requiring a model rebuild at each site. | Trust-source log, merged roots and received video. | Current scoped |
| TLS-06 | P0 | Strict hostname, expiry and activation failures | Reject the actual certificate error; do not accept all errors after observing only one flag. | No unknown-CA-only masking assumed to prove hostname or expiry validity. | Controlled certificates and rejection flags. | Current partial |
| TLS-07 | P0 | Invalid mode or invalid CA material | Unknown modes, malformed PEM, unreadable explicit CA paths and private-key material fail visibly. | No typo selecting a weaker mode silently or private key copied into trust evidence. | Configuration error and sanitized logs. | Current scoped |
| TLS-08 | P0 | Shared-bin startup and failed leader | Apply mode/trust/callbacks before attachment to a playing parent; a failed leader cannot hold healthy peers indefinitely. | No camera added before its trust policy is installed. | Source state at configuration, graph order and peer outputs. | Current scoped |
| TLS-09 | P1 | Projected Secret CA mount and rotation | Kubernetes symlink entries load correctly; after the documented restart new trust material is used and old errors clear. | No claim of hot CA rotation on a retained branch unless it is implemented and tested. | Mount generations, trust hashes and reconnect/restart logs. | Current partial |
| TLS-10 | P1 | TLS versions, SNI and termination path | Test supported TLS1.2/1.3, hostname/IP endpoints and the real ingress/proxy route; distinguish handshake errors from RTSP errors. | Legacy compatibility must not imply plaintext fallback or support for every obsolete TLS server. | Negotiated TLS protocol, endpoint route and bus diagnostics. | Not mapped |
| TLS-11 | P0 | FFmpeg succeeds while GStreamer fails | Compare actual clients in the old and new image on the same certificate/server, including server-side handshake evidence. | FFprobe success or merely passing -tls_verify1 must not be accepted as proof certificate verification ran. | Controlled client comparison and server request trace. | Current scoped |
| TLS-12 | P0 | Full native video behind an untrusted TLS endpoint | Run the actual image with legacy defaults, no CA mounted, VA decode and agent packages; strict control rejects the same endpoint. | A successful socket,404 fixture or isolated relaxed probe cannot qualify full pipeline execution. | Native decoder placement, images/vectors/packages and paired strict control. | Current scoped |

### CFG. Desired state, binding and Secret changes

| ID | Priority | Scenario | Must happen | Must not happen | Required evidence | Prior coverage |
|---|---|---|---|---|---|---|
| CFG-01 | P0 | Validate1,8,16,50 cameras and reject beyond the declared maximum | Validator, schema and runtime limits agree; duplicate IDs are rejected. | Configuration acceptance must not be reported as hardware capacity. | Validator output and compiled runtime plan. | Historical/partial; review |
| CFG-02 | P0 | Default, explicit, mixed and fractional requested FPS | Apply the agreed values consistently in the runtime and telemetry, including omitted FPS. | No silently accepting a fractional value then rounding without documented semantics. | Desired versus applied per-camera FPS and gate counters. | Known issue open |
| CFG-03 | P0 | Malformed, missing or unreadable desired state | Report the precise problem; retain last-good state according to the contract and show whether the new revision was applied. | No silent worker death, stale ready flag or claiming an invalid revision applied. | Revision and readiness timeline with outputs. | Historical/partial; review |
| CFG-04 | P0 | Missing, empty, invalid or unreadable URL Secret | Fail the affected camera visibly and keep healthy peers operational. | No URL printed with credentials or file:/ path sent to the decoder as the media URL. | Secret resolution and per-camera status. | Historical/partial; review |
| CFG-05 | P0 | Replace URL Secret with an accepted new revision | Actual source, dimensions and event revision switch to the new input; session/lifecycle follows the approved rule. | No supervisor revision2 while observations and source bytes remain on revision1. | Two visibly distinct sources, package revisions and source markers. | Known issue open |
| CFG-06 | P0 | Projected Secret/config symlink replacement | Observe Kubernetes atomic mount updates through the actual pod layout, not only in-place host writes. | No watcher that misses a symlink generation change. | Projected mount generations and applied changes. | Historical/partial; review |
| CFG-07 | P0 | Older, duplicate and rapidly arriving revisions | Apply only valid ordered revisions; expose last-good and rejected/obsolete changes honestly. | No rollback to an older source or partial mixture of two revisions. | Revision journal and per-package config revision. | Historical/partial; review |
| CFG-08 | P0 | Add/remove camera while peers process | Retire the old branch safely, start the new one and preserve peer progress. | No native longjmp/crash, retained inference owner or old-camera jobs attached to a replacement. | Graph lifecycle, pending jobs and output IDs. | Known issue open |
| CFG-09 | P0 | One geometry reused by several use cases | Emit the correct use_case_id/scope per binding while sharing the intended local presence. | No duplicate polygon required or result attributed to the wrong application. | Desired bindings and canonical package associations. | Historical/partial; review |
| CFG-10 | P0 | Multiple geometries and full-frame/no-zone bindings | Use the configured set of zones; supported no-zone use cases operate on the full frame. | No hidden mandatory zone or geometry cross-contamination. | Binding scope, geometry IDs and frame evidence. | Historical/partial; review |
| CFG-11 | P0 | Enable/disable one use case at runtime | Outputs and relevant local state change as specified; retire pending jobs safely; report any fixed graph cost remaining. | No disabled-use-case events, old vectors or claim that model work stopped merely because output stopped. | Binding revisions, actual call counts and suppressed outputs. | Historical/partial; review |
| CFG-12 | P1 | Geometry changes with subjects already present | Use a pre-agreed episode/first-observed-inside rule without inventing a physical movement. | No fabricated entry/exit solely because the polygon was edited. | Revision-aligned geometry and episode timeline. | Historical/partial; review |

### MODEL. Model preparation, devices and numerical fidelity

| ID | Priority | Scenario | Must happen | Must not happen | Required evidence | Prior coverage |
|---|---|---|---|---|---|---|
| MODEL-01 | P0 | Verify real execution device for every model | Record actual compiled execution devices and VA decode, not only configured hints. | No model or decoder silently falling back to CPU. | Per-model device evidence and native decoder identity. | Historical/partial; review |
| MODEL-02 | P0 | Missing/corrupt model, proc or unsupported plugin | Startup or affected branch reports a precise recoverable/terminal error according to policy. | No empty inference instance treated as successful initialization. | Load errors and worker/camera health. | Historical/partial; review |
| MODEL-03 | P0 | QAFT fixed-batch GPU versus approved export reference | Compare identical tensors on declared runtime versions with frozen tolerances and positive/empty frames; explain count/confidence differences. | Do not classify export/backend numerical drift as merely an accuracy issue or loosen tolerances after failure. | Matched classes/boxes/scores and raw model outputs. | Known issue open |
| MODEL-04 | P0 | Class order, preprocessing and source-coordinate conversion | Verify9-class mapping, color, scaling, padding, NMS and source box transforms on known images. | No assuming two models share class IDs because checkpoint names look similar. | Model/proc hashes and independently calculated boxes. | Historical/partial; review |
| MODEL-05 | P0 | Active static batch1 and request concurrency | Confirm the production graph uses its declared batch and nireq; output ordering stays correct under concurrency. | Eight requests must not be described as batch8. | Effective graph properties and request traces. | Historical/partial; review |
| MODEL-06 | P1 | Experimental dynamic and fixed batch2,3,4,8 | For each claimed enabled route, positive/empty mixed slots produce correct per-image results including partial batches. | Compilation success or output shape alone cannot qualify batching; inactive routes stay unqualified. | Per-slot raw outputs, detections and route-enabled status. | Known issue open |
| MODEL-07 | P0 | Face/body/gait/scene output contracts | Verify actual profile, dimension512/384/4096/768, finite values and required normalization against the loaded model. | No vector dimension inferred only from an old document or changed embedding space under an old profile ID. | Model tensor inventory and wire vectors. | Historical/partial; review |
| MODEL-08 | P0 | Model returns empty, NaN, wrong shape or no tensor | Reject malformed output visibly; keep it out of accepted events and bound its effect on peers. | No zero vector or random fallback emitted as a valid embedding. | Injected fault plus native error evidence where available. | Historical/partial; review |
| MODEL-09 | P1 | Model or runtime version changes | Re-run conversion/parity and selected full-path comparisons on the same inputs. | No carrying an old image's device or accuracy qualification to a new IR/runtime automatically. | Versioned export, plugin and comparison evidence. | Historical/partial; review |
| MODEL-10 | P1 | INT8/FP16/F32 performance claims | Measure numerical behavior and useful full-pipeline throughput alongside actual execution precision. | Smaller weights, UINT8 input, quantizers or a precision hint alone are not proof of an end-to-end speedup. | Graph/runtime precision and matched workload timings. | Historical/partial; review |

### FLOW. Scheduling, frame lifetime and shared execution

| ID | Priority | Scenario | Must happen | Must not happen | Required evidence | Prior coverage |
|---|---|---|---|---|---|---|
| FLOW-01 | P0 | Late/out-of-order inference completion | Deliver per-camera accepted frames in order or discard according to the explicit stale-result rule. | No later frame advancing tracking before its earlier accepted predecessor. | Camera/session/frame/generation traces. | Historical/partial; review |
| FLOW-02 | P0 | Old session or worker generation returns a result | Discard stale results without updating new presence, zones or identity state. | No result attached using only a reused track number. | Session restart with delayed completion. | Historical/partial; review |
| FLOW-03 | P0 | Two cameras have the same track/frame numbers | Keep jobs, surfaces, histories and outputs camera-local. | No cross-camera vector, crop or count contamination. | Deliberately colliding IDs and distinguishable frame markers. | Historical/partial; review |
| FLOW-04 | P0 | Source surface reused before evidence selection | Retain the correct frame until required crop/JPEG generation finishes, with bounded ownership. | No use-after-release, later-frame pixels or stale source dimensions. | Retained-frame lifetime and decoded JPEG markers. | Historical/partial; review |
| FLOW-05 | P0 | No-copy and copied detector paths on identical frames | Compare native preprocessing outputs/detections before interpreting tracker/event differences. | A direct Python detector match does not establish VA preprocessing parity. | Same-frame native and reference detections. | Historical/partial; review |
| FLOW-06 | P0 | Deadline, timeout and cancellation at every secondary model | Bound camera waiting and pending work; stale completion cannot mutate policy after cancellation. | No camera loop blocked indefinitely or orphan result committed later. | Queue/call/timeout spans and cancellation IDs. | Historical/partial; review |
| FLOW-07 | P1 | One busy camera dominates a shared executor | Every eligible peer receives service under the approved scheduling policy. | No good fleet aggregate hiding a starved camera. | Per-owner queue age, work counts and frame progress. | Historical/partial; review |
| FLOW-08 | P0 | Full queues and refused admissions | Apply declared backpressure/coalescing before irreversible state changes; expose skipped work. | No advancing state for a frame/package that was never accepted at its required boundary. | Admission decisions and before/after policy state. | Historical/partial; review |
| FLOW-09 | P0 | Reconnect retains or rebuilds a shared inference branch | Protect shared model owners and peer bins; bind the right new source/session after restart. | No destroying a model still used by peers or attaching to a retired bin. | Graph generations, model ownership and peer continuity. | Historical/partial; review |
| FLOW-10 | P1 | CPU preprocessing overlap and batching experiments | Compare total frame processing, queue waits, output freshness and bounded allocations. | A faster crop/MOG microbenchmark alone is not a higher camera-capacity pass. | Same-workload baseline/candidate stage and end-to-end traces. | Historical/partial; review |

### MOT. Motion gate and sampling state

| ID | Priority | Scenario | Must happen | Must not happen | Required evidence | Prior coverage |
|---|---|---|---|---|---|---|
| MOT-01 | P0 | Cold start in a quiet scene | Initial mode, safety sweeps and release follow the approved policy and still establish source health. | No permanently sleeping camera before its first useful inference. | Votes, mode transitions, raw/selected/admitted counts. | Historical/partial; review |
| MOT-02 | P0 | Quiet to brief motion to active | Wake at the approved vote boundary; measure whether a short event occurs between sweeps. | No claim that a five-second sweep guarantees capture of every shorter action. | Frame-marked transition and wake delay. | Historical/partial; review |
| MOT-03 | P0 | Active to quiet | Release only after the required evaluated negative observations, preserving pending work. | Dropped/unevaluated frames must not count as negative detector observations. | Negative votes, inferred frames and transition timestamp. | Historical/partial; review |
| MOT-04 | P0 | Stationary visible person or vehicle | Positive observations retain the intended activity and presence/dwell behavior. | No sleep or fabricated exit merely because pixel motion stops. | Stationary fixture, detections and lifecycle timeline. | Historical/partial; review |
| MOT-05 | P0 | Raw weak fire/smoke candidate below emission gate | Assert the explicitly approved wake-retention rule and separately assert no unqualified hazard event. | Do not assume no hazard event means no raw model detection, or call continued activity a motion failure without a policy decision. | Raw scores, wake reason and hazard outputs. | Historical/partial; review |
| MOT-06 | P1 | Camera shake, lighting change, rain, shadows and scene cuts | Measure motion votes, workload and false wake duration; adaptation stays within bounds. | No silently lowering final event acceptance to suppress excess workload. | Input changes, resolved motion settings and mode history. | Historical/partial; review |
| MOT-07 | P0 | Decoder healthy but inference intentionally gated | Health distinguishes quiet selection, safety sweeps and real decoded freshness from failed inference. | No unavailable camera because an appsink is intentionally quiet, or healthy inference invented from predictions. | Separate decode, selected, quiet and inference counters. | Historical/partial; review |
| MOT-08 | P0 | Scene output while active versus quiet | Honor the declared active2-second target and roughly5-second quiet safety-sweep behavior with gaps reported. | No unconditional2-second scene guarantee from a quiet-path test. | Per-camera source sample intervals and reasons. | Historical/partial; review |
| MOT-09 | P0 | Outage while quiet and after reconnection | Missing frames produce unavailable/retrying status and correct clock/session resets. | No quiet status concealing a dead source or negatives invented during the outage. | Source disconnect and recovery timeline. | Historical/partial; review |
| MOT-10 | P1 | FPS changes or sparse arrivals alter motion voting | Use the intended time/observation semantics at low, high and variable FPS. | No retention timeout converted using an assumed fixed FPS without verification. | PTS, real observation count and elapsed-time boundary tests. | Historical/partial; review |

### TRACK. Tracking, recovery and ambiguity

| ID | Priority | Scenario | Must happen | Must not happen | Required evidence | Prior coverage |
|---|---|---|---|---|---|---|
| TRACK-01 | P0 | New object with enough real detections | Confirm only after approved stable observations; track and presence ages use real evidence. | Predicted boxes alone cannot create or confirm a new presence. | Injected detections and native tracking metadata. | Historical/partial; review |
| TRACK-02 | P0 | Short occlusion and recovery | Recover the intended local presence when time, motion and available appearance evidence support it. | No unnecessary new presence solely because the temporary track_id changed. | Known observation gap and association decision. | Historical/partial; review |
| TRACK-03 | P0 | Gap just below, at and above recovery timeout | Respect elapsed-time boundaries and documented clock source. | No off-by-one missed-frame assumption changing behavior at another FPS. | Boundary fixture with controlled time. | Historical/partial; review |
| TRACK-04 | P0 | Two plausible recovery candidates | Reject ambiguous reassociation or retain uncertainty according to policy. | No nearest-object merge that contaminates face/body/gait or dwell histories. | Competing candidates, scores and rejected associations. | Historical/partial; review |
| TRACK-05 | P0 | Crossing, overlap and temporary detection merge | Keep local subjects distinct where evidence permits; record unresolved switches separately from delivery correctness. | No dedup rule automatically treating nearby different people/vehicles as the same presence. | Annotated/simple synthetic trajectories and state traces. | Historical/partial; review |
| TRACK-06 | P0 | Track number reused after expiration | Start a new presence and reject the retired subject's delayed model results. | No inherited plate, identity samples, dwell or cooldown from an old occupant. | Reused tracker ID and generation timestamps. | Historical/partial; review |
| TRACK-07 | P0 | Class/confidence jitter and duplicate detections | Apply the approved class/dedup policy without suppressing two real nearby objects. | No class flip reusing incompatible identity or inflated visible counts. | Alternating classes and duplicate/nearby detection fixtures. | Historical/partial; review |
| TRACK-08 | P0 | Variable resolution, FPS and source gap | Association gates reflect their actual intended coordinate/time semantics. | No320-pixel or45-frame assumption silently treated as universal camera behavior. | Resolved parameters and their consuming code paths. | Historical/partial; review |
| TRACK-09 | P0 | Native GVA versus legacy adaptive tracker settings | Prove each advertised adaptive control reaches its real tracker consumer; unsupported controls are declared accurately. | Reporting a resolver value is not evidence native gvatrack uses it. | Parameter-to-consumer trace and controlled behavior change. | Known issue open |
| TRACK-10 | P0 | One camera restarts while another reuses IDs | Reset appropriate local tracking state; peer tracking remains independent. | No global registry keyed only by track ID. | Two-camera restart fixture and presence maps. | Historical/partial; review |
| TRACK-11 | P1 | Fixed-detection tracker comparison | Measure fragmentation, identity switches and recovery against the same reference trajectories. | Do not infer tracking accuracy from output counts or a changed detector workload. | Fixed detections and separate tracking-quality metrics. | Historical/partial; review |
| TRACK-12 | P1 | Retired track association-state cleanup | Remove expired descriptors, cooldowns and candidate maps after the approved retention period. | No memory or identity contamination growing with total historical tracks. | Live object/map counts and repeated subject turnover. | Historical/partial; review |

### PRES. Person/vehicle presence and lifecycle

| ID | Priority | Scenario | Must happen | Must not happen | Required evidence | Prior coverage |
|---|---|---|---|---|---|---|
| PRES-01 | P0 | First stable presence | Emit exactly the qualifying fact and correct source associations under the approved binding. | No event per raw detection frame or before confirmation. | Expected observation timeline and canonical event count. | Historical/partial; review |
| PRES-02 | P0 | Unchanged subject stays for minutes or an hour | Send only approved freshness refreshes and meaningful updates; retain original first_seen semantics. | No new presence ID or embedding event merely because a cooldown expired. | Long fixed-presence timeline with source times. | Historical/partial; review |
| PRES-03 | P0 | Subject lost without positive exit | Emit only the owned unobserved/lost/unknown lifecycle transitions at the correct times. | No invented zone_exit, completed dwell or disappearance interpreted as proven exit. | Lifecycle state and forbidden-event assertions. | Historical/partial; review |
| PRES-04 | P0 | Recovery after lost state | Reconnect and emit recovery only when criteria pass; duplicate suppression follows the same continuing presence. | No re-created first presence or duplicate best sample without its approved reason. | Loss/recovery trace and candidate-state comparison. | Historical/partial; review |
| PRES-05 | P0 | Counting-only, ANPR-only or identity-only binding | Keep needed local association while emitting only the lifecycle traffic owned by enabled presence use cases. | No track_lost/recovered/unknown noise from a binding that does not own presence outputs. | Binding-specific exact event set. | Historical/partial; review |
| PRES-06 | P0 | Full-frame versus zone-scoped presence | Use configured scope, source bbox and episode association; absence of a zone is supported where specified. | No unconditional zone requirement or false in-zone status from frame visibility alone. | Binding/scope and source geometry checks. | Historical/partial; review |
| PRES-07 | P0 | Exit, re-entry and overlapping presence bindings | Preserve or renew presence/episode identifiers according to the declared lifecycle and each use_case_id. For competing eligible bindings, select the presence app before identity/ANPR, then lexical use_case_id at first confirmation; keep that owner while enabled even if a higher-priority binding is later added. Disable the owner and reselect without another first_seen. | No reuse of an ended episode or wrong-binding event attribution. | Repeated visit timeline and association keys. Owner timeline across added, disabled and re-enabled bindings and exact event reasons. | Historical/partial; review |
| PRES-08 | P0 | Presence evidence and optional subject-crop semantic embedding | First/updated evidence follows the declared policy. With siglip2-base-v1 enabled on the owning person/vehicle presence binding, the selected subject crop and matching 768D vector share source provenance and subject presence_id; exercise this without scene_search. With the profile disabled no semantic vector is promised. Heartbeats do not automatically require new evidence or embedding. | No full-scene vector substituted for the subject crop, null/wrong presence association, dependency on a scene_search binding, or missing promised artifact; no forced new crop/vector on every refresh. | Enabled/disabled profile and no-scene-binding fixtures for person and vehicle; model input crop hash, JPEG source bounds, observation/evidence/embedding IDs and subject presence_id. | Historical/partial; review |
| PRES-09 | P0 | Embedding computation versus presence emission | A new face/body/gait vector does not independently create another object_present unless an approved presence fact/update is due. | No model computation or vector availability used as a duplicate presence trigger. | Separate model-call, feature-event and presence-event timelines. | Historical/partial; review |

### ZONE. Geometry and zone transitions

| ID | Priority | Scenario | Must happen | Must not happen | Required evidence | Prior coverage |
|---|---|---|---|---|---|---|
| ZONE-01 | P0 | First observed already inside a polygon | Create the approved inside episode with unknown entry time; do not invent an outside-to-inside transition. | No fabricated zone_entry or dwell time before observation. | Initial inside fixture and episode metadata. | Historical/partial; review |
| ZONE-02 | P0 | Confirmed outside to inside transition | Apply the approved point rule, margin, hold and votes, then one entry for the binding/episode. | No event when a bbox merely touches the polygon if that is not the approved rule. | Independent geometry oracle and transition timeline. | Historical/partial; review |
| ZONE-03 | P0 | Confirmed inside to outside transition | Emit one positive exit and close the correct episode using source time. | No track-loss timeout substituted for observed outside evidence. | Exit frame, episode and forbidden unknown-as-exit check. | Historical/partial; review |
| ZONE-04 | P0 | Boundary jitter and partial overlap | Exercise positions on both sides, on the edge and within the deadband at different FPS. | No repeated entry/exit storm from bbox jitter. | Known coordinates and exact transition counts. | Historical/partial; review |
| ZONE-05 | P0 | Overlapping polygons and several bindings | Keep independent zone episodes while sharing the appropriate subject presence. | No one zone's state/cooldown suppressing another zone's valid event. | Per-zone/per-binding event and state maps. | Historical/partial; review |
| ZONE-06 | P0 | Nonrectangular, concave and clipped geometry | Apply the documented normalized-coordinate and membership semantics using source dimensions. | No detector-input coordinates or axis swap used as source geometry. | Independently calculated membership and source overlays. | Historical/partial; review |
| ZONE-07 | P0 | Invalid/degenerate geometry | Reject or explicitly handle out-of-range, duplicate, zero-area and unsupported polygon shapes according to schema/capability. | No crash, inverted box or silent scope expansion. | Validation negatives and safe error status. | Historical/partial; review |
| ZONE-08 | P1 | Geometry revision while occupied | Apply the approved config-change episode rule and tag outputs with the actual new revision. | No false physical transition caused only by editing coordinates. | Old/new polygon, episode and revision history. | Historical/partial; review |
| ZONE-09 | P0 | Revisit after positive exit | Use a new zone_episode_id and separate dwell start for the new visit. | No continuous dwell spanning two visits. | Two complete visit timelines. | Historical/partial; review |

### COUNT. Visible occupancy and count emission

| ID | Priority | Scenario | Must happen | Must not happen | Required evidence | Prior coverage |
|---|---|---|---|---|---|---|
| COUNT-01 | P0 | Healthy empty frame or zone | Produce visible_now zero only with healthy evaluated coverage, following change/heartbeat rules. | No zero interpreted as a cumulative-arrival total or sent as evidence of an unavailable source. | Coverage and exact count timeline. | Historical/partial; review |
| COUNT-02 | P0 | Stable increase and decrease | After approved debounce, send the new total and correct scope/use_case_id. | No increment-only behavior or sending the delta as the total. | Known occupancy sequence including0,1,3,2,0. | Historical/partial; review |
| COUNT-03 | P0 | Unchanged count | Suppress repeated unchanged measurements except the configured heartbeat. | No per-frame duplicate count events. | Expected event times and absence checks. | Historical/partial; review |
| COUNT-04 | P0 | No zone configured | Supported count binding counts the full frame with the appropriate null/full-frame scope. | No failure or unintended exclusion because zone_id is absent. | Full-frame binding and known detections. | Historical/partial; review |
| COUNT-05 | P0 | Separate person and vehicle counts | Map all enabled vehicle classes correctly and count each qualifying subject once per scope. | No bicycle/person/class-map confusion or person/vehicle totals mixed. | Class-labelled fixture and per-type totals. | Historical/partial; review |
| COUNT-06 | P0 | Tracker predictions, occlusion and outage | Use approved recently-real visibility and coverage semantics; report unknown/unavailable coverage rather than fabricated healthy zero. | No stale tracker predictions inflating visible occupancy indefinitely. | Real/predicted markers and freshness boundaries. | Historical/partial; review |
| COUNT-07 | P0 | Two objects merge or dedup then separate | Assert the expected total through temporary ambiguity and stabilization. | No a duplicate suppression rule permanently losing a legitimate subject. | Controlled overlapping boxes and stable output total. | Historical/partial; review |
| COUNT-08 | P0 | Multiple/overlapping scopes | Each bound count is correct; full-frame count does not sum overlapping zones as unique people. | No double-counting the same object in a supposedly unique total. | Per-scope independent count oracle. | Historical/partial; review |
| COUNT-09 | P0 | Count-only and runtime reconfiguration | Emit only required measurements/health; reset or migrate debouncing and heartbeat per the approved config rule. | No unrelated identity/lifecycle output or old-scope count after revision. | Exact binding-specific package set and revisions. | Historical/partial; review |

### DWELL. Episode timing and observation gaps

| ID | Priority | Scenario | Must happen | Must not happen | Required evidence | Prior coverage |
|---|---|---|---|---|---|---|
| DWELL-01 | P0 | Confirmed inside observation starts dwell episode | Use source observation time and the correct presence/zone/episode; preserve unknown physical entry where applicable. | No processing/send time used as entry or elapsed dwell invented before first observation. | Controlled UTC/PTS and episode facts. | Historical/partial; review |
| DWELL-02 | P0 | Subject remains and refreshes | Update last_confirmed_inside and the approved sparse presence/episode facts so elapsed/observed dwell can be derived. | No claim of dwell support from a start timestamp that never receives required observation updates. | Start, refresh and inside-confirmation timeline. | Historical/partial; review |
| DWELL-03 | P0 | Configured milestone evidence or status | Identify the exact supported V8 representation and configuration before expecting milestone evidence; if absent mark the milestone-specific subcase inapplicable, not a pass or required new event. Continue testing presence zone-episode start/last-positive/gap/exit facts under the other DWELL cases; Management derives dwell thresholds. | Do not invent a new dwell event type or require an unsupported milestone message. | Frozen contract-supported expected payload/timing. | Historical/partial; review |
| DWELL-04 | P0 | Brief loss followed by confident recovery | Preserve episode continuity only under approved recovery and expose the observation gap. | No gap silently counted as positively observed occupancy or automatic timer reset. | Gap interval, reassociation and episode ID. | Historical/partial; review |
| DWELL-05 | P0 | Long loss or recovery ambiguity | End as unknown or retain uncertainty according to policy; do not claim a confirmed departure. | No timeout-completed dwell or merge with another subject's stay. | Unknown ending and forbidden exit/completion assertions. | Historical/partial; review |
| DWELL-06 | P0 | Positive exit completes a stay | Complete the correct episode at the positive source observation time and stop its active updates. | No completion at upload time or later heartbeat. | Exit frame, episode times and final status. | Historical/partial; review |
| DWELL-07 | P0 | Repeated visit or simultaneous zones | Maintain separate episodes and durations for visits/zones, even with one local presence. | No combined dwell across visits or one zone overwriting another. | Independent episode timeline oracle. | Historical/partial; review |
| DWELL-08 | P0 | Config change, restart and source clock reset mid-stay | Apply the explicit lifecycle/session rule without fabricating continuity or silently losing uncertainty. | No dwell carried across incompatible sessions solely by reused tracker ID. | Revision/session boundary and expected retained/ended episodes. | Historical/partial; review |

### LINE. Entry/exit line crossing

| ID | Priority | Scenario | Must happen | Must not happen | Required evidence | Prior coverage |
|---|---|---|---|---|---|---|
| LINE-01 | P0 | Cross the finite line in each direction | Emit the correct direction once after stable real movement and actual segment intersection. | No side-sign change alone treated as a valid crossing. | Independent finite-segment geometry and event timeline. | Historical/partial; review |
| LINE-02 | P0 | Walk or drive past a line endpoint | Do not emit when movement crosses only the infinite extension. | No infinite-line shortcut. | Known endpoint trajectories and zero-event assertion. | Historical/partial; review |
| LINE-03 | P0 | Stand on the line or jitter across deadband | Suppress repeated crossings until the approved movement/state requirements pass. | No cross/uncross storm from localization noise. | Near-line sequence and emitted/suppressed counts. | Historical/partial; review |
| LINE-04 | P0 | Start already on the far side | Establish initial side without inventing a crossing. | No entry event from first observation alone. | Startup position and empty expected crossing set. | Historical/partial; review |
| LINE-05 | P0 | Cross then genuinely return | Allow a legitimate reverse crossing after rearm/cooldown under the approved state rule. | No cooldown suppressing all later valid visits or admitting every oscillation. | Repeated directional timeline and state. | Historical/partial; review |
| LINE-06 | P0 | Gap/prediction spans the line | Use the approved real-observation gap limit and uncertainty rule. | No crossing fabricated only by an unobserved long prediction. | Controlled gap and real/predicted flags. | Historical/partial; review |
| LINE-07 | P0 | Several lines and shared scopes | Keep independent direction/cooldown and correct line/use_case_id associations. | No crossing assigned to the nearest or first configured line incorrectly. | Multiple-line oracle and evidence overlays. | Historical/partial; review |
| LINE-08 | P0 | Evidence at crossing and original frame | Use the intended crossing observation, source bbox and line coordinates. | No image from a later queued frame or send-time event timestamp. | Crossing marker, JPEG and geometry payload. | Historical/partial; review |

### FACE. Face pose, selection, crops and embeddings

| ID | Priority | Scenario | Must happen | Must not happen | Required evidence | Prior coverage |
|---|---|---|---|---|---|---|
| FACE-01 | P0 | Qualified frontal face becomes available | Select the first approved best-window candidate and emit its contextual crop/vector with correct local presence. | No immediate per-frame feature event before confirmation/selection. | Candidate scores, selected frame and package oracle. | Historical/partial; review |
| FACE-02 | P0 | Person has no visible face | Continue body/gait/presence when qualified; produce no fabricated face vector. | No face mandatory for every person identity sample or body suppression. | Back-facing/occluded fixture and independent modality outputs. | Historical/partial; review |
| FACE-03 | P0 | Face size, confidence, sharpness and exposure boundaries | Test below/exactly/above every frozen approved face gate in source pixels and units. | No resizing a tiny face used as proof it met source-pixel quality. | Independent quality inputs and reject/accept reasons. | Historical/partial; review |
| FACE-04 | P0 | Yaw/pitch/roll boundaries and invalid pose output | Apply the approved SixDRepNet angle convention and thresholds; reject malformed/nonfinite rotations. | No radians/degrees, sign or axis mix-up accepting the wrong pose. | Known poses/rotation outputs and gate decisions. | Historical/partial; review |
| FACE-05 | P0 | Wider evidence crop from original pixels | Include approved context, square/clipping behavior and correct source offsets at every edge. | No crop taken from the detector's resized640 image or added black context presented as real pixels. | Patterned source image, expected crop coordinates and JPEG. | Historical/partial; review |
| FACE-06 | P0 | Aligned chip versus contextual evidence | Keep the embedding model's actual aligned-input provenance distinct from a resized UI crop. | No claiming the evidence crop is the exact internal fused AdaFace chip unless exported/verified. | Alignment metadata, model profile and evidence provenance. | Historical/partial; review |
| FACE-07 | P0 | Multiple faces/person ROIs overlap | Associate the selected face with the correct person; handle no-face/multiple-candidate ambiguity explicitly. | No neighbor face or best face from another ROI attached to this presence. | Distinct people markers and ROI/face association trace. | Historical/partial; review |
| FACE-08 | P0 | Better, unchanged and worse later candidates | Emit only an approved material improvement after cooldown/window rules; retain the right previous best state. | No resend simply because five seconds elapsed or suppressing a truly better sample forever. | Long candidate-quality timeline and exact output count. | Historical/partial; review |
| FACE-09 | P0 | One person stays for an hour | Bound face search cadence/state and distinguish model computations from emitted samples. | No aimless repeated embedding events or state growing with each frame. | Compute/emit counters, cooldowns and state sizes. | Historical/partial; review |
| FACE-10 | P0 | Delayed result after track loss/reassignment | Accept only for the originating camera/session/presence/frame generation or discard explicitly. | No good-looking but wrong-person crop/vector package. | Delayed face task and conflicting new presence. | Historical/partial; review |
| FACE-11 | P0 | Handoff rejection of a selected face | Do not mark the feature as delivered/acquired until the required admission/ack rule is met; preserve retryability. | No quality/dedup state preventing every subsequent valid face after a refused package. | Producer state before/after refusal and later accepted sample. | Known issue open |
| FACE-12 | P1 | View change and recovery scheduler adaptation | Reopen qualified search when view/pose improves while staying within approved cadence limits. | No permanent low-quality acquisition lockout or uncontrolled full-rate pose inference. | View transition, resolved scheduler and selected samples. | Historical/partial; review |

### BODY. Body ReID and person crop behavior

| ID | Priority | Scenario | Must happen | Must not happen | Required evidence | Prior coverage |
|---|---|---|---|---|---|---|
| BODY-01 | P0 | Face absent but body qualified | Emit the independent384D body sample and correct subject crop when the body gate passes. | No dependency on face qualification or forced face vector. | Back-view person fixture and package. | Historical/partial; review |
| BODY-02 | P0 | Source crop size and clipping | Apply body size/visibility/quality to original-frame coordinates, including borders and partial occlusion. | No stretched detector crop or negative/out-of-frame coordinates. | Patterned original pixels and independent transform oracle. | Historical/partial; review |
| BODY-03 | P0 | Low-quality, first-good and improved samples | Keep collection separate from approved emission quality/improvement/cooldown rules. | No every-second internal computation interpreted as every-second transmission. | Candidate trajectory and exact accepted sample count. | Historical/partial; review |
| BODY-04 | P0 | Two similar or overlapping people | Associate crop/vector with the right local presence and reject ambiguous recovery. | No identity contamination through proximity or swapped ROI order. | Distinct source markers and association trace. | Historical/partial; review |
| BODY-05 | P0 | Prefetched body requests complete out of order | Return each vector to its original crop/frame/presence with bounded pending work. | No batch slot/owner mix-up or cancelled task committing later. | Per-job identity and reference vectors. | Historical/partial; review |
| BODY-06 | P0 | Wrong/nonfinite/zero vector | Reject or report failure rather than emitting an apparently valid normalized sample. | No artificial all-zero/random replacement vector. | Controlled model outputs and rejected-package assertion. | Historical/partial; review |
| BODY-07 | P0 | Delivery refused and later retried | Qualified sample remains eligible until correctly admitted/acknowledged according to policy. | No sent-quality update before the required production boundary. | Feature-state and package-ID reconciliation. | Known issue open |
| BODY-08 | P1 | Same clothing long stay and changed appearance | Exercise expected best-sample updates without claiming body similarity proves a real identity. | No global cross-camera identity assigned by reusing local IDs. | Independent modality provenance and bounded state. | Historical/partial; review |

### GAIT. Silhouette sequence collection and state cleanup

| ID | Priority | Scenario | Must happen | Must not happen | Required evidence | Prior coverage |
|---|---|---|---|---|---|---|
| GAIT-01 | P0 | Stationary person and too-short sequence | Collect only eligible moving silhouettes and emit no unqualified gait vector. | No one-frame or static silhouette treated as a gait sequence. | Selected masks, motion checks and zero-vector-event assertion. | Historical/partial; review |
| GAIT-02 | P0 | Qualified moving sequence | Use the approved number/quality/spacing of silhouettes and emit4096D with sequence provenance. | No valid vector shape alone treated as proof the sequence gate passed. | Exact selected frame IDs, timestamps and model input. | Historical/partial; review |
| GAIT-03 | P0 | One background update shared across several people | Perform the authorized once-per-eligible-camera-frame update and crop each person's own mask/history. | No update once per person, cross-person crop reuse or cross-camera MOG state. | Update/reuse counters and independent mask/crop oracle. | Historical/partial; review |
| GAIT-04 | P0 | Sampling every third eligible frame at variable FPS | Verify the actual approved collection cadence and temporal coverage, including dropped/quiet frames. | No assuming the same gait time window at every processed FPS. | Selected-frame ordinals and source-time gaps. | Historical/partial; review |
| GAIT-05 | P0 | Occlusion, split/merged silhouette and invalid masks | Reject unusable sequence elements according to the approved mask-quality policy. | No empty/fragmented/background mask accepted solely because its array has the right dimensions. | Actual masks, usable-frame reasons and sequence history. | Historical/partial; review |
| GAIT-06 | P0 | Track loss, recovery and competing person | Preserve a sequence only under approved continuity; otherwise discard/restart with correct provenance. | No stitching different people or incompatible sessions into one gait vector. | Sequence membership and recovery decision. | Historical/partial; review |
| GAIT-07 | P0 | Model montage/wrapper input equivalence | Verify temporal order, scale, orientation, normalization and reconstructed30-frame tensor where that wrapper is active. | No spatial montage interpreted in wrong temporal order or dimension. | Reference tensor and wrapper output comparison. | Historical/partial; review |
| GAIT-08 | P0 | End many tracks and wait past retention | Retire normalizer/history/masks/tasks after approved TTL; bound live state by active/recoverable subjects. | No state proportional to all historical tracks or stale retained image arrays. | Object/map counts, weak references and memory over turnover. | Known issue open |
| GAIT-09 | P0 | Reconnect, resolution change and shared-mask cleanup | Use the right camera/session/frame/shape cache key and release retained source surfaces at frame end. | No mask or background model from the previous resolution/session. | Cache keys, lifetime trace and session-change fixtures. | Historical/partial; review |
| GAIT-10 | P1 | Better sequence, long stay and backpressure | Repeat only for approved material improvement; preserve sendability after refusal and bound history/candidate storage. | No fixed periodic gait resend or quality-state contamination after failed admission. | Selection/improvement timeline and queue/state limits. | Historical/partial; review |

### ANPR. Plate collection, best read and explicit outcomes

| ID | Priority | Scenario | Must happen | Must not happen | Required evidence | Prior coverage |
|---|---|---|---|---|---|---|
| ANPR-01 | P0 | Vehicle and plate crops from the source image | Apply original-frame vehicle/plate coordinates and correct nested transforms/clipping. | No OCR input from an already downscaled evidence thumbnail or wrong vehicle ROI. | Patterned source, nested boxes and exact input pixels. | Historical/partial; review |
| ANPR-02 | P0 | Partial vehicle or plate enters the frame | Collect useful candidates but wait for approved final acceptance and best-window rules. | No first low-quality partial OCR guess emitted merely because a text string exists. | Partial-to-clear sequence and selection reasons. | Historical/partial; review |
| ANPR-03 | P0 | No plate, unreadable plate and low-confidence OCR | Emit the correct enabled contract-supported unsuccessful outcome after approved bounded evaluation, without inventing text. | No silent assumption of successful ANPR or bogus plate string. | Attempt counts, end reason and outcome payload. | Historical/partial; review |
| ANPR-04 | P0 | Confident first read versus repeated support | Apply the approved single-high-confidence and consensus paths using the frozen per-use-case policy. | No obsolete mandatory four reads or unsupported universal OCR acceptance rule. | OCR candidates with independently specified expected decisions. | Historical/partial; review |
| ANPR-05 | P0 | Conflicting OCR strings over one presence | Select/correct only with required evidence and explicit relationship to the earlier accepted read. | No many unrelated plate identities or latest-string-wins correction. | Candidate text/score timeline and correction references. | Historical/partial; review |
| ANPR-06 | P0 | Higher confidence same text and confidence bands | Apply approved improvement bands/cooldown and suppress immaterial repeats. | No resend on every tiny score increase or loss of a materially improved read. | Band-boundary and cooldown oracle. | Historical/partial; review |
| ANPR-07 | P0 | Non-Indian/temporary/unusual text formats | Honor the approved format-neutral policy and retain raw OCR provenance when normalizing. | No hidden Indian-only regex or silently changing O/0 and B/8 into certainty. | Text fixtures and normalized/raw comparison. | Historical/partial; review |
| ANPR-08 | P0 | Two close vehicles and multiple plate candidates | Associate each accepted read/outcome with its own vehicle presence and source evidence. | No plate from an adjacent vehicle or conflicting active track merged by text alone. | Distinct vehicle/plate markers and association keys. | Historical/partial; review |
| ANPR-09 | P0 | Same vehicle waits, leaves and returns | Suppress duplicates within the intended episode; handle legitimate return and later improvements per approved rules. | No cooldown shared across different vehicles or a previous visit suppressing a new one forever. | Repeated-visit timeline and dedup keys. | Historical/partial; review |
| ANPR-10 | P0 | Vehicle lost or exits during OCR/window wait | Bound waiting and preserve the originating observation time/frame, or discard stale work explicitly. | No old OCR result attached to a new presence or arbitrary completion timestamp. | Delayed OCR and session/episode state. | Historical/partial; review |
| ANPR-11 | P0 | Plate evidence on the wire | Send the contract-required frame and source plate bbox/text/vehicle association; validate any declared optional crops separately. | No undocumented mandatory plate crop or missing image referenced by the observation. | Expected manifest and source-frame JPEG. | Historical/partial; review |
| ANPR-12 | P1 | Different view/quality profiles and many vehicles | Verify bounded adaptation, attempt fairness and queues without lowering final acceptance just to raise read rate. | No one crowded camera starving peer OCR or misleading aggregate queue depth. | Per-camera candidate/attempt/queue/service metrics. | Historical/partial; review |

### SCENE. Scene sampling, scope and evidence

| ID | Priority | Scenario | Must happen | Must not happen | Required evidence | Prior coverage |
|---|---|---|---|---|---|---|
| SCENE-01 | P0 | Empty frame with no detector/tracker result | Produce the enabled scene path without depending on an object track. | No missing scene history merely because YOLO found nothing. | Empty-scene frame,768D vector and source timestamp. | Historical/partial; review |
| SCENE-02 | P0 | Active two-second sampling on every camera | Measure each enabled camera's source sample intervals, compute jobs, JPEGs, vectors and admission/ack outcomes. | No aggregate scene total hiding a camera with no samples. | Per-camera cadence and complete package accounting. | Historical/partial; review |
| SCENE-03 | P0 | Quiet safety sweep | Verify the approved roughly-five-second quiet sampling behavior separately from active cadence. | No claim of unconditional two-second images while quiet inference is gated. | Motion mode plus source/API interval distributions. | Historical/partial; review |
| SCENE-04 | P0 | Full frame versus configured geometry | Use the specified source region; current polygon scene behavior is an axis-aligned bounds crop unless explicitly changed. | No asserting polygon masking when outside-polygon pixels in the bounding rectangle remain. | Source geometry, crop coordinates and image pixels. | Historical/partial; review |
| SCENE-05 | P0 | Periodic and event trigger select the same frame | Apply approved cooldown/coalescing without duplicate expensive jobs or wrong parent association. Test configured versus unconfigured trigger bindings for entry/exit/line crossing, all configured scene scopes and triggering_observation_id. Exercise below/at/above the approved trigger cooldown (currently1s) as well as periodic/trigger collision. | No duplicate samples only because two triggers fired together. No sample from an unconfigured trigger, wrong parent event or wrong scene scope. | Frame IDs, trigger reasons and model-call count. | Historical/partial; review |
| SCENE-06 | P0 | Slow scene model or delivery pressure | Bound waiting/retention; account for skipped/coalesced optional samples and preserve already-promised artifacts. | No stale backlog labelled current or silently missing promised JPEG/vector. | Queue age, source age, skip reasons and ACK state. | Historical/partial; review |
| SCENE-07 | P0 | Camera restart or source PTS reset | Use the correct new session/frame reference and avoid mixing old queued vectors with new images. | No previous-stream scene returned under a new camera epoch. | Delayed jobs and independent frame markers. | Historical/partial; review |
| SCENE-08 | P0 | Scene JPEG and embedding consistency | Both refer to the intended same source observation/region and correct768D profile. | No vector from one frame and JPEG from the next, even if both are valid individually. | Marker/region linkage and manifest/vector hashes. | Historical/partial; review |
| SCENE-09 | P1 | Text-query embedding-space compatibility | Identify the paired text/image profile and preprocessing needed by Management, keeping retrieval evaluation separate. | No claim an image embedding proves a temporal action or arbitrary text encoder is compatible. | Profile manifest and external retrieval boundary declaration. | Historical/partial; review |
| SCENE-10 | P1 | Scene-only and mixed use-case deployment cost | Measure actual decoder/primary/scene work for each binding profile while retaining scene on all cameras. | No disabling scene or its evidence to advertise more production cameras. | Profile-specific call counts and bandwidth/resource data. | Historical/partial; review |

### FIRE. Fire/smoke temporal and spatial confirmation

| ID | Priority | Scenario | Must happen | Must not happen | Required evidence | Prior coverage |
|---|---|---|---|---|---|---|
| FIRE-01 | P0 | Repeated qualified positive observations | Emit suspected only after the approved confidence, spatial and temporal gates. | No one raw detection treated as a confirmed incident. | Raw detections, confirmation state and exact event time. | Historical/partial; review |
| FIRE-02 | P0 | One weak or isolated positive | Suppress an unqualified hazard event while separately measuring any motion-retention effect. | No conflating model score, wake state and emitted hazard certainty. | Raw score versus policy/event trace. | Historical/partial; review |
| FIRE-03 | P0 | Positive observations move between unrelated locations | Apply spatial consistency and episode association correctly. | No accumulating votes from unrelated regions into one false episode. | Known hazard regions and vote assignment. | Historical/partial; review |
| FIRE-04 | P0 | Qualified evaluated negatives after suspicion | Emit one clear transition after required duration/count and reset rules. | No every-frame clear output or safety guarantee from model-negative evidence. | Negative evaluation timeline and episode reference. | Historical/partial; review |
| FIRE-05 | P0 | Dropped frames, camera loss or inference not run | Do not treat missing evaluations as negative observations. | No fire_smoke_cleared merely because video or a model stopped. | Evaluation flags and forbidden clear assertion. | Historical/partial; review |
| FIRE-06 | P0 | Evaluation gap exceeds the approved clear limit | Reset/pause clear progress exactly as specified, then require enough new evidence. | No timer-only clearing across an unevaluated gap. | Controlled gaps and clear-state transitions. | Historical/partial; review |
| FIRE-07 | P0 | Reappearance during pending clear | Preserve/update the right episode and cancel incorrect clearing. | No contradictory suspected/clear from the same unresolved episode. | Interleaved positive/negative timeline. | Historical/partial; review |
| FIRE-08 | P1 | Multiple hazard regions and camera restart | Apply documented episode separation/session handling and correct source evidence. | No old frame reused or a new session assumed to prove a safe scene. | Episode IDs, image timestamps and source markers. | Historical/partial; review |
| FIRE-09 | P0 | Independent hazard branch under primary quiet/load | Measure actual hazard evaluation cadence and effect of overload on confirmation. | No assumption that primary FPS or a live API proves hazards are being evaluated. | Fire branch frame/call/drop counters and policy events. | Historical/partial; review |

### ART. Evidence, embeddings and output associations

| ID | Priority | Scenario | Must happen | Must not happen | Required evidence | Prior coverage |
|---|---|---|---|---|---|---|
| ART-01 | P0 | Validate the complete logical V8 package | Check schema, references, required IDs, part names, byte sizes and digests before acceptance. | A valid observation JSON alone cannot pass if a promised image/vector is absent. | Complete multipart capture and independent validator. | Current partial |
| ART-02 | P0 | Pair observation with correct camera/frame/session | Decode independently distinguishable frame markers and validate source provenance. | No matching IDs used as a substitute for checking actual image content. | JPEG marker versus observation metadata. | Historical/partial; review |
| ART-03 | P0 | Subject/face crops at all borders and aspect ratios | Verify clipping, context pixels, dimensions and normalization from original source coordinates. | No stretched, swapped, double-scaled or out-of-bounds crops. | Patterned image and independent coordinate oracle. | Historical/partial; review |
| ART-04 | P0 | Model input chip versus user evidence crop | Represent actual preprocessing provenance honestly; evidence can be wider than model input. | No UI thumbnail presented as the exact internal aligned tensor without proof. | Input/evidence transform trace and metadata. | Historical/partial; review |
| ART-05 | P0 | Face/body/gait modalities of one local presence | Correlate allowed camera/session/presence/time/frame keys correctly while letting modalities qualify independently. | No forced simultaneous samples or undocumented wire group field. | Independent modality payloads and source association oracle. | Historical/partial; review |
| ART-06 | P0 | Late-selected evidence from a best-candidate window | Use the selected candidate's pixels and capture time, plus the event's correctly defined fact time. | No final window frame or JPEG-encoding time substituted silently. | Candidate frame IDs and explicit timestamp semantics. | Historical/partial; review |
| ART-07 | P0 | Retry image/vector bytes after later frames arrive | Preserve accepted package identity, exact bytes and hashes for retries. | No regenerated crop/vector changing a retried package's content. | Before/after payload bytes and digest. | Current partial |
| ART-08 | P0 | Missing/failed JPEG encoding or embedding output | Do not accept a package promising unavailable data; report retry/rejection correctly. | No event ACK assumed to imply every artifact was produced. | Failure injection and complete promised-part checks. | Historical/partial; review |
| ART-09 | P0 | Geometry, line and plate annotations | Payload boxes/geometry map to the unmodified source frame; overlays only where explicitly required by the contract. | No requiring a drawn bbox when metadata is the agreed representation, or inconsistent coordinate spaces. | Source image, expected bbox/geometry and manifest. | Historical/partial; review |
| ART-10 | P1 | Large/small evidence and many simultaneous subjects | Bound encoding/copy memory, package sizes and admission latency while preserving correct selection. | No hidden resize/crop-quality change used to claim performance parity. | Image dimensions/bytes, encode timing and memory accounting. | Historical/partial; review |

### AGENT. CV-to-agent delivery and producer state

| ID | Priority | Scenario | Must happen | Must not happen | Required evidence | Prior coverage |
|---|---|---|---|---|---|---|
| AGENT-01 | P0 | Durable/duplicate acknowledgements | Only contract-valid acknowledgements establish accepted production for the correct package/digest. | HTTP200 alone or an unrelated ACK cannot mean successful delivery. | Response schema and package identity reconciliation. | Current partial |
| AGENT-02 | P0 | Lost ACK after receiver acceptance | Retry the exact same package; duplicate response reconciles without duplicate logical production. | No new IDs, re-encoded JPEGs or a second logical event. | Receiver ID/digest journal and retry bytes. | Historical/partial; review |
| AGENT-03 | P0 | Agent503/429, timeout or slow response | Respect finite retry/backoff, bounded queues and exposed exhaustion/pressure semantics. | No infinite camera-thread block or claim all candidates are durable during outage. | Attempt times, queue occupancy and accepted/released IDs. | Historical/partial; review |
| AGENT-04 | P0 | Nonretryable400/401 or malformed ACK | Report protocol/auth failure visibly under the agreed local-agent contract. | No invented CV Management-credential dependency or silently marking a rejected package delivered. | Response/error classification and producer state. | Historical/partial; review |
| AGENT-05 | P0 | Selected identity/plate result refused at admission | Preserve the ability to deliver the approved sample/update later; mutate sent-quality state only at the correct boundary. | No dedup/quality state committed before admission causing permanent suppression. | Exact producer state and later accepted fact/sample. | Known issue open |
| AGENT-06 | P0 | Coalescing and priority under pressure | Replace only allowed unsent optional samples with correct keys; service required facts and their complete artifacts. | No cross-camera coalescing, mandatory fact replacement or promised-image starvation. | Scheduler decisions, queue age and accepted packages. | Historical/partial; review |
| AGENT-07 | P0 | CV crash before versus after durable ACK | Distinguish unaccepted volatile work from accepted agent-owned packages; preserve IDs where the contract requires it. | No CV RAM described as a durable outbox or unsupported all-candidate durability guarantee. | Crash-boundary journal and receiver ACK records. | Historical/partial; review |
| AGENT-08 | P0 | Agent absent, socket denied, replaced or restarted | Keep liveness/health truthful and recover communication after permissions/socket return without busy-looping. | No unbounded memory, dead camera peers or hidden permanent disconnection. | Socket fault timeline and post-recovery packages. | Historical/partial; review |
| AGENT-09 | P0 | Common-cutoff loss/duplicate accounting | Pause producers, drain bounded pending work and reconcile exact package IDs at one cutoff. | No subtracting final capture totals from an earlier cached health sample to infer loss. | Producer/receiver journals and in-flight inventory. | Historical/partial; review |
| AGENT-10 | P0 | Accepted package completeness at the API boundary | Receiver validates promised image/vector bytes and links before acknowledging according to the reference API behavior. | No claiming a mock receiver proves real agent database durability or Management retrieval. | Multipart receiver assertions and explicit test boundary. | Current partial |
| AGENT-11 | P1 | Maximum package size and oversized input | Enforce declared byte/count limits and report meaningful rejection without corrupting peer traffic. | No ambiguous partial acceptance or memory exhaustion on a huge part. | Limit-boundary payloads and memory/queue traces. | Historical/partial; review |
| AGENT-12 | P0 | One delivery owner per deployment | CV uses the local agent endpoint; credentials, sequencing and remote upload remain with the agent. | No stale direct V6/V7 uploader, second SQLite outbox or per-camera authoritative retry database. | Process/module/endpoint inventory and traffic trace. | Historical/partial; review |

### TIME. Observation time and freshness

| ID | Priority | Scenario | Must happen | Must not happen | Required evidence | Prior coverage |
|---|---|---|---|---|---|---|
| TIME-01 | P0 | Known source UTC mapped to event time | Compare against independently instrumented source times with explicit clock uncertainty; retain signed error. | No future-dated events hidden by zero-clamped latency. | Source marker/UTC, observed_at, captured_at and received_at. | Known issue open |
| TIME-02 | P0 | Processing delay and later upload | Observation and evidence time retain intended source semantics despite model/queue/network delay. | No send/encode/ACK timestamp substituted for when the fact/image was observed. | Controlled delays at each boundary and timestamps. | Historical/partial; review |
| TIME-03 | P0 | Media PTS reset, backward jump or loop | Start the approved new epoch/session and prevent false lifecycle/dwell ordering. | No negative duration, old queued frame attached to the new epoch or future clock drift. | Known PTS discontinuity and state timeline. | Historical/partial; review |
| TIME-04 | P0 | Wall-clock adjustment and camera clock skew | Use monotonic time for internal intervals where intended and expose unreliable source-clock quality. | No assuming UTC-formatted strings from two cameras are synchronized. | Controlled clock mapping and uncertainty records. | Historical/partial; review |
| TIME-05 | P0 | Same frame across different model completion times | Correlate outputs by source identity rather than completion order; clarify sequence intervals for gait. | No face/body/scene time rewritten to the latest completion. | Frame/sequence markers and late-result trace. | Historical/partial; review |
| TIME-06 | P0 | Selected frame rate above or below target | Test both upper and lower selection bounds on known-rate sources; classify source-limited input separately. | A minimum-FPS-only test cannot pass runaway selection, duplicate processing or catch-up as normal cadence. | Source PTS, wall time, unique frame IDs and rate-gate decisions. | Known issue open |
| TIME-07 | P0 | Stale video processed at apparently healthy FPS | Use independent source progress/age as well as throughput and last-received time. | No current receive timestamp hiding old queued video. | PES/frame markers, backlog slope and stage queues. | Historical/partial; review |
| TIME-08 | P0 | Best-window and temporal-confirmation latency | Report intentional selection/confirmation separately from model queue time and post-decision handoff delay. | No hiding old evidence by subtracting all policy delay, or treating policy hold as compute latency. | Candidate, confirmation, decision, encode and ACK timestamps. | Historical/partial; review |
| TIME-09 | P1 | Scene sampling and API arrival distributions | Report source sampling interval separately from transport arrival interval and loss/coalescing. | No aggregate average or one rolling window certifying every camera's cadence. | Per-camera p50/p95/p99/max gaps and reasons. | Historical/partial; review |

### OBS. Health, metrics and diagnostic truthfulness

| ID | Priority | Scenario | Must happen | Must not happen | Required evidence | Prior coverage |
|---|---|---|---|---|---|---|
| OBS-01 | P0 | Correct health endpoints and probes | /healthz and /metrics respond with agreed semantics; /healthy stays absent; readiness/liveness distinguish worker and camera failures. | No bypassed probe to make a broken worker look ready. | Endpoint responses and actual pod probe behavior. | Historical/partial; review |
| OBS-02 | P0 | Per-camera raw decode, selected, inference, quiet and output rates | Label each measured boundary and window accurately, including zero/unavailable states. | No selected FPS presented as incoming raw decode or quiet skipped frames counted as model work. | Independent counters and window-aligned health snapshots. | Historical/partial; review |
| OBS-03 | P0 | Counters during source outage | Keep snapshots fresh, frame age increasing and counters stable rather than replaying stale nonzero FPS. | No frozen metrics timestamp or dead camera appearing active. | Outage timer, sample ages and counter deltas. | Historical/partial; review |
| OBS-04 | P0 | Read-only/full/unavailable metrics storage | Metrics failure must be visible and must not silently terminate analytics or healthy peers. | No six camera-thread crashes from a telemetry write exception accepted as normal recovery. | Fault-contained write test and ongoing useful outputs. | Known issue open |
| OBS-05 | P0 | CPU and RAM scopes under Kubernetes | Compare kernel cgroups at matched times; deduplicate shared-worker PIDs; distinguish RSS/PSS/cgroup/working set. | No sum of per-camera shared-process RSS claimed as total pod memory. | cgroup files, PID map and API sample windows. | Historical/partial; review |
| OBS-06 | P0 | GPU/NPU utilization and unsupported counters | Identify node-wide busy/engine scope; use null/unavailable when unsupported; separate attributed call wall time. | No833ms/s called83.3percent utilization, summed overlapping GPU engines or invented per-camera device usage. | Independent supported counters and scope metadata. | Historical/partial; review |
| OBS-07 | P0 | Stage latency and queue wait | Measure preprocess, queue, model call, postprocess, policy, JPEG and handoff independently; mark parent/child overlap. | No adding overlapping spans into a false end-to-end latency. | Instrumented spans and independently measured elapsed time. | Historical/partial; review |
| OBS-08 | P0 | Sanitized structured logs across every error path | Include UTC, camera, session and PID; redact URL userinfo, tokens and nested exception/extra fields. | No credentials exposed through nested dictionaries, callback logs, subprocess stderr or health. | Synthetic credential leak tests over actual baked formatters. | Known issue open |
| OBS-09 | P0 | Legacy console and API summaries | Use current per-camera telemetry and actual revision/backend/profile values. | No stale V5/V6 labels, fake camera averages or successful summary after a worker failed. | Console/API versus raw camera snapshots. | Historical/partial; review |
| OBS-10 | P1 | Outbox/handoff metrics at the actual ownership boundary | Expose CV pending count/bytes/oldest age, ACK/rejection/retry/exhaustion; label agent-owned durable state separately. | No CV pending RAM presented as the agent's durable queue. | Scheduler plus compatible-agent status and timestamp reconciliation. | Historical/partial; review |

### RES. Resources, overload, retirement and recovery

| ID | Priority | Scenario | Must happen | Must not happen | Required evidence | Prior coverage |
|---|---|---|---|---|---|---|
| RES-01 | P0 | Finite buffers and retained surfaces | Bound frame rings, model queues, candidate windows and pending packages by documented limits. | No growth proportional to elapsed frames or an abandoned job retaining surfaces forever. | Live object counts, queue caps and memory ownership trace. | Historical/partial; review |
| RES-02 | P0 | Continuous subject turnover | Retire presence, gait, face/body/plate candidate and cooldown state after approved TTL/episode end. | No total historical track count driving memory indefinitely. | State-map sizes versus active/recoverable subjects. | Known issue open |
| RES-03 | P0 | Many people/vehicles at once | Measure cost by ROI count and enabled use cases while preserving all gates and evidence requirements. | No nominal camera count hiding per-object workload or silently skipped identity/ANPR work. | Per-camera object/candidate/model call distributions. | Historical/partial; review |
| RES-04 | P0 | One slow or blocked secondary service | Keep bounded failure handling, truthful health and documented peer/group recovery. | A manual process pause must not qualify natural GPU/model-deadlock recovery. | Controlled stall injection and separate native-hang evidence status. | Native recovery unproven |
| RES-05 | P0 | Kill/restart camera worker, native group and CV container | Respect documented fault domains, discard stale generations and recover sources/packages correctly. | No promise that a native group crash affects only one camera when it actually affects the group. | Group membership, recovery time and peer output timeline. | Historical/partial; review |
| RES-06 | P0 | SIGTERM during decode/model/JPEG/handoff | Stop within the configured grace period; release owned work and honor accepted-package ownership semantics. | No orphan FFmpeg/threads, deadlock on shutdown or silent accepted-artifact loss. | Signal timeline, exit reason and remaining process/FD inventory. | Historical/partial; review |
| RES-07 | P0 | Bounded disk/CPU/RAM/PID pressure | Report pressure/limit errors and recover after release; confine tests to owned test volumes/cgroups. | No whole-host exhaustion or silent software fallback used to pass. | Kernel limit/pressure counters and recovery outputs. | Historical/partial; review |
| RES-08 | P1 | Thermal and power behavior | Record startup and sustained temperatures, frequencies, power limits and throttling; apply predefined safety stop rules. | A run touching critical temperature is not a thermally clean production pass merely because a guard missed consecutive samples. | One-second sensor/power trace and guard decisions. | Historical/partial; review |
| RES-09 | P1 | Ten-minute busy pre-soak | At the selected operating point verify rates, source age, scene coverage, state bounds and cleanup before longer stress. | A short RSS plateau alone is not proof of leak freedom. | Per-camera/resource time series and retirement counts. | Historical/partial; review |
| RES-10 | P1 | One-hour selected-workload qualification | Run a genuine continuous hour after lower-cost gates pass; check drift, state cleanup, useful outputs and recovery after shutdown. | No warmup counted as steady time, induced outages replacing the hour, or transfer of an old image's hour pass to a new release. | Full steady trace, sample span, package accounting and numeric growth trends. | Historical/partial; review |

### PERF. Capacity and old-versus-new comparisons

| ID | Priority | Scenario | Must happen | Must not happen | Required evidence | Prior coverage |
|---|---|---|---|---|---|---|
| PERF-01 | P0 | Freeze an equivalent baseline comparison | Same fixture bytes, rates, use cases, scene cadence, thresholds, adaptive initial state, hardware and power settings; list intentional changes. | No quiet16-camera run compared directly to a busy four-camera full-output run. | Hashed fixture/config/profile and both image inventories. | Historical/partial; review |
| PERF-02 | P0 | Reproduce historical quiet16x8 and busy profiles separately | Report raw/selected/admitted/completed work plus actual evidence and modality activity. | No128 selected FPS described as128 useful object inferences or full-use-case capacity. | Per-boundary counters and per-use-case output rates. | Historical/partial; review |
| PERF-03 | P0 | Search5FPS,8FPS and10FPS count boundaries | Grow count progressively, then test neighboring pass/fail points under the same active profile. | No schema maximum50 or largest attempted run presented as sustainable capacity. | Per-camera rates, source age and passing/failing brackets. | Historical/partial; review |
| PERF-04 | P0 | Scene required on every camera | Keep active2-second scene target, JPEG and768D vector across all production capacity profiles. | No disabling scene, evidence or identity/ANPR gates to inflate the headline count. | Per-camera scene coverage and declared enabled work. | Historical/partial; review |
| PERF-05 | P1 | Resolution, density and mixed roles | Test native720/1080/1440 and supported sizes; sparse/dense/mixed content; scene-only, presence, identity, vehicle, hazard and mixed roles. | No one universal camera maximum inferred from a single quiet recording or upscaled source. | Workload table with dimensions, ROI counts, role distribution and freshness. | Historical/partial; review |
| PERF-06 | P0 | Overload then return to normal load | Bound queues and stale work; required facts remain correctly associated; demonstrate useful source progress after recovery. | No healthy flags used as proof backlog cleared or optional drops hidden. | Backlog/source-age recovery and accepted package timeline. | Historical/partial; review |
| PERF-07 | P1 | Fairness with heterogeneous camera rates/load | Every enabled camera meets its stated service/freshness criterion at the claimed operating point. | No aggregate FPS concealing a starved camera or false equal per-camera device allocation. | Per-camera distribution, worst case and queue-service shares. | Historical/partial; review |
| PERF-08 | P1 | Repeat passing boundary with honest resource headroom | Repeat informative boundary points and selected pre-soak; state host contention, power settings and exclusions. | No best single run declared hardware maximum or24/7 guarantee; no microbenchmark-only speedup claim. | Repeated results, resources, excluded attempts and scoped recommendation. | Historical/partial; review |
| PERF-09 | P0 | Five-FPS active capacity curve with scene everywhere | Find and repeat the passing/failing camera-count boundary at5FPS for each claimed profile, retaining scene JPEGs/vectors on every camera. | No extrapolating the5FPS result from8/10FPS or using a quiet-only fleet as busy capacity. | Per-camera5FPS rate/freshness, scene coverage and complete profile/resource table. | Historical/partial; review |
| PERF-10 | P0 | Eight-FPS active capacity curve with scene everywhere | Find and repeat the passing/failing boundary at8FPS under the same scene and analytics requirements. | No reusing a historical16-camera selected-FPS result without actual active inference and secondary workload. | Per-camera8FPS traces and neighboring count results. | Historical/partial; review |
| PERF-11 | P0 | Ten-FPS active capacity curve with scene everywhere | Find and repeat the passing/failing boundary at10FPS, with latency and scene delivery meeting the declared profile. | No average throughput pass while one camera or its scene images fall behind. | Per-camera10FPS rate, queue age, source markers and ACK traces. | Historical/partial; review |
| PERF-12 | P0 | Additional-use-case budget at a fixed camera count | At each useful5/8/10FPS operating point add/remove declared use-case bundles and modality profiles, keeping scene on all cameras. | No universal number of use cases per camera that ignores whether the workload is count, OCR, face/body/gait or hazards. | Exact bindings/profiles, measured jobs and pass/fail tradeoff frontier. | Historical/partial; review |
| PERF-13 | P1 | Mixed camera roles and partial-fleet expensive analytics | Test0/25/50/75/100percent identity and ANPR shares plus balanced role maps; record actual rounded camera assignments. | No nominal25percent label when rounding or camera selection produces a different workload. | Per-camera role map, density, fairness and aggregate resources. | Historical/partial; review |
| PERF-14 | P0 | Representative positive workload for every enabled profile | Record actual detections, eligible ROIs, candidate/sequence gates, model calls and emitted/accepted outputs while using busy/sparse/mixed recordings. | Enabled but unexercised face/gait/ANPR cannot qualify full-use-case capacity; no blank replay used to avoid the work. | Input/ROI distributions and per-use-case execution/production counters. | Historical/partial; review |
| PERF-15 | P1 | Per-profile complete latency and resource dashboard | Capture CPU cores/cgroup memory, GPU engines/NPU busy with scopes, all stage/queue timings, temperatures, power/throttling, bandwidth and output/ACK rates. | No GPU-attributed call wall time presented as utilization, or per-camera shared RSS summed as pod RAM. | Window-aligned raw1second traces and per-camera/profile p50/p95/p99/max with sample counts. | Historical/partial; review |
| PERF-16 | P0 | Fleet demand comes from actual camera FPS | Use the sum of desired per-camera FPS for uniform5/8/10 and mixed-rate fleets; report the actual model-specific cadences separately. | No unexplained hard aggregate FPS cap or assumption every secondary model runs at detector FPS. | Desired/applied per-camera rates, selected/admitted/completed work and bottleneck reason. | Historical/partial; review |

### SITE. Production parity and release acceptance

| ID | Priority | Scenario | Must happen | Must not happen | Required evidence | Prior coverage |
|---|---|---|---|---|---|---|
| SITE-01 | P0 | Production-like inputs before release | Test actual deployment layout and a representative set of independent camera servers, including untrusted RTSPS and401/404 cases. | One known-good gpu-box endpoint cannot qualify all office cameras. | Source/server characteristics and per-endpoint results. | Not mapped |
| SITE-02 | P0 | Actual configuration changes reach emitted output | Verify source, revision, binding scope and geometry in the bytes/packages produced, not just supervisor health. | No applied revision claimed only by configuration acceptance. | Visible source markers and per-package revisions. | Not mapped |
| SITE-03 | P0 | Use-case semantic sign-off | For each enabled binding compare expected and forbidden event timelines, counts, intervals, evidence and associations. | A matching event_type or example JPEG cannot be the whole pass criterion. | Independent per-use-case oracle and complete actual timeline. | Not mapped |
| SITE-04 | P0 | Publish compatibility, limitations and rollback data | Publish exact digest, effective defaults, model/protocol hashes, tested profile and previous immutable tag. | No vague latest-image claim, unsupported production readiness or missing rollback reference. | Release manifest, deployment notes and complete known-issue list. | Not mapped |
| SITE-05 | P0 | Management rollout smoke at the agreed API boundary | Verify deployed digest, real first frames and complete acknowledged packages for each configured camera. | Do not equate a local test receiver with actual Management storage/search/identity behavior. | Deployment-reported camera health and agent-side package inspection. | Not mapped |
| SITE-06 | P1 | Coverage review with missing/blocked cases visible | Every claimed capability maps to a case/result; historical or mocked evidence is labelled and open failures remain open. | No count of passed tests standing in for missing scenarios or unresolved policy decisions. | Versioned ledger, evidence links and release decision. | Not mapped |

### ADAPT. Adaptive gates, bounded changes and effective consumers

| ID | Priority | Scenario | Must happen | Must not happen | Required evidence | Prior coverage |
|---|---|---|---|---|---|---|
| ADAPT-01 | P0 | Startup before enough camera-quality observations | Use the approved initial/default policy and expose insufficient calibration evidence. | No arbitrary tolerant/strict mode chosen from one noisy frame or missing metrics interpreted as good quality. | Initial policy, sample count and calibration reasons. | Historical/partial; review |
| ADAPT-02 | P0 | Two cameras with very different views | Resolve each camera's policy from its own measurements and preserve isolation across shared workers. | No one blurred/crowded camera changing another camera's gates or recovery state. | Independent metrics inputs and per-camera resolved policies. | Historical/partial; review |
| ADAPT-03 | P0 | Different use cases on one camera | Allow the approved subsystem-specific behavior rather than one mode blindly controlling face, tracking, ANPR and scenes. | No poor ANPR visibility lowering unrelated face emission or hazard standards. | Per-subsystem mode and effective-gate trace. | Historical/partial; review |
| ADAPT-04 | P0 | Every permitted adaptive bound | At extreme valid inputs remain inside approved ranges, floors and ceilings, with correct units. | No relaxed collection threshold silently becoming a relaxed final acceptance threshold. | Independent bound oracle and sampled effective settings. | Historical/partial; review |
| ADAPT-05 | P0 | Hysteresis, minimum samples and hold times | Require approved evidence windows and stable transitions before changing profile; recover appropriately when conditions improve. | No mode flapping at each frame or permanent lock in a temporary degraded mode. | Boundary metrics sequence and exact profile transition times. | Historical/partial; review |
| ADAPT-06 | P0 | Input quality versus processing pressure | Handle blur/exposure/object size separately from queue delay, detector dropout and track fragmentation according to the approved resolver. | No GPU overload misdiagnosed automatically as bad camera pixels and solved by lowering final confidence. | Separated metric sources and change-reason audit. | Historical/partial; review |
| ADAPT-07 | P0 | Coordinate, FPS and time normalization | Convert pixel sizes, normalized distances, observation windows and disappearance durations using actual source dimensions/rates. | No resolution or5FPS assumption controlling a different camera silently. | Equivalent motion/size scenarios at several resolutions and rates. | Historical/partial; review |
| ADAPT-08 | P0 | Metrics missing, stale, nonfinite or contradictory | Retain/choose the approved safe fallback and report uncertainty with bounded changes. | No NaN/zero quality metric driving an extreme threshold or false certainty. | Invalid/stale telemetry fixture and resolver outputs. | Historical/partial; review |
| ADAPT-09 | P0 | Resolved gate reaches the actual native consumer | Prove the runtime applies supported tracking/motion/quality/cadence controls at their real model/policy consumer. | No telemetry-only adaptive value or legacy centroid setting advertised as native GVA control. | Parameter-to-consumer mapping and controlled response. | Known issue open |
| ADAPT-10 | P0 | No useful modality observations available | Keep face, body, gait and plate qualification independent; adaptation cannot manufacture missing information. | No fake vector, forced plate text or required face just to improve output statistics. | Unavailable modality fixture and forbidden output set. | Historical/partial; review |
| ADAPT-11 | P0 | Backpressure and recovery adaptation | Throttle only allowed optional work, report sampling gaps and restore service under approved hysteresis. | No changed detection/confirmation/emission gates or removed promised evidence hidden as optimization. | Pressure profile, allowed changes and restored workload trace. | Historical/partial; review |
| ADAPT-12 | P1 | Repeatable comparison of adaptive behavior | Replay identical calibration observations and initial state to compare old/new transitions, then test natural adaptation separately. | No baseline with one resolver state compared to a candidate already warmed on different cameras. | Seed/state snapshot and replayed metric history. | Historical/partial; review |

## 7. Required report tables and decision log

The resulting execution report must include:

1. Capability/deployment matrix: input/server/TLS mode/auth/codec/dimensions, pod security and actual supported versus rejected result.
2. Per-use-case oracle comparison: expected/actual/forbidden events, intervals and reasons, associations and evidence; include cases that emitted nothing.
3. Source-to-package timeline: source frame/PTS/UTC, selection, queues, inference, policy confirmation, JPEG and agent acceptance; keep overlapping spans separate.
4. Per-camera useful performance: raw/selected/admitted/completed/quiet FPS, object/ROI demand, scene cadence, source freshness and gaps. Never only fleet totals.
5. Resource/fault timeline: actual cgroups, CPU, RAM/PSS/RSS, GPU engines and NPU scope, pending/state counts, temperature/power/throttling, failures and recovery.
6. Delivery reconciliation at a common cutoff: required/admitted/refused/coalesced/exhausted/in-flight/acknowledged package IDs and promised artifacts. Do not invent guaranteed production for pre-ACK volatile work.
7. Old/new equivalent comparison: exact changed components, metrics and intentional semantic differences. Keep quiet throughput separate from busy full-use-case capacity.
8. Open/unsupported/blocked/invalid/excluded cases, known issues, exact tested operating envelope and a release decision justified by those limitations.

Resolve ambiguity before running an oracle: count visibility/freshness semantics; first-observed-inside versus actual entry; uncertain dwell through gaps; which bindings own lifecycle events; optional pressure-related scene skips; unknown stream/session continuity; exact candidate-improvement rules; supported media/deployment scope; and any policy document versus wire-schema contradiction. Preserve the user's existing decisions instead of reintroducing old mandatory zones, Indian-only plate regex, mandatory face visibility or invented event fields.

Outside current CV qualification: Management global/cross-camera identity matching, UI categorization of business versus lifecycle/measurement events, database durability/search/snapshot retrieval beyond the agent API, and deferred action-context/video-clip/recheck features. They can have a separate owner/test plan, but are not silently added to this contract or marked passed by our local receiver.

Historical references: [Opt79 isolated report](ISOLATED_VALIDATION_REPORT.md), [capacity clarification](CAPACITY_RESULT.md), [Opt80 comparison](OPT80_LOCAL_COMPARISON.md), [QAFT validation](model_updates/2026-11-05-qaft-int8/VALIDATION_REPORT.md), [RTSPS repair](model_updates/2026-11-05-rtsps-compatibility-fix/REPAIR_REPORT.md), and [chronological handoff](OPTIMIZATION_HANDOFF.md). None substitutes for an exact case-level expected/forbidden assertion on the new candidate.


### Local campaign regressions added on 2026-11-11

- OBS-07 / PERF-08 / INC-024: measure stage-progress tracing overhead on identical native workloads. Every required stage counter, PTS, age and camera/session association must remain available. A run with callbacks disabled is diagnostic evidence only. Native observer replacements require count/PTS parity, peer isolation, close/removal and repeated lifetime checks (`native_progress_parity.py`), followed by unchanged-policy load and independent freshness tests.
- DEP/RES / INC-007 / INC-023: source fixtures must survive CV worker replacement. A single-listen FFmpeg exiting on disconnect must not masquerade as failed decoder recovery. `test_reconnecting_replay.py` proves two consumer epochs and child cleanup; native runtime recovery still requires separate execution.
- OBS / INC-021: thermal-abort collectors preserve BOTH stdout and stderr after stopping only the owned load. Structured runtime logs are commonly stderr. Regression: `test_stopped_log_capture_keeps_stderr_without_corrupting_inspect_json`. Historical incomplete abort logs remain incomplete; do not reconstruct missing evidence or mark them passed.

These extend assertions in existing cases; they do not reset the 282-row historical ledger or assert completion of the release matrix.


- INC-024 / PERF: replay FPS must remain source-paced after SIGSTOP/cooling/socket backpressure. Save every video PES PTS/DTS and actual TCP submission times; audit before a capacity claim. Use `test_mpegts_pacing.py`, realtime suspension regression in `test_reconnecting_replay.py`, and `analyze_source_pacing.py`. Legacy `ffmpeg -re` startup-paused captures may burst and cannot establish an upper capacity boundary without independent freshness. Do not disguise a source pacing defect as a runtime FPS change.

- INC-023 / native admission: reproduce full pending-admission stall with paced sources. A streaming pad must return without waiting; reject only before admission, retain all admitted records/outputs, and report rejections as unevaluated. Verify pending-output expiry while decode continues, FIFO/frozen timestamps, session fencing, quiet safety sweeps, source recovery and scene coverage. Native-only controls cannot qualify V8 capacity. C8 unit/control evidence is scoped; full runtime/fault/soak gates remain.
- Long-lived identity state: turn over thousands of retired presences and verify gait sequence/cache/track-center state is released only after existing retirement, session replacement or disabling gait. Preserve temporary-loss/reassociation/geometry-revision history, and finish any owned pending gait job before retirement. No background/mask/model or quality threshold change is permitted in this cleanup.

- INC-023 / SDK synchronization: when a shared native graph stalls in VA preprocessing, capture full native/Python stacks plus the exact SDK version/binary hash. Check whether AcquireBuffer is asleep with completed buffers available. A controlled SDK-method pre-wait barrier regression must distinguish the original unsynchronized ReleaseBuffer from a mutex-synchronized release. A source-code race or successful short control alone does not prove the deployment root cause; no undocumented binary patch or arbitrary SDK upgrade qualifies as a repair.

- Agent API fault regression: run `v8_agent_api_fault_probe.py` against exact baked candidate. Verify transient429/503, disconnect before/after fixture storage, stable retry IDs/bytes, duplicate ACK, permanent400, malformed/missing-object ACK and bounded6-attempt exhaustion. This is a real Unix HTTP client test with synthetic vector/fixture ACK; never mark actual agent storage or Management durability PASS from it.


### Graphics dependency changes: mandatory comparison gate (2026-11-11)

Applies to REL-05, REL-07, MODEL-01…MODEL-10 and FLOW-01…FLOW-10. This adds concrete execution requirements to those cases; it does not mark them passed or increase the catalog count.

1. Freeze the parent image configuration ID, exact SDK/OpenVINO/GStreamer versions, model/proc/embedding profile hashes, policy/schema hashes and all effective environment variables. Preserve the original image for rollback.
2. Save signed repository provenance, snapshot date, verified key fingerprint, explicit dependency pins, package transaction and normalized before/after package inventories. Test container userspace separately from host/kernel changes. A source reference is not proof that a stack fixes a failure.
3. Check that protected runtime/model/contract artifacts are unchanged. Compare identical recorded pixels through the baked primary detector and actual scene executor, retaining every detection and vector, input hashes, maximum confidence/coordinate differences and per-sample embedding cosine/L2 differences. Use the real current QAFT model path, not the older detector probe default. Direct tensor parity does not establish VA preprocessing, tracking or complete event parity.
4. Preflight the test runner under its actual UID, empty writable scratch directory and the image's full SDK Python/library paths. Do not overwrite PYTHONPATH and hide gstgva. Distinguish fixture setup errors from runtime/model failures; retain both failed setup logs and corrected executions.
5. Run the native VA decode + surface-sharing graph, source-coordinate evidence and all declared GPU/NPU models. Explicitly check current device placement after loader updates. Then repeat the workload that reproduced the original stall, first without instrumentation, and compare queue ages, per-camera completed FPS, source/evidence freshness, native generations/restarts and complete agent acknowledgements. A waiter stack alone is not an ultimate root cause.
6. Keep the existing 90 C sustained/critical-minus-2 thermal guard. Thermally stopped attempts are excluded, never reclassified as a throughput pass or image failure. Archive all raw temperatures, throttling, CPU, cgroup RAM, scoped GPU/NPU counters, stage/model/queue timings and package/ACK counters with monotonic/UTC timestamps.
7. Only after numerical, device, original failure and bounded-load checks pass, continue 5/8/10-FPS profile frontiers and the selected one-hour steady stress test. A three-minute pass cannot close intermittent failure or long-duration stability.

Executable comparison: `scripts/isolated_validation/gpu_stack_compare.py`. Current diagnostic artifacts: `optimization_evidence/2026-11-10-local-v8-r2/driver-stack-control/`. This test's local capture boundary does not establish Management durability, remote snapshot retrieval or recognition accuracy.

### Mandatory regressions found during November 11 qualification

These overlap the existing 282 scenarios; there are now 28 permanent incident
regressions. Source/unit fixes are not fresh native release qualification.

| Incident | Reproduction and required check | Saved test/evidence | Current scope |
|---|---|---|---|
| INC-025 | Run the **baked** gait-enabled extractor's teardown during camera recovery. Release its foreground/background references and engine owner; preserve peer state; engine release must still occur if background cleanup raises. Then exercise native gait-enabled source/worker recovery without `AttributeError`. | `tests/test_gait_close.py` under runtime_v8; C11 complete runtime log and C12 baked checks under the local campaign evidence. | C12 baked unit pass; native recovery pending. |
| INC-026 | Force a producer reservation between the earlier zero-pending diagnostic read and the recovery fence. Decide rebuild from the count returned **atomically by the fence**; retain old accepted records/timestamps, reject new producers, and never resume an old epoch over unfinished work. Empty fences retain camera-local recovery. | `tests/test_native_admission.py`; C12 baked checks. | Concurrency/component pass; native recovery pending. |
| INC-027 | A native graph's socket/decoder/model deadlines continue during `SIGSTOP`. Do not freeze it for startup/warmup cooling and call the resulting timeout spontaneous. A phase with `native_clock_deadlines_active=true` must cause thermal **stop/exclusion**, even if pause cooling was requested. Record any startup CPU quota/period, verify actual cgroups, restore the full declared budget before warmup, and retain all failed attempts. | `scripts/isolated_validation/test_guarded_case.py`, `test_capacity_harness.py`; C11 guard pause21.066s versus native progress age21.085s; actual quota component. | Guard regression pass; unpaused native retest pending. |

The C11 profiling case is diagnostic, not capacity evidence: external perf
sampling plus cooling pauses occurred. Its 358 CPU samples reported zero lost
samples; approximately 48% of sampled user CPU was in libc copy code called
from the Intel media driver. This is not 48% of wall time, proof of a bottleneck,
or proof that copying caused heat. Preserve the raw profile and mappings before
proposing conversion or driver changes. The original C9 VA-acquire stall remains
a separate finding; these newer fixes do not establish its ultimate cause.


## Host reachability loss during all-app initialization — INC-028

On2026-11-11, `.64` became unreachable during C12 all-app startup after the
parser/decoder-input stages progressed but decoder output remained zero. The
image cause, host freeze, power loss and network loss are not distinguished yet.
This is an open incident, not proof of a kernel or image defect. Before another
heavy test after recovery, retain previous-boot kernel/pstore logs, Docker state,
actual startup CPU limits, model/stage progress, temperature history and boot ID.
Recheck competing auto-started CV services and GPU/NPU devices after any reboot.
Track host reachability independently of `/healthz`; a remote guard cannot stop
a kernel that no longer responds. Preserve partial results and report missing
intervals. No partial/offline/thermally interrupted attempt qualifies capacity.
The case count remains282; INC-028 overlapsRES-08/FLOW-09/PERF-04.


### Permanent prefetch-boundary regression (INC-029, 2026-11-11)

A C13 source review found that consuming an already-prefetched VA sample skipped the normal missing-PTS validation and `camera.consumed`/processing trace. No real-camera missing-PTS failure was observed. The C14 correction shares consumption validation between both paths, before admission/time handling, while leaving model inputs, policy, thresholds and V8 output unchanged. Release checks must include `test_surface_consumption.py`, actual prefetch pixel/PTS parity and native tracing/recovery. Holding a future sample must not report it consumed; invalid PTS must never produce an invented observation timestamp. Seven new focused cases pass (130 with existing timestamp/admission/recovery cases); native C14 validation is pending. C13 results are not relabelled as C14 passes.


### Counter-clock isolation regression (INC-030, 2026-11-12)

Per-camera counters and their reporter uptime must be read from the same fresh telemetry snapshot. An HTTP poll interval may differ from the interval covered by cached counters. The C15 original outage check divided 41 completed frames by 10.0065 poll seconds; the actual counter interval was 8.164 seconds, giving 5.022 completed FPS. Preserve that original FAIL and the independent reanalysis. A fresh corrected outage test passed.

The permanent harness regression reproduces those exact numbers and rejects missing, stale, frozen, reversed, nonfinite or boolean counter clocks and genuine slow progress. Both selected and completed rates must still meet the original 90% peer threshold; peer health, freshness, PID and errors remain checked. This changes only test measurement, with no model/image, production gate or contract change. INC-030 overlaps OBS-02, OBS-03 and PERF-07; total scenarios remain282. New-candidate qualification templates always start NOT_RUN; historical scoped evidence belongs in separate fields.


INC-030 extension (2026-11-12): isolation compares lifetime decoder-error/reconnect counters against a healthy baseline; earlier startup history remains reported, but only a new increment/reset during the isolation window fails that criterion. Invalid/missing/noninteger/negative/bool counters fail. Retain the original bad-first FAIL, derived corrected-counter reanalysis and separately named native rerun. Ordinary bad-first isolation must use baked startup deadline, not the shortened fault-injection deadline.

### Native motion negative-control requirement (2026-11-12)

For MOTION/SCENE and INC-016/INC-022, a quiet fixture must be independently verified negative for the actual baked primary and fire models, with their real native preprocessing/postprocessing. Static or black pixels alone do not establish a negative model result. Preserve decoded source evidence, native class/confidence and model/model-proc hashes. If inference stays active while a model reports positives, diagnose model/input/conversion behavior before changing the six-negative release rule. An unavailable or skipped hazard evaluation remains unevaluated, never a manufactured negative. Record quiet-to-active-to-quiet over a complete observed cycle; cold startup/warmup can consume the fixture's initial quiet section. Compare raw direct and native inference on identical pixels when results disagree, with CPU reference explicitly distinguished from production execution. No gate loosening to pass a motion benchmark.

C15 observation: the black/people/black run sustained target FPS, but all observed windows stayed active and quiet scene cadence failed; two smoke-suspected outputs had all-zero320x320 evidence. Cause remains unconfirmed. Separate runtime throughput PASS from motion-functional FAIL. `scripts/isolated_validation/fire_blank_parity_probe.py` is prepared for an exclusive same-image diagnostic, not yet executed. Preserve original reports before rerunning.

Harness regression: pixel parity must accept the archived `{command:[...]}` record as well as legacy argv, reject malformed/incomplete records before native launch, and compare only the same frozen candidate/source pixels. A runner exception before launch is not a model/runtime comparison result; a fresh native run remains required after repairing the runner.


### Integrated fire numerical regression (INC-031, 2026-11-12)

This is execution consistency, not a request to relax model accuracy or policy gates. On byte-verified all-zero320x320 BGR input the integrated C15 fire model returned raw smoke .79296875, while direct GPU FP32 returned .04375478997826576. The integrated raw-output probe with fire-only `INFERENCE_PRECISION_HINT=f32` returned exactly .04375478997826576. An explicit fire batch-one control still produced .79296875; standalone GVA controls were negative. The exact internal compiler/kernel cause remains unconfirmed. Preserve inputs, raw tensors, model/proc hashes, effective element properties and both original failures and repair experiments.

A new image must run the real integrated hazard branch, not merely a standalone fire micrograph. Require no false hazard from verified negative frames, full quiet→active→quiet counters and scene cadence, positive suspected/clear transitions, and fresh performance/stress measurements. The candidate changes only fire GPU floating-computation precision; model weights, target device, thresholds, temporal gates and primary precision remain unchanged. Diagnostic raw-data hooks intentionally disable ROI conversion and cannot qualify hazard outputs. Twenty-two focused configuration tests pass; baked-image/full-cycle/positive regressions remain pending at this checkpoint.


INC-031 later full-cycle result: FP32-only did NOT repair the real integrated detector. Same black bytes still yielded smoke .57942956686 and motion remained active. The isolated/raw-output negative result cannot substitute for the required real-detector native regression. Candidate C16 has not been built; proposed precision change remains experimental.


### Runtime image identity regression (INC-032, 2026-11-12)

The exact C15 Docker engine reports44051227... as an OCI manifest descriptor, while private CRI reports its configuration9e1b44.... Do not call every engine `.Id` a config hash or reject the same image solely because these identifier types differ. Resolve the frozen descriptor through SHA-verified manifest/index/config bytes, require linux/amd64 and reject any outside-chain/corrupt/substituted identity. Preserve the historical captured `image_config_id` field value but document its actual descriptor kind; never rewrite old measurements. New10-case pure descriptor/redaction tests pass; actual pod/config/replacement test is still required. The JSON sanitizer must operate on parsed values to retain valid quoting in serialized build history. These are validation-harness corrections, not a new CV image or a relaxed runtime check.


### INC-033: cancelled delivery payload retention

For every new image, block delivery workers and repeatedly replace one scene scope, then evict many distinct optional scopes. Assert real payload references, heap entries and semantic indexes remain bounded; removing counters alone is insufficient. Preserve in-flight requests, caller bytes, priority order, stable retry IDs and final acknowledgements. Run `tests/test_v8_handoff_retention.py` against the baked module and repeat with the compatible agent API. Exact C16-source failure evidence is in `optimization_evidence/2026-11-12-remote64-c16/handoff-retention-review/`. New-image native validation remains pending; do not infer this was the cause of live RAM growth.


### Explicit worker-GIL stall regression (INC-007)

The recovery harness now supports `--mode native-hang --native-hang-gil`. This is an explicit test-only injection: `ctypes.PyDLL(None).sleep(600)` holds the native worker's Python GIL after normal stream startup. It changes no model/decoder/production gate and does not simulate a whole-host kernel hang. Run under the same thermal/storage/ownership guard and global lease as the other native regressions. A PASS requires the actual injection log, a different fleet-parent PID replacing that exact child, its recorded SIGKILL exit (`-9`), a fresh worker and two-camera frame/package recovery. A Python callback sleeping with the GIL released is a separate regression and cannot be copied as this result.

Saved harness and oracle controls: `scripts/isolated_validation/v8_recovery_image_validation.py` and `test_native_gil_fault.py`. The fresh C18 command is queued in `optimization_evidence/2026-11-12-remote64-c18/finish_campaign_c18.py`; it is **NOT_RUN** until its own raw report completes. Models/image/contract are unchanged by this test addition.
