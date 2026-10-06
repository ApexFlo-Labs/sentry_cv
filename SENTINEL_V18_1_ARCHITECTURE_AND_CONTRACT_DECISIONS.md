# Sentinel V18.1 Architecture and Contract Decisions

Status: consolidated design record  
Date: 2026-09-29  
Current image: `ghcr.io/kiranmaibattu-cyber/sporada:intel-285h-2026.09.28-v18.1`  
Target hardware: Intel Core Ultra 285H (`linux/amd64`)

## 1. Purpose

This document consolidates the V18.1 architecture, use-case behavior, edge and
management responsibilities, event and evidence requirements, multimodal Re-ID,
semantic search, action-context matching, video clip capture, and the decisions
made while reviewing the new Sentinel V3 contract draft.

It is intentionally divided into three statuses:

- **Implemented**: behavior present in the V18.1 image.
- **Draft contract**: behavior described by the current files under `newdetails/`.
- **Target design**: agreed behavior that still requires contract and/or runtime work.

The central architectural principle is:

```text
Edge produces physical observations, evidence, and embeddings.
Management turns those facts into identity, search results, rules, incidents,
alerts, rechecks, reports, and operator decisions.
```

The edge must not invent a global person identity, open an incident, or treat an
embedding similarity as proof of a complex action.

## 2. Release Record

The latest renamed image is:

```text
ghcr.io/kiranmaibattu-cyber/sporada:intel-285h-2026.09.28-v18.1
```

Published digest:

```text
sha256:468b5c31280bb6d0e64a6ec2042265bc95690335e0adc1d80792fea3c54ee9a0
```

Version commit:

```text
7bf29223163b095339f04dc61de92d4e90140685
Release V18.1 under a distinct image tag
```

The main scene-search and multimodal Re-ID implementation commit is:

```text
d00d804fae3306414be39307e27adc28fd686544
Add V18 scene search and multimodal person Re-ID
```

The previous `2026.09.23-v18` registry tag was restored to its original image,
so V18 and V18.1 now refer to separate builds.

V18.1 verification completed with 94 passing tests. Hardware smoke verification
confirmed CPU, GPU, and NPU visibility; body, face, and gait inference on NPU;
scene inference on GPU; and the background-subtraction gait path.

## 3. System Mental Model

The system has two primary levels.

### 3.1 Edge inference level

The edge owns:

- Camera stream decoding and reconnect handling.
- Person, vehicle, plate, fire, smoke, and face detection.
- Camera-local tracking.
- Zone and line geometry evaluation.
- Face, body, gait, and scene embedding extraction.
- Evidence capture from exact source frames.
- Quality gates and bounded temporal evaluation.
- Durable, retryable delivery.
- Camera and inference health reporting.

### 3.2 Management level

Management owns:

- Desired-state generation.
- Durable storage and indexing.
- Face gallery and named recognition.
- Cross-camera person association.
- Semantic text encoding and vector search.
- Rule schedules, thresholds, grace periods, and context.
- Incident creation, grouping, acknowledgement, and resolution.
- Notifications, calls, and escalation.
- Action confirmation and secondary model execution.
- Evidence and video presentation.
- Retention and audit policy.

The resulting flow is:

```text
Desired state
  -> edge inference and temporal observation
  -> durable observation/evidence/embedding delivery
  -> management association/search/rules
  -> incident or operator output
```

## 4. V18.1 Runtime Architecture

V18.1 uses a shared-nothing, one-process-per-camera design. Each camera process
owns its decoder, OpenVINO runtime, detectors, tracker, analytics stages, evidence
writers, and upload queues. Processes share Intel devices only through the
driver, avoiding Python GIL serialization across cameras.

The frame path is:

```text
RTSP/RTSPS/HTTP/HTTPS source
  -> FFmpeg decode
  -> YOLO26n person/vehicle detection
  -> duplicate suppression
  -> geometry filtering
  -> camera-local tracking
  -> enabled use-case branches
  -> exact-frame evidence and embeddings
  -> live or durable delivery
```

Up to eight cameras are allowed by the current image contract. Camera URLs are
read from mounted secret files rather than appearing directly in desired state.

## 5. V18.1 Inputs

The implemented desired state supplies:

- `edge_id`.
- Strictly increasing `revision`.
- `camera_id`.
- Secret-file source reference.
- Processing `fps`.
- `solution_pack: sporada-secure`.
- Enabled applications.
- Normalized polygon zones.
- Ordered directional counting lines.
- Face quality and emission controls.
- Scene embedding profile and emission controls.
- Body, face, and gait Re-ID profiles.
- Re-ID emission, gait, and reassociation controls.

The implemented applications are:

```text
anpr
vehicle_counting
vehicle_entry_exit_counts
pedestrian_counting
fire_smoke_detection
face_recognition
scene_embeddings
person_reid
```

## 6. Models and Techniques

| Function | Model or technique | Output | Default device |
|---|---|---:|---|
| Person/vehicle detection | YOLO26n COCO OpenVINO FP16 | Boxes/classes | GPU |
| Plate detection | YOLO26n plate model | Plate boxes | GPU |
| Plate OCR | CCT OpenVINO OCR | Plate text | GPU/NPU |
| Fire/smoke | OpenVINO fire/smoke model | Hazard boxes/scores | GPU/AUTO |
| Face detection | SCRFD 500M | Face box/alignment | OpenVINO |
| Face embedding | AdaFace IR101 INT8 | 512D L2 vector | NPU |
| Body Re-ID | TransReID SSL INT8 | 384D L2 vector | NPU |
| Gait silhouette | OpenCV MOG2 plus morphology | Binary silhouette | CPU |
| Gait embedding | GaitBase INT8 | 4096D L2 vector | NPU |
| Scene embedding | SigLIP2 base patch16-224 FP16 | 768D L2 vector | GPU |

Detector class ownership remains with CV. Management must not configure model
class IDs or depend on detector-specific class numbering.

## 7. Delivery Channels

### 7.1 Live SSE

V18.1 uses live-tail SSE for operational telemetry:

```text
plate_read
vehicle_count_per_frame
pedestrian_count_per_frame
smoke_detected
fire_detected
face_seen
```

SSE is at-most-once, starts at the end of the journal, and does not replay
history. It must never contain raw embedding vectors.

### 7.2 Durable Sentinel delivery

Scene and multimodal Re-ID use:

```text
POST /api/v1/ingest/observations
POST /api/v1/ingest/evidence
POST /api/v1/ingest/embeddings
```

The required order is:

```text
observation acknowledged
  -> evidence acknowledged and checksum verified
  -> embedding acknowledged
```

All IDs, JSON payloads, and binary evidence are persisted before the first
attempt. Retries reuse identical IDs and bytes. Permanent validation conflicts
are retained in a visible dead-letter area.

### 7.3 Durable vehicle crossings

Completed entry/exit crossings use a separate authoritative multipart endpoint:

```text
POST /internal/vehicle-crossings
parts: event JSON + contextual snapshot
```

Crossings are not duplicated on SSE. Management deduplicates by `event_id` and
calculates totals from occurrence time.

### 7.4 Legacy face identity delivery

Standalone face recognition first uploads the face crop and then the embedding:

```text
POST /internal/face-artifacts/<sample-id>
POST /internal/face-samples
```

The same `sample_id` correlates the crop, embedding, and `face_seen` telemetry.

## 8. Implemented Use Cases

### 8.1 Baseline person and vehicle detection

YOLO26n creates class, confidence, source-frame bounding box, and camera-local
track ID. A track ID is temporary and is never a person or vehicle identity.

### 8.2 ANPR

The implemented cascade is:

```text
vehicle detection
  -> vehicle crop
  -> plate detection
  -> plate crop quality checks
  -> asynchronous OCR
  -> text stabilization across reads
  -> plate_read event and contextual evidence
```

Management owns plate watchlists, visit history, owner association, and alerting.

### 8.3 Vehicle counting

`vehicle_counting` reports the number of currently visible tracked vehicles in
a configured zone. The output is a frame measurement, not automatically true
occupancy and not an entry/exit total.

### 8.4 Pedestrian counting

`pedestrian_counting` has the same current-frame semantics for people. A stale
or unavailable camera must produce unknown coverage rather than an inferred zero.

### 8.5 Vehicle entry and exit

Each counting line is ordered from A to B. In the V18.1 convention, looking from
A toward B, the visual left side is IN and the right side is OUT:

```text
right-to-left crossing = in
left-to-right crossing = out
```

A valid crossing requires a stable-enough track, minimum displacement through
the line, and cooldown protection. Merely touching the line, appearing on the
opposite side, jittering, or remaining stationary does not count.

### 8.6 Fire and smoke

The side model reports suspected fire or smoke with score, box, camera, time,
and evidence. Management treats this as a hazard signal requiring policy and,
where appropriate, operator or safety-system confirmation.

### 8.7 Face recognition

The edge performs face detection, alignment, quality evaluation, and 512D
AdaFace extraction. It does not look up or assign a named person.

Face selection behavior is:

```text
reject below minimum quality
  -> retain best candidate during bounded window
  -> emit immediately at preferred quality
  -> otherwise emit best usable candidate when window expires
```

Cooldown suppresses repetitive samples, while material embedding change can
permit a new sample. The embedding uses the aligned model chip; UI evidence uses
a contextual crop from the same original source frame.

Only a qualifying face-gallery match may assign a named identity. Body and gait
may create candidate associations but must not name a person.

### 8.8 Multimodal person Re-ID

For each eligible tracked person, V18.1 can emit:

- Person observation and crop.
- 384D body appearance embedding.
- Optional 512D face embedding and face crop.
- Optional 4096D gait embedding after a usable sequence exists.

The management plane keeps independent indexes by exact modality, model,
version, embedding space, and dimensions.

### 8.9 Gait extraction

Gait does not use YOLO segmentation. The implemented path is:

```text
per-camera MOG2 background subtraction
  -> morphological cleanup
  -> person-box mask crop
  -> OpenGait silhouette normalization to 64x44
  -> moving-track sequence buffer
  -> resample to 30 silhouettes
  -> GaitBase
  -> normalized 4096D descriptor
```

The segmentation technique creates silhouettes; GaitBase remains the embedding
model.

### 8.10 Scene embeddings

V18.1 uses the image tower from:

```text
google/siglip2-base-patch16-224
revision 75de2d55ec2d0b4efc50b3e9ad70dba96a7b2fa2
```

Each configured scene zone, or the full frame when no zone exists, produces:

- A `scene_embedding_created` observation.
- Exact JPEG evidence.
- A normalized 768D scene vector.

Management must use the exact matching text tower and preprocessing family to
convert natural-language queries into the same image-text space.

## 9. Track IDs, Presence, and Reassociation

A tracker ID is valid only within one camera stream session. It may change after
occlusion, dropped frames, reconnect, or tracker reset.

V18.1 preserves a camera-local `presence_id` across a short tracker break only
when all configured checks pass:

- Time gap is within the limit.
- Body cosine similarity passes the threshold.
- Spatial displacement is within the limit.
- The previous track is no longer active.

If any check fails, a new presence is created. This is a local continuity aid,
not global identity.

Cross-camera association is management-owned and may combine:

```text
time and camera-path plausibility
body similarity
gait similarity
face similarity
evidence quality
operator review
```

Only face may turn a candidate into a named recognition.

## 10. Semantic Search

The basic semantic-search flow is:

```text
Edge:
camera scene -> SigLIP2 image vector + exact JPEG

Management:
natural-language query -> matching SigLIP2 text vector
  -> cosine search over scene vectors
  -> retrieve observation and JPEG
  -> apply camera, zone, and time filters
```

Example query:

```text
person standing near a red scooter
```

The vector finds visually relevant candidates. It does not prove a precise
temporal relationship such as touching, stealing, hitting, or exchanging an
object.

## 11. Action Context and Clip Capture Target Design

The agreed target extends semantic search with edge-side candidate matching and
automatic evidence capture.

```text
Management:
action description -> SigLIP2 text embedding
  -> desired state with context, query vector, match policy, and clip policy

Edge:
scene image embedding
  -> cosine comparison with active query vectors
  -> temporal confirmation
  -> frame and pre/post-event clip capture
  -> durable candidate observation, evidence, and scene embedding

Management:
recheck clip
  -> confirm, reject, or mark uncertain
  -> apply business rule and incident workflow
```

The edge result must be called a candidate. For example, similarity to “a person
touches a parked scooter” is not itself proof that touching happened.

## 12. Required Desired-State Additions

### 12.1 Action contexts

The target desired state needs `action_contexts`:

```json
{
  "id": "09c652b2-7f58-4bb3-9948-f06abc076d72",
  "name": "Person touching scooter",
  "description": "a person touches a parked scooter",
  "zone_id": "5578608d-e85d-4601-99bf-f40c81a8ca20",
  "query_embedding": {
    "profile_id": "siglip2-base-v1",
    "dim": 768,
    "vector": [0.0]
  },
  "match_policy": {
    "minimum_similarity": 0.72,
    "minimum_consecutive_matches": 3,
    "evaluation_interval_seconds": 1,
    "cooldown_seconds": 30
  },
  "evidence_policy_id": "c450b76d-2f46-416d-a371-e820f81acc65"
}
```

The displayed vector is abbreviated. The real vector must contain exactly the
declared number of finite, normalized values.

Management creates the text vector. The edge uses the vector, while retaining
the description for audit and diagnostics.

### 12.2 Evidence policies

The target desired state needs reusable `evidence_policies`:

```json
{
  "id": "c450b76d-2f46-416d-a371-e820f81acc65",
  "event_frame": true,
  "scene_frame": true,
  "clip": {
    "enabled": true,
    "seconds_before": 5,
    "seconds_after": 10,
    "maximum_duration_seconds": 30,
    "maximum_size_bytes": 524288000
  }
}
```

Management may select different policies for different contexts. Internal
FFmpeg buffering, keyframe handling, and remuxing remain implementation details.

## 13. Required Action Event

The closed event vocabulary needs `action_match_candidate`:

```json
{
  "schema_version": "3.0",
  "observation_id": "784e5367-2a3a-4847-844f-84920c93205f",
  "deployment_id": "sporadasite-01",
  "camera_id": "parking-camera",
  "config_revision": 19,
  "stream_session_id": "32642b1a-8d24-420d-93e1-e5dd288d96b5",
  "event_type": "action_match_candidate",
  "observed_at": "2026-09-29T10:30:03Z",
  "action_context_id": "09c652b2-7f58-4bb3-9948-f06abc076d72",
  "zone_id": "5578608d-e85d-4601-99bf-f40c81a8ca20",
  "similarity": 0.79,
  "threshold": 0.72,
  "consecutive_matches": 3,
  "frame_evidence_id": "f4d689d5-aea7-4be9-8bfa-ed612da47e90",
  "clip_evidence_id": "10ed25e3-773b-4fe1-8130-46f26a250639",
  "scene_embedding_id": "ac552460-9605-40f9-bf70-704b46e823ac"
}
```

The frame, clip, and embedding IDs are generated once and remain immutable on
retry. `clip_evidence_id` may be null only when the referenced evidence policy
disables clips.

## 14. Clip Evidence

The draft evidence schema already supports `video/mp4`. Target clip metadata is:

```json
{
  "schema_version": "3.0",
  "evidence_id": "10ed25e3-773b-4fe1-8130-46f26a250639",
  "observation_id": "784e5367-2a3a-4847-844f-84920c93205f",
  "evidence_type": "clip",
  "evidence_role": "clip",
  "captured_at": "2026-09-29T10:30:03Z",
  "capture_started_at": "2026-09-29T10:29:58Z",
  "capture_ended_at": "2026-09-29T10:30:13Z",
  "content_type": "video/mp4",
  "width": 1920,
  "height": 1080,
  "duration_ms": 15000,
  "size_bytes": 12840000,
  "sha256": "sha256:<64-lowercase-hex>"
}
```

Ingest must validate capture-time ordering, duration consistency, exact byte
count, and SHA-256.

For the first implementation, the edge should finish and persist the post-event
clip before beginning delivery:

```text
persist observation, frame, clip, and scene embedding
  -> POST observation
  -> POST frame evidence
  -> POST clip evidence
  -> POST scene embedding
```

This delays delivery by the configured post-event duration but preserves the
existing durable dependency model. An immediate-start notification can be added
later as a separate lifecycle if required.

## 15. Management Recheck

The initial recheck is management-side:

```text
receive action candidate and clip
  -> detect/segment person and target object
  -> track both through time
  -> evaluate proximity/contact persistence
  -> optionally run an action or vision-language model
  -> operator review when required
  -> confirm, reject, or mark uncertain
```

No management-to-edge recheck command is required for the first version because
the edge already uploads the configured evidence clip. A command contract is
needed only if management later requests a longer, different, or historical clip.

## 16. New Sentinel V3 Draft Assessment

The current files under `newdetails/` define a draft V3 contract. Their strongest
improvements are:

- Closed, typed event branches.
- Normalized source-frame geometry.
- Deployment and configuration revision correlation.
- Stream-session-scoped tracking.
- Explicit count semantics and coverage state.
- Positive zone transitions rather than inferred exits.
- Dwell lifecycle.
- Camera health and unknown-state handling.
- Profile-based embedding dimensions.
- Explicit error, retry, pause, and dead-letter behavior.
- MP4 evidence transport.

The V3 draft is a suitable foundation but is not deployable yet. It explicitly
states that V3 routes and adapters do not exist and that V2 remains active.

## 17. Contract Versus Implementation

Only boundary-visible behavior belongs in the contract.

### 17.1 Must be in contract or schema

- Desired-state fields sent by management.
- Typed observations sent by edge.
- Evidence and embedding payloads.
- IDs and correlation rules.
- Model/profile compatibility.
- Delivery ordering and acknowledgement behavior.
- Capability and configurable-limit declarations.
- Ownership and identity interpretation rules.

### 17.2 May remain implementation details

- MOG2 morphology parameters unless management controls them.
- Tracker algorithm and data structures.
- Internal body-similarity implementation.
- Gait buffer re-keying.
- FFmpeg ring-buffer structure.
- Keyframe search and remux/re-encode strategy.
- Face crop and resize code.
- Management vector-index implementation.
- Cross-camera score fusion.
- Incident and notification internals.

## 18. Minimal V3 Contract Additions

The following additions are required to represent the agreed system without
turning the contract into an implementation manual.

### 18.1 Embeddings

1. Add `gait` to the embedding `kind` enum.
2. Add `gait_embeddings` to desired-state outputs.
3. Declare concrete profiles for AdaFace 512D, TransReID 384D, GaitBase 4096D,
   and SigLIP2 768D.
4. Mark the SigLIP2 scene profile as supporting exact shared-space text queries.
5. Migrate management storage away from fixed face/body/scene dimensions.

### 18.2 Observations and identity

1. Add `scene_sample` for periodic semantic-search material.
2. Add `action_match_candidate` for an edge-triggered context candidate.
3. Permit `presence_id` on object, dwell, zone-transition, and line-cross events.
4. State that named recognition is face-only.
5. State that body and gait are candidate-association evidence only.

### 18.3 Desired state and evidence

1. Add `action_contexts`.
2. Add reusable `evidence_policies`.
3. Require action contexts to reference a configured zone, query profile, and
   evidence policy.
4. Require scene embeddings and evidence outputs when action contexts are active.
5. Add frame, clip, and scene-embedding references to action candidate events.
6. Declare action and pre/post-event clip capabilities and maximum limits in the
   image contract.

### 18.4 Packaging and control plane

1. Rename draft files to the canonical names referenced by the contract.
2. Implement `/api/v3/ingest/observations`, `/evidence`, and `/embeddings`.
3. Bind V3 bearer tokens to deployment, contract version, and camera allowlist.
4. Add examples and conformance tests for every event branch.
5. Enforce cross-field checks that JSON Schema alone cannot express.

## 19. V3 Draft Issues To Resolve

At the time of this document, the contract references canonical names such as:

```text
image-contract.yaml
desired-state.schema.json
observation.schema.json
evidence.schema.json
embedding.schema.json
acknowledgement.schema.json
```

The actual draft files include copy suffixes such as ` 1` and ` 5`, so those
references do not resolve. The package must be normalized before use.

The profile declarations are currently empty, which is valid only for an image
that emits no embeddings. A concrete V18-derived image must fill them.

The current management storage assumptions described by the handoff do not
match V18.1's 384D body and 768D scene spaces. Gait 4096D is absent entirely.
Storage and schema support must be completed before claiming conformance.

## 20. Count, Dwell, and Time Semantics

Time-sensitive events require explicit occurrence times. HTTP arrival time must
never replace camera observation time.

### 20.1 Counts

Counts must declare one of:

- `visible_now`: current visible/tracked frame estimate.
- `current_occupancy`: persistent occupancy with adequate entry/exit accounting.

V18.1's existing per-frame count is `visible_now` unless stronger persistence
and complete coverage are implemented.

### 20.2 Dwell

A dwell episode begins on the first positive in-zone observation. It remains
ongoing until a positive exit is observed. Track loss, stale frames, or camera
failure must not silently complete dwell.

When reassociation preserves the same `presence_id`, dwell may continue across a
track-ID change according to the accepted edge continuity policy.

### 20.3 Recheck and unavailable cameras

When a camera is unavailable, current count, dwell completion, and recheck
availability become unknown. Management must not infer zero occupancy or exit.

## 21. Privacy and Identity Rules

- Raw embeddings must not appear in SSE or logs.
- Camera credentials must remain in mounted secret files.
- Edge must not access the management person database.
- Edge must not emit a global `person_id`.
- Face, body, gait, and scene vectors must never be compared across incompatible
  profiles.
- Face may support named recognition after management gallery matching.
- Body and gait support candidate association only.
- Scene similarity supports retrieval and candidate triggering, not identity.
- Action-context similarity supports candidate evidence capture, not final proof.

## 22. Capability Declaration Target

The final image contract should advertise limits such as:

```yaml
actionMatching:
  enabled: true
  method: scene-query-cosine
  resultSemantics: candidate-only
  maximumContextsPerCamera: 32
  supportedProfileIds: [siglip2-base-v1]

clipCapture:
  enabled: true
  contentTypes: [video/mp4]
  supportsPreEventBuffer: true
  maximumPreEventSeconds: 15
  maximumPostEventSeconds: 30
  maximumDurationSeconds: 45
  maximumBytes: 524288000

identity:
  namedRecognitionSource: face-only
  bodyUse: candidate-association-only
  gaitUse: candidate-association-only
  personId: management-owned-and-forbidden-in-edge-output
```

Management validates desired state against these image capabilities before
deployment.

## 23. Final Target Architecture

The complete target is:

```text
Management desired state
  - cameras, zones, lines
  - enabled typed outputs
  - embedding profile IDs
  - action query vectors
  - match and evidence policies

Edge V18-derived runtime
  - decode, detection, tracking
  - count, crossing, dwell, health
  - face/body/gait/scene embeddings
  - local presence continuity
  - scene/query candidate matching
  - exact frame and pre/post-event clip capture
  - durable ordered delivery

Management ingest and intelligence
  - schema/profile validation
  - evidence and vector persistence
  - semantic search
  - face-only named recognition
  - multimodal cross-camera candidate association
  - clip-based action recheck
  - rules, incidents, alerts, audit, and reporting
```

## 24. Implementation Status Summary

### Implemented in V18.1

- Eight application selections.
- YOLO26n person/vehicle detection and tracking.
- ANPR cascade.
- Vehicle and pedestrian visible-frame counts.
- Durable vehicle entry/exit crossing delivery.
- Fire/smoke events.
- Face sample selection and durable face delivery.
- Body, optional face, and gait Re-ID evidence.
- Camera-local short-gap presence reassociation.
- Periodic SigLIP2 scene embeddings and JPEG evidence.
- Durable observation/evidence/embedding outbox.

### Present in V3 draft but not implemented in V18.1

- V3 UUID event vocabulary.
- Explicit count semantics and coverage state.
- Typed dwell and positive zone transitions.
- Typed camera health.
- Profile-based variable embedding dimensions.
- `/api/v3` ingest routes.

### Agreed target work

- Restore gait to the V3 profile and payload vocabulary.
- Add concrete V18 embedding profiles.
- Add stable presence correlation across temporal event types.
- Add `scene_sample`.
- Add action contexts and evidence policies to desired state.
- Add `action_match_candidate`.
- Add encoded pre/post-event clip buffering and upload.
- Add management clip recheck and final action decision.
- Normalize contract filenames and implement V3 control-plane adapters.

## 25. Acceptance Criteria For The Next Contract Revision

The next contract revision is ready for implementation review when:

1. Every referenced schema file exists under its canonical name.
2. Every JSON Schema validates under Draft 2020-12.
3. Concrete face, body, gait, and scene profiles are declared.
4. Management storage accepts each declared dimension and space.
5. Desired state validates action context and evidence policy references.
6. `scene_sample` and `action_match_candidate` examples validate.
7. Frame and clip evidence examples validate.
8. Observation, evidence, and embedding acknowledgement examples validate.
9. Presence continuity semantics are explicit for track-ID changes.
10. Face-only named-recognition policy is explicit.
11. Management can distinguish candidate action matches from confirmed outcomes.
12. Retry, restart, duplicate, malformed acknowledgement, and dead-letter tests
    pass without changing IDs or evidence bytes.

## 26. Final Decision

The V3 draft should be used as the typed-event and durable-delivery foundation.
It should not copy every V18 algorithm into the contract. The required contract
extensions are limited to boundary-visible capabilities: gait transport,
concrete embedding profiles, stable presence correlation, scene samples,
action-context desired state, clip evidence policies, action candidate events,
and explicit identity interpretation.

V18.1 remains the working inference baseline. The next implementation should
adapt its proven inference branches to the finalized V3 boundary, then add
action-context matching and encoded pre/post-event clip capture without moving
management-owned rules or identity decisions into the edge.
