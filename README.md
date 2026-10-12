# SentryCV Intel Edge Pipeline

Minimal bare-metal computer-vision runtime for Intel Core Ultra 9 285H.

## Pipeline

```text
RTSP -> VA-API decode -> NV12 VAMemory
  -> shared vehicle/person GPU model at 5 FPS
  -> shared smoke/fire GPU model at 1 FPS
  -> CPU BGR download, tracking, cropping and alignment
  -> CPU plate/OCR and face detection
  -> NPU face, body ReID, gait and scene embeddings
  -> discard sink
```

No management server, Redis, MQTT, Kafka or cloud delivery is used.

## Run

Install the packages listed in [`edge_app/requirements.txt`](edge_app/requirements.txt)
along with Intel DL Streamer, OpenVINO, GStreamer VA plugins and the Intel NPU
driver. Then:

```bash
bash edge_app/preflight.sh
python3 -m edge_app.main --config edge_app/config.json
```

Configure cameras and inference intervals in [`edge_app/config.json`](edge_app/config.json).
