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



 sudo wget -qO /usr/share/keyrings/dls-key.gpg https://apt.repos.intel.com/edgeai/dlstreamer/GPG-PUB-KEY-INTEL-DLS.gpg && echo "deb [signed-by=/usr/share/keyrings/dls-key.gpg]
  https://apt.repos.intel.com/edgeai/dlstreamer/ubuntu24 ubuntu24 main" | sudo tee /etc/apt/sources.list.d/intel-dlstreamer.list >/dev/null && sudo apt update && sudo apt install -y intel-dlstreamer

  Then verify:

  gst-inspect-1.0 gvadetect

  For Ubuntu 22.04, replace ubuntu24 with ubuntu22.


• On Ubuntu 24.04, use Intel’s prerequisite installer first:

  wget -O DLS_install_prerequisites.sh https://raw.githubusercontent.com/open-edge-platform/dlstreamer/main/scripts/DLS_install_prerequisites.sh
  chmod +x DLS_install_prerequisites.sh
  sudo ./DLS_install_prerequisites.sh --reinstall-npu-driver=yes

  Reboot:

  sudo reboot

  Then install DL Streamer:

  sudo wget -qO /usr/share/keyrings/dls-key.gpg https://apt.repos.intel.com/edgeai/dlstreamer/GPG-PUB-KEY-INTEL-DLS.gpg

  echo "deb [signed-by=/usr/share/keyrings/dls-key.gpg] https://apt.repos.intel.com/edgeai/dlstreamer/ubuntu24 ubuntu24 main" |
  sudo tee /etc/apt/sources.list.d/intel-dlstreamer.list

  sudo apt update
  sudo apt install -y intel-dlstreamer

  Verify:

  gst-inspect-1.0 gvadetect
  bash edge_app/preflight.sh

