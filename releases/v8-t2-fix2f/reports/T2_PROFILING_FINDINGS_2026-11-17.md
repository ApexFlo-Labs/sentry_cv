# T2 profiling: where it breaks and why (.64, 8 FPS per camera, 2026-11-17)

Data: `data/runs/prof-*` (probe reports), `data/prof/prof-*` (1 s host samples: intel_gpu_top engines, NPU busy, per-thread CPU),
GStreamer per-element latency tracer for the all-apps 1..6 camera runs. Scripts: `analyze_B.py`, `mine_existing.py`.

## All apps (SA), people video, 8 FPS, adding cameras

| cams | result | cam FPS | GPU Compute | GPU VideoEnh | GPU Video(decode) | NPU | system CPU | colour conversion per frame (mean / p95) | wait before vehicle detector |
|---|---|---|---|---|---|---|---|---|---|
| 1 | pass | 7.97 | 15% | 3% | 2% | 21% | 11% | 57 / 86 ms | ~0 |
| 2 | pass | 7.97 | 29% | 6% | 3% | 34% | 21% | 57 / 96 ms | ~0 |
| 3 | pass | 7.97 | 39% | 9% | 6% | 41% | 30% | 57 / 105 ms | ~0 |
| 4 | fail with tracer on (passes 5 of 6 clean runs) | 7.97 | 50% | 13% | 8% | 42% | 41% | 65 / 115 ms | small |
| 5 | fail | 6.9 (all cams) | 55% | 16% | 10% | 58% | 52% | queues blocked | ~410 ms |
| 6 | fail | 6.4-6.6 (all cams) | 60% | 18% | 12% | 61% | 58% | queues blocked | ~446 ms |

Frame budget at 8 FPS = 125 ms. Per camera, ONE thread runs: GPU colour conversion -> CPU videoconvert (1 thread, 1080p BGR)
-> sentinelgait -> sentinelplate -> sentinelbody. Its p95 reaches ~115 ms at 4 cams. When it falls behind, back-pressure reaches the
vehicle detector, whose inference instance is shared by all cameras, so every camera slows together (5 and 6 cams: all at 6.4-6.9 FPS).
No hardware unit is full at the break (GPU compute 55-60%, NPU 58-61%, CPU 52-58%).

## Each use case alone, 8 FPS (most cameras that pass)

| profile | apps | max cams | what is busiest at the limit |
|---|---|---|---|
| S | scene search | 13 (14 once) | GPU compute (main campaign) |
| SH | scene + fire | 13 | GPU compute 89-94% |
| SC | scene + counting | 10 | GPU compute 83-90% |
| SP | scene + presence | 9 (10 passed FPS, failed one scene-sample check) | GPU compute 72-90% |
| SX | scene + presence + counting + entry/exit | 7 (8, 9 fail one 10 s window) | GPU compute 60-75% |
| SIB | + body ReID | 7 | per-camera thread (busiest 77-89%), NPU 49-57% |
| SIF | + face ID | 5 | GPU compute 57-62% |
| SIFB | + face + body | 4 | per-camera thread / NPU 41-51% |
| SIFBG | + face + body + gait | 4 | per-camera thread / NPU 45-54% |
| SV | vehicle + ANPR (ANPR video) | 5 | NPU 56-67% (plate + OCR, ~33 plate checks/s/cam) |
| SVX | vehicle + ANPR + counting + entry/exit (ANPR video) | 4 | NPU ~67% at 6 cams (plate + OCR) |
| SA | all apps | 3-4 | per-camera thread + shared detector back-pressure |

Presence/counting cameras convert every frame to full-size BGR for Python (8/s/cam); scene and fire-only cameras do not
(about 1.4/s/cam), which is the visible difference between SH 13 and SP 9.

## Verified mixes (8 FPS)
- 8 cams: 7 scene + 1 full identity: pass. 8 cams: 6 scene + 2 full identity: pass.
- 8 cams: 4 people identity + 4 vehicle (ANPR): fail (cams at 58%).

## Settings already in T2 that target the found bottleneck (not yet tested)
- `SENTINEL_T2_CONVERT_THREADS` (videoconvert threads, now 1)
- `SENTINEL_T2_STAGE_QUEUES=1` (queues between conversion, gait, plate, body so they run on separate threads)
