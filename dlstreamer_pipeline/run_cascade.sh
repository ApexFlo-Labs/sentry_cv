#!/bin/bash
# Native DL Streamer cascade speed test, no Python frame copies.
#   run_cascade.sh STREAMS SECS [det|track|scrfd|faces]   (streams beyond 10 reuse cam01..cam10)
# det / track / scrfd / faces add one stage each; faces = the full chain:
#        detector (GPU) -> tracker -> SCRFD on person boxes every 3rd frame (NPU) -> AdaFace on faces (NPU)
# det:   detector only, the ceiling for this stream count
# SHARE_NPU=0 gives every camera its own NPU model copies (use it if the shared run stalls at 0 fps).
# COUNT=0 drops the Python face counter. Face-stage settings: FDET (top1 = decode in the model, raw = gvainference + Python decode), FACE_DEV (NPU), PRE (pre-process-backend, e.g. va), NIREQ, DECODE=0 skips the Python decode,
# SCRFD_DEV / ADA_DEV set each face model's device; DET_NPU_EVERY=k moves every k-th detector to the NPU.
# QMAX caps inner queues; ADA_CFG = ie-config for AdaFace; FACE_REID=N embeds each tracked face every N-th time.
# SEC=1 adds his secondary analytics (plates/OCR, ReID, gait, scene). FIRE_SCALE=1 shrinks fire/smoke frames on the GPU; FIRE_COUNT=1 adds fire/smoke frames to the total. FINT = face detector runs on every FINT-th frame (default 3). FIRE_FPS = fire/smoke frames per second per camera (default 1). FTH = face detector threshold (default 0.55, as SentryCV). DET_SHARED=1 / DET_BATCH=n / DET_NIREQ=n: one shared, batched detector. ALIGN=1 aligns faces inside the embedding model. FIRE_DEV adds fire/smoke on a side branch at 1 frame/s per camera. TRACE=1 adds a per-step time table (GStreamer latency tracer). TAG names the run.
N=${1:-8}; SECS=${2:-120}; MODE=${3:-faces}; SHARE_NPU=${SHARE_NPU:-1}
H=~/chandana_optimization; C=$H/cascade
source /opt/intel/dlstreamer/scripts/setup_dls_env.sh >/dev/null 2>&1
# DET_MODEL / DET_PROC_FILE swap the detector (e.g. the 9-class traffic-v11 model, made static by prepare_v11.py)
DET=${DET_MODEL:-$H/sentinel_img13/rootfs/models/traffic/openvino/vehicle_yolo26n_fp16.xml}
DET_PROC=${DET_PROC_FILE:-$H/sentrycv/edge_app/vehicle_yolo26_model_proc.json}
FIRE=$H/sentinel_img13/rootfs/models/traffic/openvino/smoke_fire.xml
FIRE_PROC=${FIRE_PROC_FILE:-$H/sentrycv/edge_app/smoke_fire_model_proc.json}
# per-model device: SCRFD_DEV / ADA_DEV (default FACE_DEV, default NPU); on the GPU the crop stays zero-copy
SDEV=${SCRFD_DEV:-${FACE_DEV:-NPU}}; ADEV=${ADA_DEV:-${FACE_DEV:-NPU}}
opts() { [ "$1" = GPU ] && echo "pre-process-backend=va-surface-sharing ${NIREQ:+nireq=$NIREQ}" || echo "${PRE:+pre-process-backend=$PRE} ${NIREQ:+nireq=$NIREQ}"; }
SOPTS=$(opts $SDEV); AOPTS=$(opts $ADEV)
# QMAX caps the queues between stages (each queued frame holds a GPU video-memory slot)
Q="queue${QMAX:+ max-size-buffers=$QMAX max-size-bytes=0 max-size-time=0}"
# ADA_CFG passes OpenVINO settings to the embedding model, e.g. PERFORMANCE_HINT=CUMULATIVE_THROUGHPUT
[ -n "${ADA_CFG:-}" ] && AOPTS="$AOPTS ie-config=$ADA_CFG"
P=""
for ((i = 0; i < N; i++)); do
  cam=cam$(printf %02d $((i % 10 + 1)))
  # LAST_CAM replaces the last stream's path (drop_test.sh uses its own test path there)
  [ -n "${LAST_CAM:-}" ] && [ $i = $((N - 1)) ] && cam=$LAST_CAM
  [ "$SHARE_NPU" = 1 ] && npu=shared || npu=$i
  P+=" urisourcebin uri=rtsp://127.0.0.1:8654/$cam ! decodebin3 ! vapostproc ! video/x-raw(memory:VAMemory),format=NV12"
  # the cap is enforced the way SentryCV does it: videorate drop-only + a fixed frame rate in the caps
  # (max-rate alone let some streams run at ~20 fps); all test streams run at 18-25 fps, above the cap
  P+=" ! videorate drop-only=true ! video/x-raw(memory:VAMemory),framerate=${CAP:-15}/1"
  # FIRE_DEV=NPU|CPU|GPU: fire/smoke on its own branch at 1 frame/s, as SentryCV does, so its boxes
  # never mix with the person/face chain
  FB=""
  if [ -n "${FIRE_DEV:-}" ]; then
    P+=" ! tee name=t$i"
    FB=" t$i. ! queue leaky=downstream max-size-buffers=2 ! videorate drop-only=true max-rate=${FIRE_FPS:-1}"
    # the 1 frame/s goes to normal memory first (CPU and the CPU-made crop cannot read GPU memory)
    # 3-channel BGR: the fire model's padding colour has 3 values, so 4-channel BGRx crashes its pre-processing
    # FIRE_SCALE=1: the GPU shrinks the frame to the model size (320x320, letterboxed) before it is
    # copied out, so the CPU converts a small frame instead of a full 1080p/1440p one
    [ "${FIRE_SCALE:-0}" = 1 ] && fsz=",width=320,height=320" && fvp="vapostproc add-borders=true" || { fsz=""; fvp="vapostproc"; }
    FB+=" ! $fvp ! video/x-raw,format=BGRx$fsz ! videoconvert ! video/x-raw,format=BGR"
    FB+=" ! gvadetect model=$FIRE model-proc=$FIRE_PROC device=$FIRE_DEV pre-process-backend=opencv"
    # FIRE_COUNT=1 counts fire/smoke frames too, so the total = detection + fire/smoke (as his dashboard counts)
    FB+="   threshold=0.35 model-instance-id=fire-$npu ${FIRE_COUNT:+! gvafpscounter interval=10} ! fakesink sync=false async=false"
  fi
  P+=" ! queue leaky=downstream max-size-buffers=4"
  # DET_NPU_EVERY=k puts every k-th camera's person/vehicle detector on the NPU (spill-over)
  if [ -n "${DET_NPU_EVERY:-}" ] && [ $((i % DET_NPU_EVERY)) = $((DET_NPU_EVERY - 1)) ]; then
    P+=" ! gvadetect model=$DET model-proc=$DET_PROC device=NPU pre-process-backend=${DET_NPU_PRE:-va}"
    dnpu=-npu  # its own shared instance: one model instance cannot serve two devices
  else
    dnpu=""
    P+=" ! gvadetect model=$DET model-proc=$DET_PROC device=GPU pre-process-backend=va-surface-sharing"
  fi
  # DET_SHARED=1: one detector instance for all cameras (one request pool); DET_BATCH=n batches frames
  [ "${DET_SHARED:-0}" = 1 ] && did=det-shared$dnpu || did=det$i
  P+="   model-instance-id=$did nireq=${DET_NIREQ:-6} ${DET_BATCH:+batch-size=$DET_BATCH} threshold=0.3 ! $Q"
  # stages are added in order, so each mode adds one step: det -> track -> scrfd -> faces
  [ "$MODE" != det ] && P+=" ! gvatrack tracking-type=zero-term-imageless ! $Q"
  # SEC=1: his secondary analytics (plates + OCR, body ReID, gait, scene; secondary_branch.py) on a side
  # branch at 1 frame/s per camera, shrunk on the GPU to 640x360 before it is copied out
  if [ "${SEC:-0}" = 1 ]; then
    P+=" ! tee name=s$i ! $Q"
    # the boxes are read before the shrink (it drops box metadata) and matched by camera key + timestamp
    FB+=" s$i. ! queue leaky=downstream max-size-buffers=2 ! videorate drop-only=true max-rate=1"
    FB+=" ! gvapython module=$C/secondary_branch.py class=Stash function=process kwarg={\"key\":$i}"
    FB+=" ! vapostproc ! video/x-raw,format=BGRx,width=640,height=360"
    FB+=" ! gvapython module=$C/secondary_branch.py class=Secondary function=process kwarg={\"key\":$i}"
    FB+=" ! fakesink sync=false async=false"
  fi
  if [ "$MODE" = scrfd ] || [ "$MODE" = faces ]; then
    if [ "${FDET:-top1}" = top1 ]; then
      # decode inside the model: gvadetect makes the face box itself, no Python
      # ALIGN=1: the square face box for the aligned embedding (prepare_align.py)
      [ "${ALIGN:-0}" = 1 ] && fmodel=scrfd_top1x_u8.xml || fmodel=scrfd_top1_u8.xml
      # FPRE overrides the face detector's crop path (opencv = CPU-made crop, va = VA hardware crop)
      [ -n "${FPRE:-}" ] && SOPTS_I="pre-process-backend=$FPRE ${NIREQ:+nireq=$NIREQ}" || SOPTS_I="$SOPTS"
      P+=" ! gvadetect model=$C/models/$fmodel model-proc=$C/scrfd_top1_proc.json device=$SDEV $SOPTS_I"
      P+="   inference-region=roi-list object-class=person inference-interval=${FINT:-3} threshold=${FTH:-0.55} model-instance-id=scrfd1-$npu ! $Q"
      # FACE_REID=N (output change, conditional): track faces, embed each tracked face only every N-th time
      [ -n "${FACE_REID:-}" ] && P+=" ! gvatrack tracking-type=zero-term-imageless ! $Q"
    else
      P+=" ! gvainference model=$C/models/scrfd_500m_320_u8.xml model-proc=$C/scrfd_proc.json device=$SDEV $SOPTS"
      P+="   inference-region=roi-list object-class=person inference-interval=3 model-instance-id=scrfd-$npu ! queue"
      [ "${DECODE:-1}" = 1 ] && P+=" ! gvapython module=$C/cascade_faces.py function=decode"
    fi
  fi
  # ALIGN=1: landmarks + alignment + AdaFace in one model (face_align_u8.xml), as SentryCV's Python
  # code does them; DL Streamer's own alignment fails on this version ("Unsupported layout for image")
  [ "${ALIGN:-0}" = 1 ] && emodel=face_align_u8.xml || emodel=adaface_u8.xml
  [ "$MODE" = faces ] && P+=" ! gvaclassify model=$C/models/$emodel device=$ADEV $AOPTS object-class=face ${FACE_REID:+reclassify-interval=$FACE_REID} model-instance-id=ada-$npu ! $Q"
  # the Python face counter costs speed: COUNT=0 for speed runs
  [ "${COUNT:-1}" = 1 ] && { [ "$MODE" = scrfd ] || [ "$MODE" = faces ]; } && P+=" ! gvapython module=$C/cascade_faces.py function=count"
  P+=" ! gvafpscounter interval=10 ! fakesink sync=false async=false$FB"
done
[ -n "${PRINT_ONLY:-}" ] && { echo "gst-launch-1.0 -e $P"; exit 0; }
NAME=${MODE}_s${N}$([ "$SHARE_NPU" = 1 ] || echo _own)${TAG:+_$TAG}
mkdir -p $C/runs; LOG=$C/runs/$NAME.log
echo "   $NAME: $N streams, $SECS s -> $LOG"
# usage: CPU of gst-launch (average over its life) and NPU busy share, sampled 5 s before the end
NPU_BUSY=/sys/class/accel/accel0/device/npu_busy_time_us
npu0=$(cat $NPU_BUSY 2>/dev/null || echo 0)
( sleep $((SECS - 5)); ps -C gst-launch-1.0 -o pcpu= | head -1 > $LOG.cpu; cat $NPU_BUSY > $LOG.npu 2>/dev/null ) &
# whole-system CPU (all processes, over 10 s in the middle of the run), RAM in use, and the pipeline's own memory
( sleep $((SECS / 2)); a=($(head -1 /proc/stat)); sleep 10; b=($(head -1 /proc/stat))
  idle=$(( (b[4] + b[5]) - (a[4] + a[5]) )); tot=0; for k in 1 2 3 4 5 6 7 8; do tot=$(( tot + b[k] - a[k] )); done
  rss=$(ps -C gst-launch-1.0 -o rss= | head -1)
  echo "system CPU $(( 100 * (tot - idle) / tot ))% | RAM used $(free -m | awk '/Mem:/ {printf "%.1f GB of %.1f GB", $3/1024, $2/1024}') | pipeline memory $(( ${rss:-0} / 1024 )) MB" > $LOG.sys ) &
# GPU engine busy, measured over 20 s in the middle of the run
( sleep $((SECS / 2 - 10)); pid=$(pgrep -n -x gst-launch-1.0)
  [ -n "$pid" ] && python3 $C/gpu_busy.py $pid 20 > $LOG.gpu 2>&1 || echo "not measured (pipeline had stopped)" > $LOG.gpu ) &
# TRACE=1 logs how long each step takes per frame (GStreamer latency tracer); costs some speed
[ "${TRACE:-0}" = 1 ] && export GST_TRACERS="latency(flags=element)" GST_DEBUG="GST_TRACER:7"
t0=$(date +%s)
timeout -s INT -k 15 $SECS gst-launch-1.0 -e $P > $LOG 2>&1
ran=$(( $(date +%s) - t0 ))
wait
cpu=$(tr -d ' ' < $LOG.cpu 2>/dev/null); npu1=$(cat $LOG.npu 2>/dev/null || echo $npu0)
npu=$(( (npu1 - npu0) / ((SECS - 5) * 10000) ))
# the counter prints a running average about once a second: report the median of the second half
# (after warm-up), leaving out the last 3 lines where the run is being stopped
vals=$(grep -o "FpsCounter(average[^)]*): total=[0-9.]*" $LOG | sed 's/.*total=//' | head -n -3)
n=$(echo "$vals" | grep -c .)
fps=$([ "$n" -ge 4 ] && echo "$vals" | tail -n $((n - n / 2)) | sort -n | awk '{v[NR] = $1} END {printf "%.1f", v[int((NR + 1) / 2)]}' || echo none)
faces=$(grep "^cascade:" $LOG | tail -n +4 | awk '{f += $7; e += $9; p += $5; n++}
      END {if (n) printf "people %.1f/s, faces %.1f/s, embeddings %.1f/s", p / n, f / n, e / n}')
# per-stream rates from the last overall line; counting each stream at most 15 fps keeps streams that
# ignore the frame-rate cap from inflating the total
# the last running-average line covers the whole run per stream (the final lines at shutdown cover < 1 s)
streams=$(grep -a "FpsCounter(average" $LOG | tail -1 | grep -o "([0-9., ]*)" | tail -1 | tr -d '()')
capped=$(echo "$streams" | tr ',' '\n' | awk -v c=${CAP:-15} '{if ($1 > c) $1 = c; s += $1; n++} END {if (n) printf "%.1f", s}')
echo "   RESULT $NAME: total $fps fps, capped at ${CAP:-15}/stream ${capped:-?} (max possible $((N * ${CAP:-15}))) | CPU ${cpu:-?}% (of 1600) NPU ${npu}% ${faces:+| $faces}"
echo "   pipeline ran $ran s of $SECS s"
echo "   GPU engines: $(cat $LOG.gpu 2>/dev/null)"
echo "   $(cat $LOG.sys 2>/dev/null)"
[ "${TRACE:-0}" = 1 ] && python3 $C/trace_summary.py $LOG
[ "${SEC:-0}" = 1 ] && python3 $C/sec_summary.py $LOG
echo "   per stream (whole run): ${streams:-?}"
echo "   running average, every ~10th: $(grep -o "FpsCounter(average[^)]*): total=[0-9.]*" $LOG | sed 's/.*total=//' | awk 'NR % 10 == 1' | tr '\n' ' ')"
grep -iE "error|critical|failed" $LOG | grep -v "^cascade" | sort | uniq -c | head -5
[ "$fps" = none ] && { echo "   last log lines:"; tail -4 $LOG | cut -c1-200; }
