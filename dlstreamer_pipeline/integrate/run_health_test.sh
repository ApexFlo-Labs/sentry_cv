#!/bin/bash
# Contract health test: /readyz /healthz /metrics under runtime_supervisor.py, 4 cameras, a camera drop and an engine stall.
cd ~/chandana_optimization/integrate || exit 1
pgrep -u admin1 -f "[i]ntegrate9.py|[r]untime_supervisor" && { echo busy; exit 1; }
cp desired_state_4_anpr.json desired_state_rt4.json
export HANDOFF_PROCESS=1 HANDOFF_QUEUE=64 PIX_VIA_MAP=1 CFACES=1 POLICY_OURS=1 \
       PIX_JPEG=1 PIX_JPEG_OD=1 PIX_ON_DEMAND=1 PIX_LAZY=1 REGION_DECODE=1 PKG_SHM=1 VEH_MODEL=$PWD/models/vehicle_qaft_int8_b1.xml
export APEX_API_HOST=127.0.0.1 APEX_API_PORT=8656 WARMUP_S=60
T0=$(date +%s)
DROP_AT=150 DROP_FOR=60 MOCK_SAVE_EVERY=20 CAMS=4 DESIRED=desired_state_rt4.json SCRIPT=runtime_supervisor.py CPU_OPT=threads CPU_OPT_THREADS=2 ./run_integrate.sh 330 > /tmp/health_run.txt 2>&1 &
sleep 8; R=$(readlink -f run/latest)
python3 health_driver.py 8656 $T0 150 60 > $R/health_driver.txt 2>&1
wait
cat $R/health_driver.txt
grep -a "supervisor:\|cam01: removed\|cam01: stream back" $R/integrate.log | cut -c1-150 | head -12
grep -a "schema/contract errors" /tmp/health_run.txt | head -1
grep -a "Traceback" -A3 $R/integrate.log | head -8
for p in $(ps -u admin1 -o pid=,ppid= | awk '$2==1{print $1}'); do a=$(tr "\0" " " < /proc/$p/cmdline | cut -c1-60); case "$a" in *python3*|*cpu_sample*) echo "ORPHAN $p $(readlink /proc/$p/cwd) $a";; esac; done; echo "orphan check done"
