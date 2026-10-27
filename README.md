# sentry_cv

Sentinel CV V6 edge runtime snapshot for the Intel Core Ultra 9 285H
deployment profile.

This repository contains the present working CV image source, the V6
management contract/schema package, the related design notes, and the model
artifacts needed to build the runtime image without downloading models at
container startup.

## Current V6 Image Candidate

```text
ghcr.io/kiranmaibattu-cyber/sentinel-cv-runtime:intel-285h-2026.10.27-v6-dev19
image ID: 79a70ea03e6aa6e4068d777427c345417a996cf37a702fef16292c5e8611aee5
local digest: sha256:b227eec52224caac67890623021ad1935fcc57db22ff49bb39248b3c3f9eef3a
publication status: published development image in personal GHCR
registry digest: sha256:b227eec52224caac67890623021ad1935fcc57db22ff49bb39248b3c3f9eef3a
```

This is the first candidate in this repository with a true V6 wire boundary.
Desired state, observations, evidence metadata, embeddings and acknowledgements
use schema `6.0`, delivery targets `/api/v6/ingest/...`, and new durable records
are stored under `/state/sentinel_v6`.

The image is built from:

- `docker/Dockerfile.sentinel-v6`
- `edge_runtime/solution_packs/sporada_secure/runtime_v6`
- `newdetails/sentry-v6`
- baked OpenVINO/ONNX model files under `models/`

Dev19 replaces the static 80-class vehicle IR with the supplied dynamic-batch
nine-class vehicle IR. The current per-camera detector fixes batch 1 before GPU
compilation and uses the GPU's FP16 execution path without an explicit `f32`
hint. A future batching worker must compile a separate fixed-size model instance
for its selected batch. Dev18 remains the preceding published build. Both carry
`org.opencontainers.image.source` pointing at
`https://github.com/kiranmaibattu-cyber/sentry_cv`.

## Runtime Contract

Management mounts the desired state and camera URL secrets into the container:

| Path | Purpose |
|---|---|
| `/configs/desired_state.json` | V6 desired state from Management |
| `/run/secrets/sentinel/cameras/<camera-id>.url` | Camera source URL secret |
| `/state` | Persistent event/evidence/outbox state |
| `/tmp/sentinel` | Generated runtime files and temporary state |

Before upgrading from a 5.2-wire image, drain `/state/sentinel_v5_2`. A
partially acknowledged 5.2 record cannot safely be relabelled as V6.

The runtime exposes:

| Endpoint | Purpose |
|---|---|
| `GET /healthz` | Process liveness plus per-camera health, FPS, use-case latency/output rates, CPU/RAM, device placement, and accelerator workload telemetry |
| `GET /readyz` | Worker/config/model readiness |
| `GET /metrics` | Prometheus runtime, camera, use-case, resource, and emission metrics |

`/healthz` remains a liveness endpoint and returns HTTP 200 while the HTTP
process is alive; `ready` and `worker_running` inside its JSON describe runtime
readiness. FPS and rates cover the latest completed two-second window. Model
invocation wall time is explicitly separate from hardware utilization. NPU
utilization comes from the node's cumulative busy counter. GPU utilization is
left `null` when the node provides no compatible busy counter. CPU is reported
as cores consumed, with camera-process and container-cgroup scope separated;
container memory is not inferred by summing process RSS.

## Hardware Policy

This image is intended for Intel 285H edge nodes and expects accelerator device
access from the container runtime:

| Stage | Device |
|---|---|
| Decode | Intel iGPU / VAAPI |
| YOLO person/vehicle detection | Intel GPU |
| Face detection | Intel GPU |
| SixDRepNet head-pose quality gate | Intel NPU |
| Face embedding | Intel NPU |
| Body ReID embedding | Intel NPU |
| Gait embedding | Intel NPU |
| Scene embedding | Intel GPU |
| Plate/OCR models | OpenVINO traffic model stack |

The intended deployment policy is no CPU fallback for decode or model
inference.

Face inference uses a CV-owned adaptive lifecycle. New and recovered tracks are
sampled rapidly, acquired tracks are checked every 3 seconds, and tracks with a
quality score of at least 0.75 are checked every 15 seconds. Event emission
still requires the fixed recognition-quality and material-improvement gates.

## Build

The build expects the Intel traffic runtime base image to exist locally:

```text
localhost/apexfabric-intel-traffic-runtime-base:intel-285h-2026.09.18-v2
```

Build the GHCR image:

```bash
SENTINEL_IMAGE_REPOSITORY=ghcr.io/kiranmaibattu-cyber/sentinel-cv-runtime \
SENTINEL_IMAGE_VERSION=2026.10.27-v6-dev19 \
./scripts/build_sentinel_v6_image.sh
```

Push it:

```bash
podman push ghcr.io/kiranmaibattu-cyber/sentinel-cv-runtime:intel-285h-2026.10.27-v6-dev19
```

## Contract And Design Docs

- `newdetails/sentry-v6/`: active V6 schemas, examples and contract.
- `Sentry_Edge_Inference_Design.md`: event production policy/design guidance.
- `V4_1_EVENT_PRODUCTION_POLICY_WORKING.md`: working policy notes used during
  the V6.0 alignment.
- `SENTINEL_V6_DEV_IMAGE.md`: image-specific implementation notes.
- `SENTINEL_EDGE_MANAGEMENT_CONTRACT.md`: management/edge contract notes.
- `SENTINEL_V18_1_ARCHITECTURE_AND_CONTRACT_DECISIONS.md`: history from the
  V18.1/V4/V5 design discussion.
- `SESSION_CONTINUITY_RECORD.md`: detailed chronology, current implementation
  truth, test evidence, repository rules, and unresolved work from the ongoing
  design and validation sessions.
