> **Active C16, Nov12:** Local candidate `localhost/sentinel-cv:v8-c16-native-fire-npu`, engine OCI manifest `044c9557cabde7337513cf306a078ab953d452de8c4e108001b5d95e48399dfa`. Explicit native fire NPU placement mitigates the reproduced integrated GPU discrepancy; models, gates and V8 unchanged. Fresh foundation and ANPR passed; fresh recovery, capacity and one-hour checks continue. Read the exact-image C16 ledger; no publication or inherited capacity qualification.

> **Historical C15, Nov12:** all-app short passing points5×5,3×8,2×10; completed3×8hour passed throughput with positive memory growth. Scene-only18×5/14×8/11×10 passed, next19×5/15×8/12×10 failed. Parent engine manifest44051227. Later transport, latency and isolatedK3s results are saved; defaultGPUfire numerical discrepancy remained open. ReadtimestampedACTIVE_CAMPAIGNandbothhandoffs;noall-tests-doneclaim.

> **Historical C15 — 2026-11-11 18:21 UTC:** `localhost/sentinel-cv:v8-c15-gpu-compile1`, Docker config `44051227c6e04a350de178ccb2380605f5b3dc1024d19586b5ae74554eef1bac`. Internal GPU-containing GVA compilation threads limited to1; inference/device/precision/models/production gates/contract unchanged. C14 native startup crash localized to Intel OpenCL compiler and retained as FAIL. Six diagnostic plus three fresh uninstrumented C15 starts had no observed native fatal; this is mitigation evidence, not proof of elimination. Fresh baked checks passed, one-camera foundation now runs before a new capacity/hour/postcheck campaign. All measurements remain conditional turboOFF. Consult ACTIVE_CAMPAIGN.json and both handoffs; no publication or inherited capacity claim.

> **Historical C14 — 2026-11-11:** Root `/home/admin1/Documents/sentry-validation-20261111-c14`; local candidate `localhost/sentinel-cv:v8-c14-prefetch-boundary`, config03a24b49f6eeb5edf6889c965e984490b7a07af8742ee474a4bc10439156a088. Corrected prefetch consumption validation/tracing, INC-029. Fresh332unit+22subtests/30policy/3validators pass; native1×8baseline7.987FPS with three identity modalities+scene passes. Native positives/recovery, boundary repeats, all-app5/8/10frontiers and hour continue. All measurements conditional CPUturboOFF. C13 counts below are historical, NOT C14 qualification. Read ACTIVE_CAMPAIGN.json and exact-image ledger. Full details/failures/restoration in both handoffs.

> **Historical C13 checkpoint — 2026-11-11 14:30 UTC:** Root `/home/admin1/Documents/sentry-validation-20261111-c13`; local image `localhost/sentinel-cv:v8-c13-bgr-prefetch`, configID `e254aaa143b0c6919438b784135580579e65a373674c0abed3e36ec9335d59db`. Superseded by C14. Retained C13 results do not qualify the active image.

# Current .64 testing and optimization campaign

> **CONDITIONAL TURBO-OFF NATIVE TESTING — 2026-11-11:** All-app1×5FPS completed120seconds at4.99FPS with face/body/gait/scene/evidence; temperatures50–86°C. Stock-power attempts repeatedly thermally excluded. Turbo is temporarily disabled with original setting saved; no image/gate changes. Continuation controller340748 runs positive use-case/recovery and5/8/10FPS capacity checks; consult CAMPAIGN_CURRENT.json. This is not stock-power or full production qualification. Full restoration details are in the handoff.

Authorized again by the user on 2026-11-10. The user requests complete tracking,
measured optimization and thermal cooling followed by continued testing. This
supersedes the earlier instruction to leave `.64` testing deferred.

The required scenario list is [28 suites / 282 cases](PIPELINE_TEST_PLAN.md),
with [32 cumulative incident families](PIPELINE_TEST_MATRIX.json). This is the
scope to cover, not a claim that every fixture exists or every case passed.
Missing native, deployment and load fixtures remain work, not implied success.

## Current state

Target: `admin1@192.168.1.64`, under `/home/admin1/Documents`.

Active root: `/home/admin1/Documents/sentry-validation-20261112-c16`.
Exact local Docker engine OCI manifest ID:
`sha256:044c9557cabde7337513cf306a078ab953d452de8c4e108001b5d95e48399dfa`.
The OCI config digest is independently verified during deployment checks. This local candidate has not been published.
The primary controller continues the all-use-case 5/8/10-FPS search and a selected
one-hour run. The exclusive postcheck controller then runs API faults, pixel
parity, independently marked latency, HTTP, shared-TCP consumers, native timing
and selected mixed-role budgets with scene on every camera, followed by the
newly reachable six supplied RTSPS paths and a mixed live fleet. Actual status and
results are linked in [ACTIVE_CAMPAIGN.json](optimization_evidence/ACTIVE_CAMPAIGN.json)
and [the current measurements](optimization_evidence/2026-11-12-remote64-c16/RESULTS_CURRENT.md).
No contract or production thresholds are relaxed for these tests.

On Nov12 the earlier overnight stop was traced to a test-clock mismatch:
cached per-camera counters were divided by HTTP poll time. INC-030 preserves
the original FAIL, exact counter-clock reanalysis and focused regressions.
A fresh corrected outage, controlled reconnect, native inference-hang recovery
and missing-PTS fault test passed. Independent nonfatal failures remain FAIL
while later checks continue; thermal guards still cool/retry and unsafe cleanup
still blocks load. Eighteen scene-only cameras passed the short5FPS point;
the maximum is not yet established and this is not all-app or sustained capacity.
The search must extend beyond prior boundaries whenever all planned points pass.

Both original `observations` and renewed `observations-20261112` segments feed
the resource audit. Their clocks are comparable only within the same host boot.
All six supplied public RTSPS paths currently probe successfully; old TCP
9001–9010 refuse connections, so controlled TCP sources keep validation moving.
Consult timestamped artifacts rather than carrying these availability results
forward to a later deployment.

### Historical C12 setup and findings (superseded)

Earlier access failures are historical. The former remote root was
`/home/admin1/Documents/sentry-validation-20261111-c12`.

Frozen candidate: `localhost/sentinel-cv:v8-c12-recovery-fence`.
Imported Docker config: `02b5c8ab4ceec3fe9ba0481e916d5286d616abb39884f8fefc3aaaa8485d1ff1`.
Source Podman config: `8312b38ffcd473f621569cab83bdf5d572c8f052b1d9e403595b1fe2d25d36db`.
All 70 RootFS diffIDs and execution configuration agree; representation differs.
Baked checks, plugins, device enumeration and 80 protected hashes passed remotely.
They do not establish native capacity or full scenario coverage.

Remote startup controls exposed two test setup defects: LP-core-only affinity was
rejected by OpenVINO, and Docker rejected restoring an explicit CPU quota with
NanoCPUs. Both attempts are retained. The corrected harness restores the original
quota representation and verifies full CPU capacity before warmup. No image,
production gate, policy or model changed. Thermal exclusions remain exclusions.

V8 desired state 8.0, wire 7.0, local agent 1.0 remain frozen. The delivery boundary
is complete package submission and ACK at the agent API; downstream Management
and durable agent storage are outside this campaign scope.

### Latest measured state (Nov 11)

Remote unit/policy/schema and device/hash checks passed. Native TLS connection
fixtures passed15 cases, plus strict public RTP without extra CA. Baked API fault
fixtures passed11 cases, including an actual30-second timeout and exact-package
duplicate reconciliation. Sixteen baked configuration cases cover1/8/16/50
cameras and omitted8 / explicit5/8/10 FPS. Five RTSPS sources are reachable;
externalTCP9001–9010 currently refuse connections.

The unrestricted native foundation reached frames in the earlier invocation,
then hit a Docker test-control error while restoring startup quota. That harness
error is corrected and regression-tested. The fresh corrected case waited15
minutes for cooling without starting; optional profilers are now disabled for a
separate idle diagnostic. User confirmedfans working and explicitly requested
waiting until cool. No host power or image/policy changes have been applied.

**No remote5/8/10-FPS camera capacity, native latency envelope or one-hour stress
result is qualified.** See `optimization_evidence/2026-11-11-remote64-c12/RESULTS_CURRENT.json`
and the case/incident ledgers. Native jobs remain gated by cooling, then resume.

## Execution and decision sequence

1. **Recover access and identify the host.** Record hostname, boot ID, CPU/RAM,
   kernel, GPU/NPU/firmware, devices/groups, power settings, thermal sensors,
   free disk and link speed. Preserve the previous freeze evidence. Check
   competing CV/benchmark work and auto-started services before claiming
   isolation; do not terminate operating-system, network or unrelated services.
2. **Freeze a reproducible run.** Use a new directory and ledger, exact image,
   tracked harness and receiver hashes, fixture hashes, generated/validated
   desired state, source mapping, roles, rates and initial conditions. Do not
   reuse a historical case-name PASS or mutate an old evidence directory.
3. **Cheap correctness and deployment gates.** Baked unit/policy checks,
   canonical/embedded schemas, models/plugins/device placement, Secrets and
   non-root/read-only layout, TLS modes/auth classification, worker-start
   failures and explicitly required/forbidden outputs. Missing prerequisites
   are BLOCKED; code that violates the contract remains a failure to resolve.
4. **Bounded one-camera native foundation.** Separately identify decode,
   primary/tracker, scene, and secondary costs; verify fresh source frames,
   correct crops/vectors, ACKs and stage progress. Diagnostic decode-only and
   primary-only runs are not production capacity results. Include actual
   identity, plate and hazard opportunities rather than merely enabling apps.
5. **Combined regressions and isolation.** Untrusted RTSPS video, repeated
   source interruptions, full native admissions, blocked secondary and
   native/GIL hangs, healthy peer progress, stale/late result fencing,
   converter lifetime, truthful health, delivery refusal/retry and cleanup.
   One fault does not substitute for another. Verify new output after EACH
   recovery, not just a successful state-transition call.
6. **Use-case semantic timelines.** Exercise all applicable presence, zone,
   count, crossing, dwell episode, face/pose/body/gait, ANPR, scene and hazard
   cases. Use original-frame pixel/time oracles; test every effective gate
   below/at/above its boundary. Test unchanged subjects, improvement, gaps,
   rejected delivery and retirement. Scope labelled recognition accuracy
   separately; execution and association correctness remain required.
7. **Capacity frontier.** At each of 5, 8 and 10 requested FPS, keep scene
   JPEG/vector on every camera. Explore useful counts 1,2,4,8,12,16,20,24,32,40,50
   only while passing and within measured resource limits. At first failure,
   stop escalating that branch and refine neighboring counts. Repeat passing
   boundaries; report untested higher counts as NOT_RUN, not hardware failures.
8. **Use-case budget and mixed workloads.** Render the checked-in S/SP/SC/SX,
   face/body/gait combinations, vehicle/ANPR, hazard and all-app profiles.
   Test busy people, vehicles, mixed activity, subject turnover, long stays
   and quiet views separately; vary actual supported resolutions. Declare
   each camera's role, FPS and fixture explicitly. Measure selected,
   completed, inferred and quiet work separately, plus per-camera scene and
   positive-use-case evidence. `sum(camera FPS)` is requested demand, not a
   fixed global throttle. Schema acceptance for50 cameras is not capacity.
9. **Optimize only demonstrated execution bottlenecks.** Identify the stage,
   queue, copying/conversion, scheduling or device contention from traces.
   Make one bounded change, preserve the frozen baseline, build a uniquely
   identified candidate and compare identical frames/roles/load. Repeat
   affected semantics and cumulative regressions. No gate relaxation,
   dropped promised evidence, lower scene frequency or hidden CPU fallback
   may be used to manufacture a speedup.
10. **Stability and final comparison.** After cheaper gates pass, run the
    selected busy pre-soak and one declared sustained stress workload, then
    overload/recovery and restart/cleanup checks. A thermally interrupted
    interval is not a completed stress hold. Report capacity by profile,
    FPS, resolution and activity, with limiting stage and resource headroom.

The practical completion criterion is a reproducible passing operating envelope
and a measured failing boundary/limiting resource for the requested profiles,
with identified execution defects addressed and required semantic/recovery
regressions passing. It is not a claim of a universally optimal architecture or
an assumed20-camera result borrowed from the lighter reference benchmark.

## Thermal and host-failure handling

Record sensors and temperatures through startup, warmup, steady load, shutdown
and cooling. Do not SIGSTOP a live native graph. Stop a thermally excluded attempt and use
the guarded runner's cool threshold and dwell before a fresh attempt, bounded by reported critical temperature. Abort the owned attempt on
sustained high readings, preserve partial evidence and classify it as
`EXCLUDED_RESOURCE_ABORT`. Stop only the current case's run/case-labeled
containers and owned child process group. Wait for sustained cool readings and
retry the same case in a new attempt directory. Never promote the partial hold
or silently continue its elapsed-time clock after cooling.

Missing/untrustworthy temperature readings, repeated thermal retries or resource
exhaustion must be explicit blockers; they are not clean image failures or
passes. Cooling must not be implemented by silently changing production gates,
host power limits, fans or firmware. An unchanged nonthermal failure is not
blindly rerun: save it, form a concrete hypothesis and repair or isolate it.

The earlier `.64` hard freeze has no proven cause. Surviving samples did not
show sustained overheating or RAM exhaustion, so a thermal guard is not proof
that the freeze cause is fixed. Capture bounded kernel/device/resource progress
outside the workload and mirror checkpoints to the controller before high load.
If the host disappears, record the interruption and preserve evidence; do not
automatically reboot or classify missing final data as a completed test.

## Required evidence per attempt

- Host/image/driver/model/schema/policy/harness/fixture identities and exact
  nonsecret invocation, actual desired state and per-camera workload mapping.
- Raw progress, per-camera membership/session/PID, frame/call/drop counters,
  queue depths/oldest ages, source timestamps and independent marked freshness
  where measurable. Failed health polls remain recorded, not omitted.
- Stage queue/preprocess/inference/postprocess/policy/crop/JPEG/handoff times;
  latency percentiles only where underlying per-observation samples exist.
- CPU cores, RAM/cgroup/PSS scopes, GPU/NPU metric scope, temperature,
  throttling/power, FD/thread/surface/state growth and observer overhead.
- Complete package/evidence/vector associations, per-camera use-case/scene
  outputs and explicit positive/negative fixture expectations. Enabled-but-not-
  exercised modalities remain unqualified for busy-workload capacity.
- Thermal exclusions, failed setup attempts, cleanup outcomes, exact assertions
  passed/failed, remaining gaps and next hypothesis; never overwrite history.

## Tooling boundary

Use current tracked tools with explicit image/engine/run/case parameters.
`scripts/v8_capacity_probe.py` is a bounded throughput smoke and package-capture
tool; its generic PASS alone is not full capacity or production qualification.
Separate independent source-freshness, per-app positive coverage, resource and
sustained checks are required by the protocol.

Do not restart historical `campaign.py`, `extended_campaign.py`,
`qaft_validation_campaign.py`, `transition_driver.py` or the `prepare_*.py`
mutation chain wholesale. They retain historical image/host/PID/ledger
assumptions. Select and adapt relevant fixtures, keeping original results.

The previous uncommitted checklist work is preserved. No new push, image
publication, ApexFlo write or production deployment is implied by this campaign.
