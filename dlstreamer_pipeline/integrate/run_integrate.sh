#!/bin/bash
# Integration test (CAMS cameras, default 4; DESIRED desired-state file) on argus-3: stand-in delivery agent + our Option 5 engine with Kiranmai's V8 event code.
# Same test clips as before (gi00-gi03 on port 8655). cam01's feed is stopped for 60 s at 4 min to test
# camera_health / track_lost / rejoin events. Default 600 s. Output in integrate/run/<time>/ (run/latest).
# Touches only ~/chandana_optimization/integrate and port 8655.
H=~/chandana_optimization; I=$H/integrate; G=$H/group_ingest; cd $I || exit 1
DUR=${1:-600}
N=${CAMS:-4}; DESIRED=${DESIRED:-desired_state.json}
ss -ltn | grep -q ":8655 " && { echo "port 8655 busy"; exit 1; }
pgrep -f "[i]ngest_nomux.py|[i]ngest_gates.py|[i]ntegrate[0-9]*.py" >/dev/null && { echo "a pipeline is already running"; exit 1; }
V=$H/chandana_optimization_videos; CLIPS=("$V/plate_clip_35-45min.mp4" "$V/PCR2_18h.mp4" "$V/PCR2_10h.mp4" "$V/NVR_ch14.mp4")
mkdir -p secrets; for i in $(seq 0 $((N - 1))); do printf "rtsp://127.0.0.1:8655/gi%02d\n" $i > secrets/cam$(printf %02d $i).url; done
R=$I/run/$(date +%Y%m%d-%H%M%S); mkdir -p $R/state; ln -sfn $R $I/run/latest
MOCK_NO_SAVE=${MOCK_NO_SAVE:-} MOCK_SAVE_EVERY=${MOCK_SAVE_EVERY:-} python3 mock_agent.py $R/agent.sock $R > $R/agent.out 2>&1 &
AGENT=$!
FEEDS=()
feed() { ffmpeg -nostdin -hide_banner -loglevel error -re -stream_loop -1 -i "${CLIPS[$(($1 % 4))]}" -an -c:v copy -fflags +genpts -f rtsp -rtsp_transport tcp rtsp://127.0.0.1:8655/gi$(printf %02d $1) 2>/dev/null & FEEDS[$1]=$!; }
$H/rtsp/mediamtx $G/gi-mediamtx.yml > $R/mediamtx.out 2>&1 &
MTX=$!
sleep 2; for i in $(seq 0 $((N - 1))); do feed $i; done; sleep 5
trap 'kill $AGENT $MTX ${FEEDS[@]} 2>/dev/null' EXIT
SETUP="source /opt/intel/dlstreamer/scripts/setup_dls_env.sh >/dev/null 2>&1"
env -i HOME=$HOME USER=$USER PATH=/usr/local/bin:/usr/bin:/bin PYTHONNOUSERSITE=1 GST_REGISTRY=$G/app/gst-registry.bin \
  SENTINEL_DELIVERY_AGENT_SOCKET=$R/agent.sock SENTINEL_CV_HANDOFF_STATUS_PATH=$R/handoff_status.json \
  APEXFABRIC_STATE_ROOT=$R/state SNAPSHOT_ROOT=$R/state/snapshots PROFILE_EVENTS=${PROFILE_EVENTS:-} WARMUP_S=${WARMUP_S:-120} CPU_OPT=${CPU_OPT:-} CPU_OPT_THREADS=${CPU_OPT_THREADS:-1} SCENE_DEVICE=${SCENE_DEVICE:-NPU} BODY_DEVICE=${BODY_DEVICE:-NPU} GAIT_DEVICE=${GAIT_DEVICE:-NPU} HEAD_POSE_DEVICE=${HEAD_POSE_DEVICE:-NPU} EVENTS_IDLE=${EVENTS_IDLE:-} NO_PIX=${NO_PIX:-} NO_TENSORS=${NO_TENSORS:-} PIX_VIA_MAP=${PIX_VIA_MAP:-} T_GAIT_SCALE=${T_GAIT_SCALE:-1} V8_FORMAT_ONLY=${V8_FORMAT_ONLY:-} CFACES=${CFACES:-} POLICY_BODY_REFRESH_S=${POLICY_BODY_REFRESH_S:-} POLICY_SECONDARY_RATE=${POLICY_SECONDARY_RATE:-} POLICY_OURS=${POLICY_OURS:-} HANDOFF_PROCESS=${HANDOFF_PROCESS:-} V8_NO_HANDOFF=${V8_NO_HANDOFF:-} NO_SECONDARY=${NO_SECONDARY:-} HANDOFF_QUEUE=${HANDOFF_QUEUE:-} QUEUE_MON=${QUEUE_MON:-} RULES_CPUS=${RULES_CPUS:-} FIRE_DEVICE=${FIRE_DEVICE:-} SCRFD_DEVICE=${SCRFD_DEVICE:-} SEC_PROCESS=${SEC_PROCESS:-} PIX_ON_DEMAND=${PIX_ON_DEMAND:-} PIX_JPEG=${PIX_JPEG:-} PIX_JPEG_OD=${PIX_JPEG_OD:-} NO_IDENTITY=${NO_IDENTITY:-} PIX_HELD=${PIX_HELD:-} PIX_LAZY=${PIX_LAZY:-} JPEG_GPU_RATE=${JPEG_GPU_RATE:-} EXPORT_EVERY=${EXPORT_EVERY:-} VEH_MODEL=${VEH_MODEL:-} REGION_DECODE=${REGION_DECODE:-} PKG_SHM=${PKG_SHM:-} \
  bash -c "$SETUP; cd $I; timeout -k 20 $((DUR + 120)) /usr/bin/python3 -s ${SCRIPT:-integrate2.py} $I/$DESIRED $DUR" > $R/integrate.log 2>&1 &
PIPE=$!
./cpu_sample.sh ${SCRIPT:-integrate2.py} $((${WARMUP_S:-120} + 10)) $((DUR - 15)) $R/cpu.txt &
echo "SCRIPT=${SCRIPT:-integrate2.py} CPU_OPT=${CPU_OPT:-none} CPU_OPT_THREADS=${CPU_OPT_THREADS:-1} SCENE=${SCENE_DEVICE:-NPU} BODY=${BODY_DEVICE:-NPU} GAIT=${GAIT_DEVICE:-NPU} HEADPOSE=${HEAD_POSE_DEVICE:-NPU} EVENTS_IDLE=${EVENTS_IDLE:-0} NO_PIX=${NO_PIX:-0} NO_TENSORS=${NO_TENSORS:-0} PIX_VIA_MAP=${PIX_VIA_MAP:-0} T_GAIT_SCALE=${T_GAIT_SCALE:-1} V8_FORMAT_ONLY=${V8_FORMAT_ONLY:-0} CFACES=${CFACES:-0} BODY_REFRESH=${POLICY_BODY_REFRESH_S:-1} SECONDARY_RATE=${POLICY_SECONDARY_RATE:-0} POLICY_OURS=${POLICY_OURS:-0} HANDOFF_PROCESS=${HANDOFF_PROCESS:-0} V8_NO_HANDOFF=${V8_NO_HANDOFF:-0} MOCK_NO_SAVE=${MOCK_NO_SAVE:-0} NO_SECONDARY=${NO_SECONDARY:-0} HANDOFF_QUEUE=${HANDOFF_QUEUE:-0} PIX_VIA_MAP=${PIX_VIA_MAP:-0} DESIRED=${DESIRED:-desired_state.json}" > $R/variant.txt
echo "$(date +%T) started, results in $R"
if [ "${DROP_AT:-240}" -gt 0 ]; then  # DROP_AT=0: no feed drop (pure throughput/latency run)
  sleep ${DROP_AT:-240}; kill ${FEEDS[1]} 2>/dev/null; echo ">>> $(date +%T) cam01 feed stopped" | tee -a $R/events.txt
  sleep ${DROP_FOR:-60}; feed 1; echo ">>> $(date +%T) cam01 feed back" | tee -a $R/events.txt
fi
wait $PIPE; echo "$(date +%T) integrate exit $?" | tee -a $R/events.txt
curl -s --unix-socket $R/agent.sock http://agent/v1/status > $R/agent_status.json; echo
echo "== pipeline reports (last 6):"; grep -a " total " $R/integrate.log | tail -6 | cut -c1-330
echo "== camera events:"; grep -aE "removed|rebuilt|camera_status|event code error|FATAL|Traceback|Error" $R/integrate.log | head -20 | cut -c1-220
echo "== agent: $(cat $R/agent_status.json)"; grep -a " 4[0-9][0-9] \| 5[0-9][0-9] " $R/agent.log | head -5
echo "== variant: $(cat $R/variant.txt)"; echo "== CPU (60 s .. end):"; cat $R/cpu.txt; grep -a "CPU_USAGE" $R/integrate.log | cut -c1-160
echo "== steady state (after warm-up):"; grep -a "STEADY " $R/integrate.log | cut -c1-260
E=$(grep -a "STEADY window starts" $R/integrate.log | sed 's/.*epoch //'); echo "== frame arrival at reader -> package saved, after warm-up:"; [ -n "$E" ] && python3 $I/latency.py $R $E
echo "== contract check:"; python3 $I/check_packages.py $R
echo INTEGRATE DONE
