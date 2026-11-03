#!/bin/bash
# Multi-camera capacity test: N clients of the local RTSP stream, one model on the Metis card.
cd ~/chandana_axelera
FPS=${FPS:-10}; SECS=${SECS:-30}; MODEL=${MODEL:-yolov8n-coco}; ROOT=${ROOT:-build}
URL=rtsp://127.0.0.1:8554/cam1
for N in "$@"; do
  srcs=$(for i in $(seq $N); do printf "%s " $URL; done)
  echo "=== $MODEL N=$N cams, $FPS fps/cam, target $((N*FPS)) fps | $(uptime | sed 's/.*load/load/')"
  ( sleep $((SECS/2)); timeout 5 axmonitor --ui console -c print 2>/dev/null \
      | grep "Core Utilization" | head -5 | tail -4 | sed 's/^/  card:/' ) &
  ~/voyager-sdk-latest/inference.py $MODEL $srcs --metis m2 --no-display --show-stats \
    --enable-vaapi --enable-hardware-codec --frame-rate $FPS --frames $((N*FPS*SECS)) \
    --build-root ~/chandana_axelera/$ROOT --data-root ~/chandana_axelera/data \
    > runs_multicam_${MODEL}_N${N}.log 2>&1
  echo "  exit $?"; grep -E "End-to-end :|CPU %|Latency" runs_multicam_${MODEL}_N${N}.log | sed 's/^/  /'
  wait
done
