# sentry_cv

Sentinel CV V5.2 edge runtime snapshot for the Intel Core Ultra 9 285H
deployment profile.

This repository contains the present working CV image source, the V5.2
management contract/schema package, the related design notes, and the model
artifacts needed to build the runtime image without downloading models at
container startup.

## Current Image

```text
ghcr.io/kiranmaibattu-cyber/sentry_cv:intel-285h-2026.10.05-v5.2-dev4
```

The image is built from:

- `docker/Dockerfile.sentinel-v5-2`
- `edge_runtime/solution_packs/sporada_secure/runtime_v5_2`
- `newdetails/sentry-v5.2`
- baked OpenVINO/ONNX model files under `models/`

The previous working image snapshot was:

```text
ghcr.io/kiranmaibattu-cyber/sentinel-cv-runtime:intel-285h-2026.10.05-v5.2-dev4
sha256:54cd1b5c3af020749d819daa918fcae528387c788a3d934fca1681a536801e1d
```

## Runtime Contract

Management mounts the desired state and camera URL secrets into the container:

| Path | Purpose |
|---|---|
| `/configs/desired_state.json` | V5.2 desired state from Management |
| `/run/secrets/sentinel/cameras/<camera-id>.url` | Camera source URL secret |
| `/state` | Persistent event/evidence/outbox state |
| `/tmp/sentinel` | Generated runtime files and temporary state |

The runtime exposes:

| Endpoint | Purpose |
|---|---|
| `GET /healthz` | Process liveness |
| `GET /readyz` | Worker/config/model readiness |
| `GET /metrics` | Runtime counters and revision state |

## Hardware Policy

This image is intended for Intel 285H edge nodes and expects accelerator device
access from the container runtime:

| Stage | Device |
|---|---|
| Decode | Intel iGPU / VAAPI |
| YOLO person/vehicle detection | Intel GPU |
| Face detection | Intel GPU |
| Face embedding | Intel NPU |
| Body ReID embedding | Intel NPU |
| Gait embedding | Intel NPU |
| Scene embedding | Intel GPU |
| Plate/OCR models | OpenVINO traffic model stack |

The intended deployment policy is no CPU fallback for decode or model
inference.

## Build

The build expects the Intel traffic runtime base image to exist locally:

```text
localhost/apexfabric-intel-traffic-runtime-base:intel-285h-2026.09.18-v2
```

Build the GHCR image:

```bash
SENTINEL_IMAGE_REPOSITORY=ghcr.io/kiranmaibattu-cyber/sentry_cv \
SENTINEL_IMAGE_VERSION=2026.10.05-v5.2-dev4 \
./scripts/build_sentinel_v5_2_image.sh
```

Push it:

```bash
podman push ghcr.io/kiranmaibattu-cyber/sentry_cv:intel-285h-2026.10.05-v5.2-dev4
```

## Contract And Design Docs

- `newdetails/sentry-v5.2/`: active V5.2 schemas, examples and contract.
- `Sentry_Edge_Inference_Design.md`: event production policy/design guidance.
- `V4_1_EVENT_PRODUCTION_POLICY_WORKING.md`: working policy notes used during
  the V5.2 alignment.
- `SENTINEL_V5_2_DEV_IMAGE.md`: image-specific implementation notes.
- `SENTINEL_EDGE_MANAGEMENT_CONTRACT.md`: management/edge contract notes.
- `SENTINEL_V18_1_ARCHITECTURE_AND_CONTRACT_DECISIONS.md`: history from the
  V18.1/V4/V5 design discussion.
