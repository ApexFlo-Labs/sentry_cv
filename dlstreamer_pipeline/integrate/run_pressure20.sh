#!/bin/bash
# 20 cameras under the supervisor: /healthz capacity must report the real fps against the requested 10 fps per camera.
cd ~/chandana_optimization/integrate || exit 1
pgrep -u admin1 -f "[i]ntegrate9.py|[r]untime_supervisor" && { echo busy; exit 1; }
cp desired_state_20_anpr.json desired_state_rt20.json
export HANDOFF_PROCESS=1 HANDOFF_QUEUE=64 PIX_VIA_MAP=1 CFACES=1 POLICY_OURS=1 \
       PIX_JPEG=1 PIX_JPEG_OD=1 PIX_ON_DEMAND=1 PIX_LAZY=1 REGION_DECODE=1 PKG_SHM=1 VEH_MODEL=$PWD/models/vehicle_qaft_int8_b1.xml
export APEX_API_HOST=127.0.0.1 APEX_API_PORT=8656 WARMUP_S=120
DROP_AT=0 MOCK_SAVE_EVERY=20 CAMS=20 DESIRED=desired_state_rt20.json SCRIPT=runtime_supervisor.py CPU_OPT=threads CPU_OPT_THREADS=2 ./run_integrate.sh 330 > /tmp/press20.txt 2>&1 &
sleep 8; R=$(readlink -f run/latest)
sleep 240
for i in 1 2 3; do curl -s -m 3 http://127.0.0.1:8656/healthz | python3 -c "import json,sys; h=json.load(sys.stdin); c=h['capacity']; s=h['per_camera_state']; print('capacity', json.dumps(c)); print('states', sorted({x['state'] for x in s}), 'inference fps per camera min/max', min(x.get('inference_fps') or 0 for x in s), max(x.get('inference_fps') or 0 for x in s))"; sleep 20; done
curl -s -m 3 http://127.0.0.1:8656/metrics | grep -E "^sentinel_runtime_|^apexfabric_runtime_"
curl -s -o /dev/null -w "readyz %{http_code}\n" -m 3 http://127.0.0.1:8656/readyz
wait
grep -a "STEADY [0-9]* s" $R/integrate.log | cut -c1-200
grep -a "schema/contract errors" /tmp/press20.txt | head -1
for p in $(ps -u admin1 -o pid=,ppid= | awk '$2==1{print $1}'); do a=$(tr "\0" " " < /proc/$p/cmdline | cut -c1-60); case "$a" in *python3*|*cpu_sample*) echo "ORPHAN $p $(readlink /proc/$p/cwd) $a";; esac; done; echo "orphan check done"
