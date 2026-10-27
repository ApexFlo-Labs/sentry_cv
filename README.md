# SentryCV Intel Edge Pipeline

Minimal bare-metal computer-vision runtime for Intel Core Ultra 9 285H.

## Pipeline

```text
Supervisor
  -> four ingest workers, five cameras each
     -> independent RTSP ingest and VA-API decode per camera
     -> primary frames selected at 10 FPS and smoke/fire at 2 FPS
     -> system-memory NV12 latest-frame slots (bounded, no backlog)
  -> one central OpenVINO GPU scheduler
     -> one vehicle/person model with four asynchronous requests
     -> one smoke/fire model with one asynchronous request
     -> fair round-robin camera scheduling and Python metadata sink
```

Secondary analytics code remains available but is currently disabled in the
configuration. The legacy DL Streamer primary path remains available by setting
`settings.primary_backend` to `dlstreamer`. The direct path deliberately uses
system-memory NV12: it avoids the unstable VA-surface inference path while also
avoiding BGR conversion and halving shared-frame bandwidth.

## Run

Install the packages listed in [`edge_app/requirements.txt`](edge_app/requirements.txt)
along with Intel DL Streamer, OpenVINO, GStreamer VA plugins and the Intel NPU
driver. Then:

```bash
bash edge_app/preflight.sh
edge_app/run.sh
```

Configure cameras, worker group size and inference settings in
[`edge_app/config.json`](edge_app/config.json). The supervisor restarts only a
failed five-camera worker and replaces all workers when the configuration changes.



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

