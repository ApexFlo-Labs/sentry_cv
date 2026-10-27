# SentryCV Current Runtime Report

Snapshot: 2026-10-27, Asia/Kolkata. Dynamic rates are ten-second rolling
measurements and will vary with RTSP timing and visible objects.

## Deployment summary

- Host: `apexFabric-box`, Intel Core Ultra 9 285H, 16 logical CPUs.
- RTSP publication: 10 MediaMTX publishers on port 8654.
- Application inputs: 20 logical cameras; cameras 11-20 intentionally consume
  the same ten URLs as cameras 1-10.
- Ingest: four isolated workers with five logical cameras each.
- Inference: one centralized OpenVINO process. Both GPU models receive normalized
  RGB generated directly from NV12 inside the OpenVINO preprocessing graph.
- Retention: no frames, crops, plate strings, or embedding vectors are saved or
  logged. Only anonymous aggregate counters and rates are written to runtime
  metrics under `/run/user/1000`.

## Live RTSP inputs

| Logical camera IDs | RTSP source | Video | Codec | Native input | Nominal FPS |
|---|---|---|---|---:|---:|
| 01, 11 | cam01 | `05_heavy_peak.mp4` | H.264 | 1280x720 | 24.92 |
| 02, 12 | cam02 | `07_rush_sustained.mp4` | H.264 | 1280x720 | 24.92 |
| 03, 13 | cam03 | `11_hospital_ch7.mp4` | H.264 | 1280x720 | 25 |
| 04, 14 | cam04 | `NVR_ch14_main_20260617100000_20260617100006.mp4` | HEVC | 2560x1440 | 25 |
| 05, 15 | cam05 | `NVR_ch4_main_20260617110001_20260617110058.mp4` | HEVC | 2560x1440 | 25 |
| 06, 16 | cam06 | `NVR_ch6_main_20260617110002_20260617110058.mp4` | HEVC | 2560x1440 | 25 |
| 07, 17 | cam07 | `NVR_ch9_main_20260617100000_20260617100004.mp4` | HEVC | 2560x1440 | 25 |
| 08, 18 | cam08 | `RAVINDRA BHARATI_PCR-2026-04-24_17h00min00s000ms.mp4` | H.264 | 1920x1080 | 25* |
| 09, 19 | cam09 | `RAVINDRA BHARATI_PCR2-2026-04-24_10h00min00s000ms.mp4` | HEVC | 1920x1080 | 25* |
| 10, 20 | cam10 | `RAVINDRA BHARATI_PCR2-2026-04-24_18h00min00s000ms.mp4` | HEVC | 1920x1080 | 25* |

`*` The RTSP stream advertises 25 FPS. Its average-rate metadata is irregular or
unavailable because these files are looped publishers, so the advertised rate is
used as the source rate.

## Processing pipeline

The primary model's embedded class order is used exactly: `bus`, `car`,
`minivan`, `motorcycle`, `rickshaw`, `truck`, `bicycle`, `person`, and `others`.
This is not the COCO class order.

| Stage | Device | Model/input | Configured cadence | Observed aggregate |
|---|---|---|---:|---:|
| RTSP decode | iGPU VA-API | Native H.264/HEVC | Native source rate | 20 logical decoders |
| Primary normalization | iGPU/OpenVINO preprocessing | NV12→RGB, 640x640, FP32 /255 | 10 FPS/camera | Included below |
| Person/vehicle detection | iGPU/OpenVINO | Custom nine-class YOLO, 640x640, 4 async requests | 10 FPS/camera; 200 FPS target | ~188-191 FPS |
| Smoke/fire inference | iGPU/OpenVINO | NV12→RGB, YOLOv8 320x320, 1 async request | 2 FPS/camera; 40 FPS target | 40 FPS |
| Smoke/fire postprocess | CPU/NumPy + OpenCV | Confidence 0.35, class-aware NMS 0.70 | Every inference | ~9-12 detections/s |
| Secondary admission | Bounded NV12 queue; conditional CPU conversion | 640x640 NV12→BGR only when pixels are required | Up to 0.7 FPS/camera; 14 FPS target | ~13-14 FPS |
| Plate detection | NPU/OpenVINO | 224x224 vehicle crops | Every 3rd secondary frame | ~10-15 detections/s |
| Indian ANPR OCR | NPU/OpenVINO | 128x64 plate crops | Every detected plate | 0 accepted strings/s |
| Face detection | NPU/OpenVINO | SCRFD 320x320 person crops | Every 3rd secondary frame | ~0.2 qualifying faces/s |
| Face embedding | NPU/OpenVINO | AdaFace 112x112, 512-D | Per qualifying face | ~0.2 embeddings/s |
| Body ReID embedding | NPU/OpenVINO | TransReID 256x128, 384-D | Every 3rd secondary frame | ~15-22 embeddings/s |
| Gait embedding | NPU/OpenVINO | 30x64x44 sequence, 4096-D | After 20 valid moving silhouettes | ~0.1 embeddings/s |
| Scene embedding | NPU/OpenVINO | SigLIP2 224x224, 768-D | Every 7 secondary frames (~0.1 FPS/camera) | ~2.0 embeddings/s |
| Tracking/cropping/postprocess | CPU/OpenCV | Transient memory only | Detection-driven | Included in CPU load |

Primary FPS and smoke/fire inference FPS count completed frame inferences.
Smoke/fire detection rates are separately decoded, thresholded, and NMS-filtered
products. Plate, face, ReID, gait, and scene rates count generated products, so
they depend on visible objects and are not expected to equal their scheduling
cadence.
Scene phases are deterministically staggered across cameras to avoid synchronized
NPU bursts.

## Current performance and health

- Combined primary + smoke/fire frame inference: approximately 228-231 FPS.
- Primary latency: approximately 17-20 ms average, 27-31 ms p95.
- Secondary queue: normally 0-1 of 32 after cadence balancing.
- Secondary dropped work: 0.
- Secondary inference failures: 0.
- Average admitted-frame NV12→BGR conversion: approximately 0.25-0.31 ms.
- Average complete secondary work item: approximately 44-47 ms, including
  object-driven NPU inference.
- System CPU: 34.9-40.2%.
- SentryCV CPU: 26.1-26.6%, or about 4.2 of 16 logical cores.
- RAM: 30.6% system use; SentryCV cgroup approximately 3.16 GB.
- iGPU compute: 97.6-99.1%.
- iGPU decode: 22.2-23.4%.
- NPU: 39.8-78.0%; scene and object-driven inference make this intentionally
  bursty.
- Service restarts: 0; all 10 publishers and all 20 consumers are active.

## Interpretation

The iGPU compute engine is the limiting resource because it is nearly saturated
by the 200 FPS primary target plus 40 FPS smoke/fire target. Decode capacity is
not limiting. The NPU has absorbed every secondary model, including scene
embedding, while the bounded secondary queue currently operates without drops.
Low face and zero accepted-ANPR rates reflect the current traffic footage and
strict quality/Indian-format filters; they do not indicate model startup errors.

A host CPU profile found the largest central-process cost in Intel GPU driver
completion polling (`libigdrcl`), not OpenCV cropping. Setting OpenVINO GPU queue
throttle to `LOW` reduced host CPU but also reduced primary delivery and raised
latency, so the deployed value remains `MEDIUM` to preserve the throughput goal.
