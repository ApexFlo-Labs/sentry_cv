# SentryCV End-to-End Engineering and Validation Report

## 1. Document control

| Field | Value |
|---|---|
| System | SentryCV Intel edge inference pipeline |
| Target host | `apexFabric-box` (`100.121.56.73` over Tailscale) |
| Repository | `/home/admin1/sentry_cv` |
| Branch | `SentryCV_Pipeline` |
| Report date | 2026-10-27, Asia/Kolkata |
| Deployed revision at final validation | `7485a1e` |
| Dashboard | `http://100.121.56.73:8090/` |
| RTSP listener | TCP port `8654` |

This report records the work performed during the session from remote access and
RTSP validation through pipeline redesign, performance testing, secondary
analytics enablement, correctness fixes, and final live validation. It also
documents rejected alternatives and the reasons behind the current design.

Measurements are workload-dependent. Unless explicitly described as an isolated
benchmark, FPS and utilization values are rolling live measurements from the 20
logical-camera test workload. Detection counts are model outputs, not accuracy
scores against labelled ground truth.

## 2. Goals and constraints

The engineering goals were:

1. Bring up and validate the existing application on the Intel edge host.
2. Generate controllable RTSP test streams from local video files.
3. Scale from 10 to 20 logical camera inputs, with a near-term design target of
   30 cameras.
4. Maximize primary person/vehicle inference throughput.
5. Run person/vehicle and smoke/fire inference on the iGPU.
6. Run face, body ReID, gait, scene, license-plate, and OCR models on the NPU.
7. Keep postprocessing, tracking, frame selection, and cropping on the CPU where
   appropriate.
8. Expose exact system CPU, RAM, iGPU compute, iGPU decode, and NPU utilization.
9. Prevent unbounded queues and stale-frame latency.
10. Do not persist frames, crops, OCR strings, or embeddings. Generate them only
    to validate and optimize the pipeline, then discard them.

The final implementation retains the code for all enabled analytics while
making cadence and admission configurable so primary throughput remains
protected.

## 3. Validated host environment

### 3.1 Hardware and operating system

| Component | Validated value |
|---|---|
| Hostname | `apexFabric-box` |
| Kernel | Linux `7.0.0-30-generic`, Ubuntu 24.04 series |
| CPU | Intel Core Ultra 9 285H |
| Logical CPUs | 16 |
| CPU topology | 1 socket, 16 cores, 1 thread/core |
| Maximum reported CPU frequency | 5.4 GHz |
| Installed memory | 30.8 GiB usable |
| Swap | Disabled |
| iGPU | Intel Graphics with VA-API/iHD media driver |
| NPU | Intel AI Boost 3720, exposed as `/dev/accel/accel0` |

### 3.2 Runtime stack

| Software/capability | Validated value |
|---|---|
| OpenVINO | `2026.2.0-21903-52ddc073857-releases/2026/2` |
| OpenVINO devices | `CPU`, `GPU`, `NPU` |
| GStreamer | 1.24.2 |
| VA-API | 1.24.0 |
| VA driver | Intel `iHD_drv_video.so` |
| DL Streamer | Installed under `/opt/intel/dlstreamer` |
| Python environment | `/home/admin1/sentry_cv/.venv` |
| Hardware counters | i915 PMU through `perf`; NPU busy time through sysfs |

DL Streamer elements are made visible by sourcing
`/opt/intel/dlstreamer/scripts/setup_dls_env.sh`. A plain shell that has not
sourced that file can report `gvadetect` as unavailable even though the
application preflight succeeds. The repository preflight script sources the
environment before checking plugins.

`kernel.perf_event_paranoid=-1` was configured so the dashboard can read i915
PMU busy-time counters. Without that permission, exact iGPU engine percentages
cannot be collected by the unprivileged dashboard service.

## 4. Concepts established during design

### 4.1 GStreamer and DL Streamer

GStreamer is the media graph framework. It opens RTSP sources, depayloads and
decodes H.264/H.265, applies rate control and format conversion, creates queues,
and delivers frames or metadata between pipeline elements.

DL Streamer is Intel's computer-vision layer on top of GStreamer. Elements such
as `gvadetect`, `gvastreammux`, and `gvastreamdemux` connect GStreamer buffers to
OpenVINO inference and attach inference metadata to the corresponding buffers.
`gvadetect` does not provide the trained intelligence itself; it executes the
OpenVINO IR model supplied by this repository.

Python is the orchestration and application layer. It builds pipelines,
supervises processes, schedules direct OpenVINO requests, decodes model outputs,
tracks objects, creates crops, runs secondary models, collects metrics, and
discards transient products.

### 4.2 Why decode is not batched

Video decoding is stateful per compressed stream. Each RTSP source has its own
transport timing, codec state, reference frames, loss behavior, resolution, and
reconnect lifecycle. Combining compressed packets from independent cameras into
one decoder batch would not create useful inference-style batching.

The design therefore keeps one ingest/decode branch per camera. Frames may be
combined or scheduled together only after decode.

### 4.3 `nireq` is concurrency, not batch size

`nireq` is the number of asynchronous inference requests available to a compiled
model. With batch size 1 and `nireq=4`, up to four independent single-frame
requests can be in flight. It does not mean four frames are combined into one
tensor, and it is not multiplied independently for every camera in the current
central process.

The active primary configuration is batch 1 with four global asynchronous
requests. Smoke/fire uses one asynchronous request.

### 4.4 RTSP port and stream paths

MediaMTX listens on one RTSP server port, currently `8654`. Publishers and
consumers both connect to that server port; the path differentiates streams:

```text
rtsp://127.0.0.1:8654/cam01
rtsp://127.0.0.1:8654/cam02
...
```

One published path can have multiple consumers. Generating three copies creates
three independent paths and three publisher processes, which is useful for
independent timing or load tests. It is not required merely to allow three
applications to watch the same stream; all three can consume one URL.

## 5. Work chronology, findings, and decisions

### 5.1 Remote access and baseline validation

1. Tailscale connectivity to `100.121.56.73` was established.
2. SSH key authentication was authorized for `admin1`.
3. The host identity, home directory, kernel, and repository were confirmed.
4. The existing application and RTSP generator were inspected independently.
5. It was confirmed that stopping SentryCV must not stop MediaMTX or the FFmpeg
   publishers; they are separate user services.

Decision: manage the CV runtime, RTSP server, RTSP publishers, and dashboard as
separate services. This allows stream generation to stay alive while the
inference application is stopped, restarted, or modified.

### 5.2 RTSP generation and control UI

The dashboard was extended into a stream control plane. It now:

- discovers suitable local video files;
- presents a selectable list with Select all and Deselect all actions;
- enables an independent copy count only for selected videos;
- accepts an RTSP port, defaulting to `8654`;
- validates the port range and detects an occupied/incompatible port;
- stops the previous generated publishers before creating a new set;
- waits until every requested publisher is ready;
- reports failures rather than declaring success early;
- lists every generated RTSP URL;
- restarts the CV pipeline after a successful stream-set replacement; and
- exposes live publisher and consumer counts.

Current external generator assets are:

```text
/home/admin1/chandana_optimization/rtsp/generate_rtsp.sh
/home/admin1/chandana_optimization/rtsp/ui_state.json
/home/admin1/chandana_optimization/chandana_optimization_videos/
```

The dashboard implementation is in `dashboard/server.py` and
`dashboard/static/` inside this repository.

### 5.3 Initial per-camera architecture assessment

The initial design effectively created inference resources per camera. That is
simple and strongly isolated but scales poorly to 30 cameras because model
instances, threads, queues, and device scheduling are duplicated. It also makes
global GPU concurrency difficult to control.

The first redesign grouped cameras into workers:

```text
Supervisor
├── Worker 1: cameras 1-5
├── Worker 2: cameras 6-10
├── Worker 3: cameras 11-15
└── Worker 4: cameras 16-20
```

For a 30-camera deployment, the same grouping produces six workers. Startup is
staggered, and a failed ingest worker affects at most five cameras.

Decision: retain five-camera ingest workers for fault containment and lifecycle
management, but do not replicate primary inference in every worker.

### 5.4 Early throughput tests

The initial 10-camera workload used approximately 5 FPS/camera for primary
detection and 1 FPS/camera for smoke/fire, a nominal total of 60 frame
inferences/s. Only about 23 results/s were initially visible. This showed that
model compute was not the only issue; pipeline delivery, result accounting, and
per-camera inference construction also mattered.

The requested rates were then raised to:

- primary person/vehicle: 10 FPS per camera;
- smoke/fire: 2 FPS per camera.

With 10 cameras, the nominal request rate became 120 FPS. With 20 logical
cameras, it became 240 FPS. The 20-camera test duplicated the ten RTSP source
URLs as cameras 11-20. This deliberately doubled decoding and inference load
without requiring 20 unique source files.

The iGPU compute engine approached saturation while decode utilization remained
much lower. This established that decode was not the capacity limiter. It also
explained fluctuating output rates: independently timed streams, synchronized
bursts, inference queues, and competing GPU models caused short-window
variation.

### 5.5 Multi-model GPU placement

Smoke/fire was temporarily moved during diagnosis, but the requirement was to
keep both primary models on the iGPU. The final placement is:

- person/vehicle model: GPU;
- smoke/fire model: GPU;
- all requested secondary neural models: NPU;
- crop extraction, tracking, NMS, OCR text filtering, and orchestration: CPU.

Decision: solve multi-model GPU scheduling explicitly rather than hiding the
contention by moving a primary model to another accelerator.

### 5.6 Batch-model investigation

The source model was inspected and batch-capable artifacts were created without
overwriting the original:

- `vehicle_yolo26n_fp16_dynamic.xml/.bin`: dynamic batch, input
  `[?,3,640,640]`, output `[?,300,6]`;
- `vehicle_yolo26n_fp16_batch4.xml/.bin`: static batch 4, input
  `[4,3,640,640]`, output `[4,300,6]`;
- a smoke/fire batch-4 experiment was also preserved.

`tools/convert_vehicle_batch.py` patches fixed-batch reshape operations and
rebuilds per-image Top-K/gather postprocessing. It validates the serialized
model shapes after conversion.

A simple DL Streamer batch/mux test showed that merely setting batch 4 did not
produce a healthy live pipeline. Cross-camera frame delivery and mux timing were
the limiting factors. A batch must wait for suitable frames from multiple
streams, and an irregular or delayed source can reduce batch formation and
increase latency.

Decision: preserve the batch-capable artifacts for future work, but use a fair
central asynchronous scheduler with batch 1 for the current live system. This
provides high utilization without waiting for cross-camera batches.

### 5.7 Isolated OpenVINO benchmark and bottleneck conclusion

`tools/benchmark_direct_preprocess.py` was used to compare:

- raw FP32 NCHW input;
- OpenVINO preprocessing from BGR `u8` NHWC;
- asynchronous request counts 1, 2, and 4; and
- direct NV12 preprocessing with request counts 1 through 4.

The isolated model test produced more than 300 frame inferences/s on the GPU.
The exact transient console output was not retained as a repository artifact,
so the result is intentionally recorded as `>300 FPS`, not as a fabricated
decimal value.

Conclusion: the model itself could run substantially faster than the original
end-to-end live rate. The bottleneck was integration overhead: per-worker model
instances, frame transfer/conversion, mux behavior, scheduling, and stream
delivery.

### 5.8 Centralized direct-OpenVINO scheduler

The primary architecture was changed to one central OpenVINO inference process.
The ingest workers now own RTSP lifecycle and VA-API decode, while the central
process owns GPU and NPU model instances.

Each ingest branch writes its newest frame to a seqlock-protected shared-memory
slot. A new frame overwrites an older unconsumed frame. This is deliberate:
real-time video analytics should drop stale work rather than build latency.

The central scheduler:

- polls all cameras fairly in round-robin order;
- submits new frames to a global OpenVINO `AsyncInferQueue`;
- uses four requests for primary and one for smoke/fire;
- keeps a frame allocation alive until its callback completes;
- decodes outputs in the callback;
- submits only cadence-approved primary frames to secondary processing; and
- publishes aggregate metrics through `/run/user/1000`.

The direct path uses system-memory NV12 rather than VA-surface sharing. A
VA-surface inference path was not retained because it was unstable in this
multi-process topology. NV12 still avoids CPU-side color conversion for every
primary frame and reduces shared-memory bandwidth compared with BGR.

The old DL Streamer inference path remains selectable with
`settings.primary_backend=dlstreamer` as a fallback.

### 5.9 Secondary analytics data flow

Secondary work is driven by primary detections, not by a second independent
decode path.

```text
RTSP packet
  -> per-camera VA-API decode to NV12
  -> latest-frame shared-memory slot
  -> OpenVINO GPU preprocessing: NV12 -> RGB -> FP32 / 255
  -> primary GPU inference
  -> CPU output decode: class, confidence, bounding box
  -> cadence admission
  -> selected NV12 frame converted to BGR on CPU
  -> CPU tracking and crop extraction
       ├── vehicle crop -> NPU plate detector
       │                    -> CPU plate crop -> NPU OCR
       ├── person region -> NPU face detector -> NPU face embedding
       ├── person crop -> NPU body ReID embedding
       ├── tracked silhouettes -> NPU gait embedding
       └── periodic full frame -> NPU scene embedding
  -> count dimensions/products
  -> discard frame, crops, text, and vectors
```

The NPU does not crop images. CPU/NumPy/OpenCV creates crops from the original
selected BGR frame using the bounding boxes returned by primary or secondary
detectors. The NPU receives prepared tensors and performs neural inference.

The secondary queue is bounded. When full, the oldest queued result is dropped
so the system remains current. The current tuned cadence prevents that policy
from being exercised during the test workload.

### 5.10 Secondary model enablement

The following features were enabled and routed to the NPU:

- license-plate detection;
- Indian alphanumeric ANPR OCR;
- SCRFD face detection;
- AdaFace face embedding;
- TransReID body embedding;
- temporal gait embedding; and
- SigLIP2 scene embedding.

Person/vehicle primary metadata is still consumed even though secondary
products are not stored. Simple IoU tracking assigns transient per-camera track
IDs. Gait collection consumes all admitted secondary frames because it needs a
temporal silhouette sequence. Face, body ReID, and plate work run less often to
control NPU pressure. Scene phases are deterministically staggered by camera so
all cameras do not submit scene inference simultaneously.

Indian ANPR accepts only these normalized forms:

- conventional state/RTO registrations; and
- Bharat Series registrations containing `BH`.

Non-matching or low-confidence OCR text is discarded.

### 5.11 Correctness finding: input color order

Decoded frames are NV12, while the Ultralytics-exported models expect normalized
RGB. A color conversion is required, but it need not occur on the CPU. The
OpenVINO preprocessing graph now performs:

```text
NV12 u8 -> RGB -> FP32 -> divide by 255 -> NCHW
```

This runs as part of the GPU graph. It replaced an incorrect BGR target.

An A/B test used 30 live frames: three frames from each of the ten unique camera
sources.

| Measurement | BGR preprocessing | RGB preprocessing | Change |
|---|---:|---:|---:|
| Total detections | 300 | 343 | +14.3% |
| Detections/frame | 10.000 | 11.433 | +14.3% |
| Mean confidence | 0.58774 | 0.61534 | Higher |
| Median confidence | 0.58177 | 0.61694 | Higher |
| Sum of confidences | 176.322 | 211.060 | +19.7% |
| Same-class boxes with IoU > 0.5 | — | 73.18% agreement | Material output change |

Decision: use RGB for both GPU models. The model metadata independently confirms
RGB/reversed-channel preprocessing.

### 5.12 Correctness finding: primary class mapping

The running code initially interpreted the primary output as a subset of COCO
classes. Inspection of the model metadata showed a custom nine-class order:

| Class ID | Label |
|---:|---|
| 0 | bus |
| 1 | car |
| 2 | minivan |
| 3 | motorcycle |
| 4 | rickshaw |
| 5 | truck |
| 6 | bicycle |
| 7 | person |
| 8 | others |

The runtime was corrected to use this exact order. Plate admission now treats
bus, car, minivan, motorcycle, rickshaw, and truck as vehicle candidates.
Bicycle and `others` are intentionally excluded from plate processing.

This correction substantially increased valid person/vehicle-driven secondary
work. It also exposed that the earlier secondary cadence was too aggressive.

### 5.13 Correctness finding: smoke/fire postprocessing

The smoke/fire model returns `[1,6,2100]`, representing 2,100 candidates with
four box values and two class scores. Previously, completed model requests were
reported as if they were detections.

The final implementation:

1. transposes candidates to rows;
2. chooses the highest class score for each row;
3. applies confidence threshold `0.35`;
4. converts center-width-height boxes to clipped corner coordinates;
5. rejects invalid boxes;
6. applies class-aware OpenCV NMS at IoU `0.70`; and
7. separately counts fire and smoke detections.

The dashboard now distinguishes frame inference throughput from postprocessed
detection throughput.

### 5.14 Secondary load balancing

After the color and class corrections, the old secondary cadence produced queue
pressure because more valid person and vehicle detections reached the NPU.
Several cadences were tested.

- At the earlier high admission rate, the bounded queue accumulated work and
  dropped hundreds of stale items.
- At 0.3 FPS/camera with every heavy stage on each admitted frame, the queue was
  stable, but gait sequences formed too slowly.
- The final balance admits 0.7 FPS/camera, gives every admitted person frame to
  tracking/gait, runs face/ReID/plate every third admitted frame, and runs scene
  embedding every seventh admitted frame.

Final decision:

```json
{
  "input_fps": 0.7,
  "person_interval": 3,
  "plate_interval": 3,
  "scene_interval": 7,
  "queue_size": 32
}
```

This produced a stable queue, zero drops, zero failures, and actual gait
embeddings while protecting primary throughput.

### 5.15 Follow-up: ROI zero-copy and host CPU profiling

The proposed future optimization was to crop object ROIs directly from decoded
VA surfaces and avoid bringing full frames through host memory. Before changing
the process topology, the running central process was sampled with `perf` and
the existing conversion/crop operations were microbenchmarked.

The profile showed that the largest host cost was not crop extraction:

- approximately 54% of sampled atom-core cycles were inside Intel's GPU compute
  runtime (`libigdrcl`), principally GPU completion polling;
- OpenCV routines represented only a few percent of samples; and
- `memmove` represented approximately 3%.

On the target host, a standalone 640x640 NV12-to-BGR conversion measured about
0.14 ms wall time with OpenCV's normal thread configuration. Live, instrumented
conversion inside the loaded pipeline averages approximately 0.25-0.31 ms.
Creating a NumPy ROI view costs less than one microsecond. At about 13-14
secondary frames/s, conversion consumes only a few milliseconds of wall time per
second. A full secondary work item averages roughly 44-47 ms because it includes
object-driven NPU inference and preprocessing.

OpenVINO's `GPU_QUEUE_THROTTLE=LOW` was then tested because Intel documents it as
a way to reduce GPU-driver CPU polling. The measured comparison was:

| Setting | Central CPU | Whole service CPU | Primary delivery | Average latency |
|---|---:|---:|---:|---:|
| `MEDIUM`, baseline | ~3.0 cores | ~4.0 cores | ~192 FPS | ~18.1 ms |
| `LOW`, tested | ~1.8 cores | ~2.7 cores | ~170 FPS | ~24-25 ms |

`LOW` saved substantial host CPU but lost roughly 11-12% primary throughput and
raised latency by about 35-40%. It was rejected because this deployment's stated
priority is maximum inference rate. The property remains configurable and the
active value is explicitly `MEDIUM`.

Two safe frame-handling improvements were retained:

1. NV12-to-BGR conversion was removed from the GPU inference completion
   callback. The callback now publishes the existing NV12 array to the bounded
   secondary queue and returns promptly, allowing the inference request to be
   reused.
2. The secondary thread converts a frame only when a scheduled plate,
   person/gait, or scene stage needs pixels. Frames with no pixel-consuming work
   remain NV12 and are released without conversion. Conversion and total
   secondary processing time are exported as metrics.

True decoded-surface zero-copy was not implemented because it is not a local
crop-function change. OpenVINO GPU remote tensors can wrap a `VASurfaceID`, but
the application must keep the `VADisplay` and surface alive until every
asynchronous request has finished. In the current design, VA decode occurs in
four worker processes and inference occurs in a fifth process. A numeric
`VASurfaceID` is not a safe cross-process payload.

A genuine zero-copy design would require one of these architectures:

- move decode and GPU inference into the same process/VA context, weakening the
  current five-camera failure isolation;
- run a GPU model instance in every ingest worker, reintroducing the model and
  scheduler duplication already shown to reduce throughput; or
- export DMA-BUF file descriptors from each worker, transfer them with Unix
  descriptor passing, import them into a central VA/OpenCL context, and maintain
  explicit surface lifetime/fence accounting. This is a significant native
  media-runtime project, not a Python crop optimization.

It would also not make the NPU stages end-to-end zero-copy. The GPU can consume
VA surfaces through its remote-tensor API, but the NPU models require their own
prepared tensors. GPU ROI preprocessing would still need a synchronized
GPU-to-NPU or GPU-to-host handoff. Given the measured 0.25 ms conversion cost,
that complexity is not justified by the present bottleneck.

## 6. Current deployed architecture

```text
MediaMTX :8654
  └── 10 FFmpeg publishers / 10 unique paths

SentryCV supervisor
  ├── Ingest worker 01: logical cameras 01-05
  ├── Ingest worker 02: logical cameras 06-10
  ├── Ingest worker 03: logical cameras 11-15
  ├── Ingest worker 04: logical cameras 16-20
  │     └── each camera: RTSP -> VA decode -> two rate-limited NV12 slots
  │                     primary 640x640@10 FPS, smoke 320x320@2 FPS
  └── Central inference process
        ├── GPU primary model, batch 1, 4 async requests
        ├── GPU smoke/fire model, batch 1, 1 async request
        ├── CPU postprocessing and bounded secondary queue
        └── Shared NPU model set for plate/OCR/face/ReID/gait/scene
```

The supervisor starts workers two seconds apart and cameras within workers 0.5
seconds apart. If one ingest worker exits, only its five-camera group is
restarted after a three-second backoff. If central inference exits, the
supervisor restarts it. A valid configuration-file change replaces the worker
generation; invalid JSON/configuration is rejected without first stopping the
healthy generation.

## 7. Active configuration

### 7.1 Primary and smoke/fire

| Setting | Primary person/vehicle | Smoke/fire |
|---|---:|---:|
| Device | GPU | GPU |
| Model input | 640x640 | 320x320 |
| Source format | NV12 | NV12 |
| Model color | RGB | RGB |
| Batch size | 1 | 1 |
| Async requests | 4 global | 1 global |
| Requested cadence | 10 FPS/camera | 2 FPS/camera |
| Confidence | 0.30 | 0.35 |
| NMS IoU | Embedded primary output | 0.70 class-aware |
| Performance hint | Throughput | Throughput |

### 7.2 Model inventory and validated tensor contracts

| Function | Active device | Input | Output |
|---|---|---|---|
| Person/vehicle | GPU | `[1,3,640,640]` at runtime | `[1,300,6]` |
| Smoke/fire | GPU | `[1,3,320,320]` | `[1,6,2100]` |
| Plate detection | NPU | `[1,3,224,224]` | `[1,300,6]` |
| ANPR OCR | NPU | `[1,3,64,128]` | `[1,15,37]` |
| SCRFD face detection | NPU | dynamic spatial input | nine score/box/landmark outputs |
| AdaFace | NPU | `[1,3,112,112]` | `[1,512]` |
| TransReID | NPU | `[1,3,256,128]` | `[1,384]` |
| GaitBase | NPU | `[1,30,64,44]` | `[1,256,16]`, flattened to 4096-D |
| SigLIP2 scene | NPU | dynamic image tensor, prepared at 224x224 | `[1,768]` |

## 8. Current RTSP workload

There are ten publishers and twenty logical consumers. Logical cameras 11-20
reuse the URLs consumed by cameras 1-10.

| Logical camera IDs | Path | Source file | Codec | Native size | Nominal FPS |
|---|---|---|---|---:|---:|
| 01, 11 | `cam01` | `05_heavy_peak.mp4` | H.264 | 1280x720 | 24.92 |
| 02, 12 | `cam02` | `07_rush_sustained.mp4` | H.264 | 1280x720 | 24.92 |
| 03, 13 | `cam03` | `11_hospital_ch7.mp4` | H.264 | 1280x720 | 25 |
| 04, 14 | `cam04` | `NVR_ch14_main_20260617100000_20260617100006.mp4` | HEVC | 2560x1440 | 25 |
| 05, 15 | `cam05` | `NVR_ch4_main_20260617110001_20260617110058.mp4` | HEVC | 2560x1440 | 25 |
| 06, 16 | `cam06` | `NVR_ch6_main_20260617110002_20260617110058.mp4` | HEVC | 2560x1440 | 25 |
| 07, 17 | `cam07` | `NVR_ch9_main_20260617100000_20260617100004.mp4` | HEVC | 2560x1440 | 25 |
| 08, 18 | `cam08` | `RAVINDRA BHARATI_PCR-2026-04-24_17h00min00s000ms.mp4` | H.264 | 1920x1080 | 25 advertised |
| 09, 19 | `cam09` | `RAVINDRA BHARATI_PCR2-2026-04-24_10h00min00s000ms.mp4` | HEVC | 1920x1080 | 25 advertised |
| 10, 20 | `cam10` | `RAVINDRA BHARATI_PCR2-2026-04-24_18h00min00s000ms.mp4` | HEVC | 1920x1080 | 25 advertised |

## 9. Tests conducted during the session

### 9.1 Test matrix

| Test | Purpose | Method | Result |
|---|---|---|---|
| Tailscale/SSH access | Prove remote command execution | SSH to `admin1@100.121.56.73` using the authorized key | Passed |
| Repository scope | Avoid modifying unrelated projects | Checked branch, status, and paths before edits | Only `sentry_cv` tracked files changed; unrelated untracked installer preserved |
| RTSP server separation | Verify streams survive CV stop | Checked MediaMTX and publisher services independently of `sentry-cv.service` | Confirmed separate lifecycle |
| Publisher readiness | Validate generated streams | UI waits for all requested publishers; process and status counts checked | 10/10 publishers active |
| Multi-consumer behavior | Confirm one URL can serve multiple clients | 20 logical cameras consumed ten paths | Passed; 20 consumers active |
| Codec/decode coverage | Exercise mixed camera formats | H.264 and HEVC files at 720p, 1080p, and 1440p | Passed using VA-API |
| DL Streamer preflight | Verify media/inference plugins and bindings | `bash edge_app/preflight.sh` | Passed |
| OpenVINO devices | Verify accelerator drivers | Queried `ov.Core().available_devices` | `CPU`, `GPU`, and `NPU` present |
| Model readability | Verify all active models load | Read all models with OpenVINO and inspected tensor contracts | Passed |
| Initial 10-camera rate | Establish baseline | 5 primary + 1 smoke FPS/camera | About 23 visible results/s; below 60 target |
| Increased 10-camera rate | Exercise 10+2 FPS/camera | Live service and rolling metrics | Pipeline ran; GPU scheduling became dominant |
| 20-camera scale | Target 200 primary + 40 smoke FPS | Duplicated ten URLs into 20 logical cameras | Sustained approximately 228-231 combined FPS after redesign |
| Worker grouping | Limit failure domain | Five cameras per worker with staggered startup | Four healthy workers for 20 cameras |
| Batch conversion | Preserve weights while enabling batch | Created dynamic and static-4 IRs; reloaded and checked shapes | Passed model-shape validation |
| DL Streamer batch path | Test live cross-camera batching | Mux/batch smoke test | Not retained; frame delivery/mux timing limited the path |
| Direct GPU benchmark | Separate model speed from live pipeline overhead | Raw, BGR-preprocess, and NV12-preprocess OpenVINO tests with multiple requests | Isolated primary model exceeded 300 FPS |
| Central scheduler | Remove per-worker model contention | One OpenVINO process, round-robin latest frames, async requests | Live primary approximately 188-192 FPS; direct callback bursts can exceed 200 |
| RGB/BGR A/B | Validate model input color | 30 live frames across ten unique sources | RGB improved detection count 14.3% and confidence sum 19.7% |
| Primary label validation | Verify semantic output mapping | Inspected embedded model metadata and synthetic rows | Correct nine-class custom order applied |
| Smoke output shape | Determine postprocessing contract | Inspected live output tensor | `[1,6,2100]` confirmed |
| Smoke synthetic parser | Validate confidence, coordinate conversion, and NMS | Injected overlapping fire boxes plus a smoke box | Duplicate fire box suppressed; fire and smoke retained |
| Smoke live parser | Validate real callback integration | Ran GPU model on live frames | 40 inferences/s and separate NMS-filtered detection counters |
| Secondary model startup | Verify requested NPU placement | Compiled/warmed every enabled secondary model | All initialized on NPU |
| Secondary overload test | Find sustainable cadence | Observed queue and drop counters at higher cadence | Excess work and stale drops exposed |
| Secondary cadence test | Balance throughput and gait continuity | Compared high rate, 0.3 FPS, and final 0.7 FPS scheme | Final queue stable; gait products generated |
| Scene staggering | Avoid simultaneous NPU bursts | Deterministic per-camera phase | Passed; approximately 2 scene embeddings/s aggregate |
| Data-retention audit | Ensure transient operation | Inspected sinks and output handling | Frames, crops, OCR strings, and vectors discarded |
| Python validation | Catch syntax/import errors | `python3 -m py_compile` on changed Python modules | Passed |
| JSON validation | Catch invalid runtime configuration | `python3 -m json.tool edge_app/config.json` | Passed |
| JavaScript validation | Catch dashboard syntax errors | `node --check dashboard/static/app.js` | Passed |
| Diff validation | Catch whitespace/patch defects | `git diff --check` | Passed |
| Service health | Detect runtime exceptions and reconnects | systemd state, journal scan, dashboard health API | Active, zero restarts, zero recent errors/disconnects at final snapshot |

### 9.2 Important interpretation of tests

- A completed inference is not the same as a detection. Primary and smoke/fire
  frame-inference rates measure completed frames. Detection rates count boxes
  remaining after confidence filtering and NMS.
- Plate, face, ReID, gait, and scene figures count generated products and depend
  on scene content. They are not expected to equal input cadence.
- The ten duplicated logical cameras intentionally count the same source content
  twice. Detection totals therefore do not represent unique real-world objects.
- No labelled evaluation set was used. The tests prove pipeline correctness,
  tensor handling, scheduling, and throughput; they do not establish precision,
  recall, or false-positive rate.

## 10. Final live inference report

### 10.1 Snapshot

Snapshot time: `2026-10-27T23:14:19+05:30`.

| Metric | Value |
|---|---:|
| SentryCV service | Active/running |
| Service restarts | 0 |
| Configured publishers | 10 |
| Active publishers | 10 |
| Logical consumers | 20 |
| Worker processes | 4 |
| Central inference status | Running |
| Combined primary + smoke frame inference | 231.5 FPS |
| Primary frame inference, derived | 191.5 FPS |
| Smoke/fire frame inference | 40.0 FPS |
| Average primary latency | 17.530 ms |
| Primary p95 latency | 28.835 ms |
| Smoke/fire detections | 11.9/s |
| Fire detections | 0.6/s |
| Smoke detections | 11.3/s |
| Secondary admitted/processed rate | 13.2 FPS |
| Secondary queue | 4/32 queued, one active at sample time |
| Secondary dropped | 0 |
| Secondary failures | 0 |
| Errors in previous 10 minutes | 0 |
| Disconnects in previous 10 minutes | 0 |

### 10.2 Cumulative product counters at snapshot

Counters below are since the most recent central inference process start.

| Product | Total | Rolling rate at snapshot |
|---|---:|---:|
| Primary frame inferences | 258,705 | 191.5/s derived |
| Smoke/fire frame inferences | 54,354 | 40.0/s |
| Smoke/fire detections | 14,524 | 11.9/s |
| Fire detections | 2,132 | 0.6/s |
| Smoke detections | 12,392 | 11.3/s |
| Secondary frames processed | 18,220 | 13.2/s |
| Plate detections | 18,494 | 13.0/s |
| Accepted Indian ANPR strings | 0 | 0.0/s |
| Face embeddings | 207 | 0.2/s |
| Body ReID embeddings | 26,126 | 17.6/s |
| Gait embeddings | 70 | 0.0/s in this ten-second window |
| Scene embeddings | 2,599 | 2.0/s |

The zero instantaneous gait rate is expected because a gait embedding is
emitted only after a sufficiently long valid moving-silhouette sequence. The
non-zero cumulative count proves that the path is active. Zero accepted ANPR
strings reflects the current traffic footage plus confidence and Indian-format
filters; it is not a model-startup failure.

### 10.3 Hardware snapshot and observed ranges

| Resource | Instantaneous snapshot | Observed steady/live range |
|---|---:|---:|
| System CPU | 34.3% | Approximately 29-40% |
| SentryCV CPU | 24.1%, 3.86 logical cores | Approximately 3.8-4.2 cores |
| Central inference CPU | 18.2%, 2.91 cores | Workload-dependent |
| Ingest CPU | 6.1%, 0.97 cores | Workload-dependent |
| System memory used | 10.24 GiB, 33.2% | Approximately 31-33% |
| SentryCV RSS/cgroup reading | 3.69 GiB at snapshot | Approximately 3.2-3.7 GiB |
| iGPU compute | 87.85% | Commonly 88-99% |
| iGPU decode | 21.63% | Commonly 21-23% |
| NPU busy | 10.85% | Bursty, previously observed approximately 10-78% |

Accelerator figures are sampled over 500 ms. The NPU is intentionally bursty
because most secondary inference is object-driven and scene submissions are
periodic. A low instantaneous NPU percentage does not mean the NPU models are
inactive.

### 10.4 Bottleneck conclusion

The iGPU compute engine is the current limiting resource. Decode is only around
one fifth utilized, so adding decode batching would not address the limiting
stage. Primary delivery is close to, but not always exactly, the nominal 200
FPS because sources and frame selectors are asynchronous. Smoke/fire reliably
reaches its 40 FPS target.

The secondary NPU queue is currently healthy. CPU cost is meaningful because it
includes shared-frame copies, selected NV12-to-BGR conversion, tracking,
cropping, postprocessing, telemetry, and process orchestration, but CPU is not
the immediate throughput ceiling.

## 11. Metrics and dashboard semantics

The dashboard polls `/api/status` approximately every two seconds.

| Dashboard field | Meaning |
|---|---|
| Output rate | Primary frame inferences/s plus smoke/fire frame inferences/s |
| Smoke/fire rate in detail text | Actual confidence/NMS-filtered boxes/s |
| iGPU compute | i915 `ccs0-busy` sampled through `perf` |
| iGPU decode | Mean of i915 `vcs0-busy` and `vcs1-busy` |
| NPU | Delta of `npu_busy_time_us` over the sample interval |
| System CPU | Whole-host busy percentage from `/proc/stat` |
| SentryCV CPU | systemd cgroup CPU normalized across 16 logical CPUs |
| Core equivalents | Unnormalized consumed CPU time expressed as fully busy cores |
| Secondary queue | Waiting work/capacity, excluding the item actively processing |

Primary runtime metrics are written atomically to
`/run/user/1000/sentry-cv-primary.json`. The supervisor merges them with worker
state into `/run/user/1000/sentry-cv-metrics.json`. These are ephemeral runtime
metrics, not analytics-result storage.

## 12. Data retention and privacy behavior

The active pipeline does not persist analytics content.

- Shared-memory frame slots are overwritten by newer frames.
- CPU frame arrays and crops are released after each work item.
- Face, body, gait, and scene embedding vectors are reduced to dimension/count
  metadata and discarded.
- OCR strings are checked and counted, then discarded.
- Bounding boxes exist only in transient Python payloads.
- No image, crop, video, embedding, plate text, or event database is written.

Only aggregate counters, rates, queue state, process identifiers, and hardware
utilization are exposed to the dashboard.

## 13. Operational procedure

Run these commands from `/home/admin1/sentry_cv` unless stated otherwise.

### 13.1 Preflight

```bash
bash edge_app/preflight.sh
```

This validates VA decode devices, NPU presence, required GStreamer/DL Streamer
elements, Python bindings, and required model files.

### 13.2 Start, stop, and inspect the CV service

```bash
systemctl --user start sentry-cv.service
systemctl --user stop sentry-cv.service
systemctl --user restart sentry-cv.service
systemctl --user status sentry-cv.service
journalctl --user -u sentry-cv.service -f
```

Stopping `sentry-cv.service` should not stop MediaMTX, the dashboard, or RTSP
publishers.

### 13.3 Inspect supporting services

```bash
systemctl --user status sentry-cv-ui.service
systemctl --user status sentry-mediamtx.service
systemctl --user status sentry-rtsp-cam01.service
```

Publisher services follow the `sentry-rtsp-camNN.service` naming pattern.

### 13.4 Validate the dashboard API

```bash
curl http://100.121.56.73:8090/api/status
```

### 13.5 Change camera or inference configuration

Edit `edge_app/config.json`. The supervisor watches its modification time,
validates camera grouping, and replaces the running generation after a valid
change. Configuration changes should be made atomically to avoid unnecessary
reloads.

### 13.6 Reproduce static/dynamic model work

```bash
.venv/bin/python tools/convert_vehicle_batch.py \
  models/traffic-v11/vehicle_yolo26n_fp16.xml \
  models/traffic-v11/vehicle_yolo26n_fp16_batch4.xml \
  --batch 4
```

Do not replace the active model without validating input layout, color order,
normalization, output shape, class order, and postprocessing contract.

### 13.7 Run the isolated GPU benchmark

The live service should be stopped or the benchmark should run in a controlled
maintenance window; otherwise it competes for the same GPU and invalidates both
measurements.

```bash
.venv/bin/python tools/benchmark_direct_preprocess.py \
  --model models/traffic-v11/vehicle_yolo26n_fp16_dynamic.xml \
  --iterations 1000
```

## 14. Repository changes produced by the work

| Commit | Purpose |
|---|---|
| `d9e185e` | Built the streamlined Intel edge inference pipeline |
| `cee3117` | Added Intel DL Streamer installation guidance |
| `2e50084` | Added centralized OpenVINO scheduling, shared frames, supervisor, dashboard, and batch-model tools/artifacts |
| `a88e562` | Enabled transient NPU secondary analytics |
| `df37a39` | Exposed plate throughput in the dashboard |
| `465c44c` | Enabled NPU scene embeddings and added runtime reporting |
| `537cef5` | Staggered scene embeddings across cameras |
| `7485a1e` | Corrected RGB preprocessing, model class mapping, smoke/fire postprocessing, metrics, and secondary cadence |

Principal runtime files:

| File | Responsibility |
|---|---|
| `edge_app/config.json` | Camera, device, cadence, model, and worker configuration |
| `edge_app/pipeline.py` | Per-camera GStreamer RTSP ingest and VA decode graph |
| `edge_app/shared_frames.py` | Lock-free latest-frame shared-memory slots |
| `edge_app/direct_inference.py` | Central GPU scheduling, model postprocessing, and secondary admission |
| `edge_app/secondary.py` | CPU tracking/cropping and shared NPU secondary pipeline |
| `edge_app/supervisor.py` | Worker lifecycle, restart isolation, configuration reload, and aggregate metrics |
| `dashboard/server.py` | Control API, RTSP generation orchestration, health, and hardware telemetry |
| `dashboard/static/` | Browser UI |
| `tools/convert_vehicle_batch.py` | Batch-aware primary IR conversion utility |
| `tools/benchmark_direct_preprocess.py` | Isolated OpenVINO preprocessing/concurrency benchmark |

## 15. Known limitations and risks

1. **True live batch-4 inference is not active.** The active system uses batch 1
   with four asynchronous requests. The batch-4 files are validated artifacts,
   not proof of a production-quality cross-camera batch scheduler.
2. **Central inference is a larger failure domain.** Ingest failures remain
   isolated to five cameras, but a central inference crash briefly affects all
   cameras until the supervisor restarts it.
3. **The direct path is not end-to-end VA zero-copy.** Decode remains on the
   iGPU, but frames cross through bounded system-memory NV12 slots. This was a
   stability/performance tradeoff.
4. **Secondary BGR conversion uses CPU.** It is limited to admitted secondary
   frames, approximately 14/s aggregate, rather than all 200 primary frames/s.
5. **Current videos are not a labelled evaluation corpus.** Smoke/fire and plate
   counts may include false positives. Accuracy must be evaluated separately.
6. **ANPR output is currently zero.** This is consistent with traffic footage,
   crop quality, and strict Indian-format filtering, but it warrants a dedicated
   labelled plate test before production use.
7. **Duplicated URLs are a load test.** They do not represent 20 independent
   camera scenes or independent network behavior.
8. **The iGPU has limited headroom.** A 30-camera target at the same 10+2 FPS
   rates requests 360 frame inferences/s and is unlikely to fit without true
   batching, a smaller model/resolution, cadence reduction, or more hardware.
9. **Metrics are rolling and burst-sensitive.** Especially NPU and per-class
   detection rates, a single dashboard sample should not be treated as a
   long-duration capacity result.

## 16. Recommended next steps

### 16.1 Establish repeatable accuracy tests

Create a labelled, legally approved evaluation set for:

- the nine primary classes;
- smoke and fire;
- Indian plate detection and OCR;
- face visibility/quality;
- identity consistency for face and ReID; and
- gait sequence validity.

Report precision, recall, mAP, OCR exact-match rate, and false-positive rate
separately from throughput.

### 16.2 Run a controlled soak test

Run the current 20-camera profile for at least 8-24 hours and record:

- per-camera input and inference rate;
- latency p50/p95/p99;
- queue depth, drops, failures, and restarts;
- RTSP reconnects;
- CPU, memory, GPU decode/compute, and NPU busy distributions; and
- temperature and throttling counters.

### 16.3 Implement real micro-batching only with bounded wait

If the CV team supplies a native batch-4 IR, validate it against batch-1 output
first. A production micro-batcher should:

- take at most one latest frame per camera;
- use a small maximum batch wait, for example 2-5 ms;
- allow partial or dynamic batches when the model supports them;
- never wait indefinitely for a slow camera;
- restore outputs to camera IDs deterministically; and
- compare throughput and p95 latency against the current async batch-1 path.

Batching should be retained only if the end-to-end live result improves, not
merely because an isolated model benchmark improves.

### 16.4 Plan the 30-camera profile from measured capacity

At current rates, the 20-camera target already drives iGPU compute close to
saturation. Before enabling 30 cameras, test one or more of:

- lower primary cadence for low-priority cameras;
- lower smoke/fire cadence where risk policy permits;
- a smaller or lower-resolution primary model;
- native GPU batch-4 with bounded-wait scheduling;
- priority scheduling between primary and smoke/fire; or
- distributing cameras across multiple accelerator hosts.

### 16.5 Improve observability

Add long-duration time-series export for rate, latency percentiles, queue state,
accelerator busy time, thermals, and per-camera freshness. Preserve only
operational aggregates to maintain the current no-content-retention policy.

## 17. Final conclusion

The final system is a healthy 20-logical-camera edge inference pipeline with
per-camera VA-API decode, isolated five-camera ingest workers, centralized
asynchronous OpenVINO GPU inference, and shared NPU secondary analytics. It
sustains approximately 228-231 combined frame inferences/s under the current
mixed H.264/HEVC workload, with zero secondary drops or failures in the final
validation window.

The main throughput limitation is iGPU compute, not decode. The most important
correctness fixes were RGB preprocessing, use of the model's actual nine-class
mapping, and real smoke/fire confidence/NMS postprocessing. The most important
stability decisions were latest-frame shared-memory slots, bounded secondary
work, staggered startup/scene submissions, and separating ingest failure domains
from centralized inference scheduling.

The architecture is now suitable for continued optimization and controlled
batching experiments, but the 30-camera goal at unchanged per-camera rates
requires additional measured optimization rather than a configuration-only
increase.
