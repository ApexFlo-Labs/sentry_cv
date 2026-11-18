#!/bin/bash
# 20 cameras under runtime_supervisor.py (final configuration) with one hot desired-state change in the middle.
cd ~/chandana_optimization/integrate || exit 1
pgrep -u admin1 -f "[i]ntegrate9.py|[r]untime_supervisor" && { echo busy; exit 1; }
cp desired_state_20_anpr.json desired_state_rt20.json
export HANDOFF_PROCESS=1 HANDOFF_QUEUE=64 PIX_VIA_MAP=1 CFACES=1 POLICY_OURS=1 \
       PIX_JPEG=1 PIX_JPEG_OD=1 PIX_ON_DEMAND=1 PIX_LAZY=1 REGION_DECODE=1 PKG_SHM=1 VEH_MODEL=$PWD/models/vehicle_qaft_int8_b1.xml
export APEX_API_HOST=127.0.0.1 APEX_API_PORT=8656 WARMUP_S=120
DROP_AT=0 MOCK_SAVE_EVERY=20 CAMS=20 DESIRED=desired_state_rt20.json SCRIPT=runtime_supervisor.py CPU_OPT=threads CPU_OPT_THREADS=2 ./run_integrate.sh 450 > /tmp/sup20.txt 2>&1 &
sleep 8; R=$(readlink -f run/latest)
python3 - desired_state_rt20.json 8656 <<'PY' > $R/driver.txt 2>&1
import json, os, sys, time, urllib.request
D, PORT = sys.argv[1], sys.argv[2]
def ready():
    try: r = urllib.request.urlopen(f"http://127.0.0.1:{PORT}/readyz", timeout=3); return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e: return e.code, json.loads(e.read())
    except Exception as e: return None, {}
t = time.time()
while time.time() - t < 300 and ready()[0] != 200: time.sleep(2)
c, p = ready(); print(time.strftime("%T"), "start readyz", c, "rev", p.get("active_revision"))
time.sleep(200)
d = json.load(open(D)); d["revision"] += 1
cam = d["cameras"][0]; cam["geometry"]["zones"].append({"id": "cam00-test", "name": "Test", "poly": [[0, 0], [1, 0], [1, 1], [0, 1]]})
for b in cam["bindings"]:
    if b["app"] == "vehicle_presence": b["geometry_ids"].append("cam00-test")
tmp = D + ".tmp"; json.dump(d, open(tmp, "w")); os.replace(tmp, D)
time.sleep(15); c, p = ready()
print(time.strftime("%T"), "after hot change readyz", c, "rev", p.get("active_revision"),
      "cameras healthy", sum(x["state"] == "healthy" for x in p.get("per_camera_state", [])), "/", len(p.get("per_camera_state", [])))
PY
wait
cat $R/driver.txt
grep -a "supervisor:" $R/integrate.log | cut -c1-160
echo "workers applied hot config: $(grep -ac 'hot config applied, revision' $R/integrate.log)"
grep -a "STEADY [0-9]* s\|STEADY latency_ms frame" $R/integrate.log | cut -c1-230
grep -a "schema/contract errors\|== agent" /tmp/sup20.txt | cut -c1-140 | head -3
grep -a "Traceback\|hot config FAILED" $R/integrate.log | sort | uniq -c | head
for p in $(ps -u admin1 -o pid=,ppid= | awk '$2==1{print $1}'); do a=$(tr "\0" " " < /proc/$p/cmdline | cut -c1-60); case "$a" in *python3*|*cpu_sample*) echo "ORPHAN $p $(readlink /proc/$p/cwd) $a";; esac; done; echo "orphan check done"
