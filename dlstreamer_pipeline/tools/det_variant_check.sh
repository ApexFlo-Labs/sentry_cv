#!/bin/bash
# Output check, FP16 vs another detector build, on the recorded clips, every frame, box by box.
#   det_variant_check.sh MODEL_NAME ["cam01 cam03 cam05"]   (MODEL_NAME = file in models/, without .xml)
V=$1; CAMS=${2:-"cam01 cam03 cam05"}
C=~/chandana_optimization/cascade; D=$C/detcheck
source /opt/intel/dlstreamer/scripts/setup_dls_env.sh >/dev/null 2>&1
for cam in $CAMS; do
  [ -s $D/${cam}_v11_fp16.jsonl ] || { echo "$cam: no FP16 output to compare with"; continue; }
  rm -f $D/${cam}_$V.jsonl
  timeout 300 gst-launch-1.0 -q filesrc location=$D/$cam.mp4 ! decodebin3 ! vapostproc ! "video/x-raw(memory:VAMemory),format=NV12" \
    ! gvadetect model=$C/models/$V.xml model-proc=$C/v11_model_proc.json device=GPU pre-process-backend=va-surface-sharing nireq=4 threshold=0.3 \
    ! gvametaconvert add-empty-results=true ! gvametapublish method=file file-format=json-lines file-path=$D/${cam}_$V.jsonl \
    ! fakesink sync=false > /dev/null 2>&1 || echo "$cam $V: run ended with an error"
  python3 $C/compare_det.py $D/${cam}_v11_fp16.jsonl $D/${cam}_$V.jsonl "$cam FP16 vs $V" | sed 's/GPU/FP16/g; s/NPU/new/g'
done
