#!/bin/bash
# Contract desired-state test: runtime_supervisor.py around the final configuration, 4 cameras, desired state changed while running.
cd ~/chandana_optimization/integrate || exit 1
cp desired_state_4_anpr.json desired_state_rt4.json
export HANDOFF_PROCESS=1 HANDOFF_QUEUE=64 PIX_VIA_MAP=1 CFACES=1 POLICY_OURS=1 \
       PIX_JPEG=1 PIX_JPEG_OD=1 PIX_ON_DEMAND=1 PIX_LAZY=1 REGION_DECODE=1 PKG_SHM=1 VEH_MODEL=$PWD/models/vehicle_qaft_int8_b1.xml
export APEX_API_HOST=127.0.0.1 APEX_API_PORT=8656 WARMUP_S=60
DROP_AT=0 MOCK_SAVE_EVERY=1 CAMS=4 DESIRED=desired_state_rt4.json SCRIPT=runtime_supervisor.py CPU_OPT=threads CPU_OPT_THREADS=2 ./run_integrate.sh 1100 > /tmp/rt_run.txt 2>&1 &
sleep 8
R=$(readlink -f run/latest)
python3 rt_driver.py desired_state_rt4.json $R 8656 > $R/driver.txt 2>&1
cat $R/driver.txt
pkill -TERM -f "[r]untime_supervisor.py $PWD/desired_state_rt4.json" ; wait
grep -a "supervisor:" $R/integrate.log | cut -c1-200
grep -a "schema/contract errors" /tmp/rt_run.txt | head -2
grep -a "Traceback" -A3 $R/integrate.log | head -12
