#!/bin/bash
# OUR full pipeline (Option 6 + secondary plates) + V8 input/rules/output; body/gait/scene for her rules from one shared model server.
# Usage: CAMS=20 DUR=420 WARMUP_S=120 ./run_full.sh   (desired state with ANPR enabled)
cd ~/chandana_optimization/integrate
if pgrep -f "[i]ntegrate[0-9]*.py" >/dev/null || ss -ltn | grep -q ":8655 "; then echo "busy: another run is active"; exit 1; fi
CAMS=${CAMS:-20}; START=$(date "+%Y-%m-%d %H:%M:%S")
MOCK_SAVE_EVERY=${MOCK_SAVE_EVERY:-20} CAMS=$CAMS DESIRED=desired_state_${CAMS}_anpr.json SCRIPT=${SCRIPT:-integrate9.py} CPU_OPT=threads CPU_OPT_THREADS=2 DROP_AT=${DROP_AT:-0} WARMUP_S=${WARMUP_S:-120} ./run_integrate.sh ${DUR:-420} > /tmp/full.txt 2>&1
R=$(readlink -f run/latest); cp /tmp/full.txt $R/summary.txt
echo "######## FULL $CAMS cams ${LABEL:-} ($R)"
head -3 /tmp/full.txt | grep -a "busy\|already"
grep -a "STEADY [0-9]* s\|STEADY latency_ms" $R/integrate.log | sed "s/^.*STEADY/STEADY/" | cut -c1-200
grep -a "^SUM\|^BOX\|^NPU\|^GPU media" $R/cpu.txt 2>/dev/null
grep -a "FATAL\|Traceback\|event code error\|POLICY\|Option 7 face path\|shared model server ready\|model server: served\|model server stopped" $R/integrate.log | tail -6 | cut -c1-220
grep -a "^secondary:" $R/integrate.log | tail -2
grep -a " total " $R/integrate.log | tail -1 | grep -o "handoff {[^}]*}"
python3 - $R <<'PY'
import re,sys
t=open(sys.argv[1]+"/integrate.log",errors="replace").read()
print("plates stats (last report):", re.findall(r"plates_\w+ [0-9.]+", t)[-3:] if re.findall(r"plates_\w+ [0-9.]+", t) else "not in report")
PY
grep -a "^== agent" $R/summary.txt | cut -c1-900; grep -a "schema/contract errors\|^evidence types\|^   " $R/summary.txt | head -12
grep -a -A18 "package saved" $R/summary.txt | head -20
echo "GPU kernel warnings: $(journalctl -k --since "$START" --no-pager 2>/dev/null | grep -c "GPU HANG\|Fence expiration")"
pkill -u $USER -f "[c]pu_sample.sh integrate8.py" 2>/dev/null
echo FULL DONE
