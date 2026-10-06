# Sentry Edge Inference Design

## 1. Scope

This document defines the initial edge video-inference design for a 20-camera deployment. It covers:

- SigLIP vector extraction
- Vehicle detection, tracking, plate extraction, and ANPR
- Person detection, tracking, ReID, and face-vector extraction
- Data sent from the edge to the management server

Management-side storage, cross-camera association, semantic retrieval, incident workflows, and agentic reasoning are outside this document.

## 2. Common Conventions

- All messages use a versioned contract.
- All event and observation IDs are immutable and globally unique.
- Timestamps use UTC ISO 8601.
- Messages include an `idempotency_key` so pipeline retries do not create duplicates.
- A tracker ID is unique only within an edge node, camera, and tracker session.
- The globally usable track reference is therefore:

```text
(edge_id, camera_id, tracker_session_id, track_id)
```

- Model and preprocessing versions accompany derived results.
- Vectors use FP32 initially.
- Face and ReID vectors represent candidate identity evidence. They do not establish a global identity at the edge.

## 3. Initial Processing Rates

| Pipeline | Per-camera rate | Aggregate rate for 20 cameras |
|---|---:|---:|
| Vehicle inference | 8 FPS | 160 FPS |
| Person inference | 5 FPS | 100 FPS |
| SigLIP periodic extraction | 1 frame per 5 seconds | 4 frames/second |

The rates are initial design targets and must be validated on the selected edge hardware with representative camera streams.

## 4. SigLIP Vector Extraction

### 4.1 Finalized behavior

- Sample one frame every five seconds from each camera.
- Extract one SigLIP embedding from the sampled frame.
- Send the vector as FP32.
- Do not send the source snapshot at this stage.
- Include detected object classes, track references, bounding boxes, and confidence when these are available for the sampled frame.
- SigLIP extraction remains valid when detection metadata is unavailable.
- An important person or vehicle transition may trigger an additional embedding between periodic samples.

For 20 cameras, periodic sampling produces:

- 4 embeddings per second
- 345,600 embeddings per day
- Approximately 1.06 GiB/day of raw 768-dimensional FP32 vectors, excluding message and storage overhead

### 4.2 SigLIP observation object

```json
{
  "schema_version": "1.0",
  "message_type": "siglip_observation",
  "observation_id": "01J...",
  "idempotency_key": "edge-01:cam-01:2026-09-30T10:15:20Z:siglip-v1",
  "edge_id": "edge-01",
  "camera_id": "cam-01",
  "captured_at": "2026-09-30T10:15:20.000Z",
  "processed_at": "2026-09-30T10:15:20.120Z",
  "trigger": "periodic",
  "model": {
    "name": "siglip-base-patch16-224",
    "version": "qualified-version",
    "preprocessing_version": "v1",
    "dimension": 768,
    "data_type": "float32"
  },
  "vector": [0.0123, -0.0456, 0.0789],
  "detected_entities": [
    {
      "object_class": "car",
      "track_ref": {
        "tracker_session_id": "session-20260930-01",
        "track_id": "vehicle-17"
      },
      "bounding_box": [120, 80, 410, 360],
      "confidence": 0.94
    }
  ]
}
```

`trigger` is either `periodic` or `entity_transition`. The example vector is abbreviated.

## 5. Vehicle Inference

### 5.1 Finalized behavior

1. Run vehicle detection and tracking at 8 FPS.
2. Assign a camera-local tracking ID when a vehicle is detected.
3. Maintain temporary state for the scoped track reference.
4. Collect candidate plate crops rather than relying on the first visible plate.
5. Apply plate-quality checks such as size, sharpness, angle, obstruction, and exposure.
6. Run ANPR on qualified candidates and retain the strongest result.
7. Send vehicle data to management at three lifecycle stages:
   - arrival
   - identity update after ANPR succeeds or definitively ends without a reliable result
   - departure
8. Determine departure by a configured exit-line crossing or a track-missing timeout. A brief occlusion must not create a false departure.

### 5.2 ANPR state

```text
not_attempted
    -> collecting_candidates
        -> recognized
        -> failed
        -> timed_out
```

A Boolean `anpr_done` flag must not be used because it cannot distinguish a successful read from failure, timeout, or work still in progress.

### 5.3 Vehicle lifecycle object

The same contract is used for all three lifecycle messages. `event_type` and populated fields change with the lifecycle stage.

```json
{
  "schema_version": "1.0",
  "message_type": "vehicle_lifecycle",
  "event_id": "01J...",
  "idempotency_key": "edge-01:cam-01:session-01:vehicle-17:identity-updated:1",
  "event_type": "vehicle_identity_updated",
  "edge_id": "edge-01",
  "camera_id": "cam-01",
  "tracker_session_id": "session-01",
  "track_id": "vehicle-17",
  "captured_at": "2026-09-30T10:15:21.000Z",
  "first_seen_at": "2026-09-30T10:15:18.000Z",
  "last_seen_at": "2026-09-30T10:15:21.000Z",
  "vehicle": {
    "class": "car",
    "class_confidence": 0.96,
    "bounding_box": [120, 80, 410, 360],
    "detection_confidence": 0.97
  },
  "anpr": {
    "state": "recognized",
    "plate_text": "KA01AB1234",
    "confidence": 0.91,
    "candidate_count": 4,
    "best_candidate_quality": 0.88
  },
  "models": {
    "detector_version": "vehicle-detector-v1",
    "tracker_version": "tracker-v1",
    "plate_detector_version": "plate-detector-v1",
    "anpr_version": "anpr-v1"
  }
}
```

Allowed `event_type` values:

- `vehicle_arrived`
- `vehicle_identity_updated`
- `vehicle_departed`

For departure events, include `departure_reason` with a value such as `exit_line_crossed` or `track_timeout`.

## 6. Person Inference

### 6.1 Finalized behavior

1. Run person detection and tracking at 5 FPS.
2. Assign a camera-local track ID immediately after a track is established.
3. Send an immediate arrival event for a new confirmed track.
4. While people are present, send at most one batched track update per second per camera.
5. Send an immediate departure event after an exit-line crossing or configured missing-track timeout.
6. Extract a ReID vector from a qualified person crop.
7. Extract a face vector only when the face passes minimum size, pose, sharpness, visibility, and exposure checks.
8. A face vector is optional; lack of a qualified face must not prevent person tracking or ReID extraction.
9. Retain the best ReID and face samples for each track.
10. Send a vector for a new track, and resend it only when a materially better-quality vector becomes available. Do not retransmit identical vectors every second.
11. Cross-camera association and identity resolution occur in management, not at the edge.

### 6.2 Person batch object

```json
{
  "schema_version": "1.0",
  "message_type": "person_track_batch",
  "batch_id": "01J...",
  "idempotency_key": "edge-01:cam-01:2026-09-30T10:15:21Z:person-batch",
  "edge_id": "edge-01",
  "camera_id": "cam-01",
  "captured_at": "2026-09-30T10:15:21.000Z",
  "tracker_session_id": "session-01",
  "persons": [
    {
      "track_id": "person-42",
      "track_state": "active",
      "first_seen_at": "2026-09-30T10:15:18.000Z",
      "last_seen_at": "2026-09-30T10:15:21.000Z",
      "bounding_box": [120, 80, 280, 460],
      "detection_confidence": 0.94,
      "reid": {
        "vector": [0.0123, -0.0456, 0.0789],
        "dimension": 512,
        "data_type": "float32",
        "quality": 0.87,
        "model_version": "reid-v1",
        "is_replacement": false
      },
      "face": {
        "vector": [0.0234, -0.0567, 0.0890],
        "dimension": 512,
        "data_type": "float32",
        "quality": 0.82,
        "model_version": "face-encoder-v1",
        "is_replacement": false
      }
    }
  ]
}
```

The example vectors are abbreviated. When no face passes the quality gate, `face` is `null`. When no new or improved vector needs transmission, `reid` and/or `face` may be omitted from the periodic track update.

### 6.3 Person lifecycle object

```json
{
  "schema_version": "1.0",
  "message_type": "person_lifecycle",
  "event_id": "01J...",
  "idempotency_key": "edge-01:cam-01:session-01:person-42:departed",
  "event_type": "person_departed",
  "edge_id": "edge-01",
  "camera_id": "cam-01",
  "tracker_session_id": "session-01",
  "track_id": "person-42",
  "captured_at": "2026-09-30T10:15:28.000Z",
  "first_seen_at": "2026-09-30T10:15:18.000Z",
  "last_seen_at": "2026-09-30T10:15:27.000Z",
  "departure_reason": "track_timeout"
}
```

Allowed `event_type` values:

- `person_arrived`
- `person_departed`

## 7. Temporary Track State

The edge maintains short-lived state for every active vehicle and person track:

```json
{
  "track_ref": {
    "edge_id": "edge-01",
    "camera_id": "cam-01",
    "tracker_session_id": "session-01",
    "track_id": "vehicle-17"
  },
  "first_seen_at": "2026-09-30T10:15:18.000Z",
  "last_seen_at": "2026-09-30T10:15:21.000Z",
  "lifecycle_state": "active",
  "arrival_sent": true,
  "departure_sent": false,
  "best_reid_quality": null,
  "best_face_quality": null,
  "anpr_state": "collecting_candidates",
  "anpr_candidate_count": 3
}
```

This cache is operational state only. It prevents duplicate lifecycle messages and avoids repeatedly executing expensive identity extraction on poor or unchanged observations.

## 8. Initial Edge Output Summary

| Output | Frequency |
|---|---|
| SigLIP observation | Every 5 seconds per camera, plus selected transition triggers |
| Vehicle arrival | Once per confirmed vehicle track |
| Vehicle identity update | Once after ANPR recognition, failure, or timeout |
| Vehicle departure | Once per completed vehicle track |
| Person arrival | Once per confirmed person track |
| Person track batch | Maximum once per second per camera while people are present |
| Person vector update | New track or materially improved ReID/face sample only |
| Person departure | Once per completed person track |

