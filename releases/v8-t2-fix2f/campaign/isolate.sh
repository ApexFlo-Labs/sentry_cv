#!/bin/bash
# usage: isolate.sh on|off   (run as root on .64). Records originals in T/ISOLATION_ORIGINAL.json, restores exactly.
T=/home/admin1/Documents/sentry-validation-20261116-t2
U="sudo -u admin1 XDG_RUNTIME_DIR=/run/user/1000 systemctl --user"
SYS="k3s apexfabric-control"; USR="sentry-cv sentry-cv-ui tracker-miner-fs-3"
if [ "$1" = on ]; then
  [ -f $T/ISOLATION_ORIGINAL.json ] || {
    echo "{\"no_turbo\":\"$(cat /sys/devices/system/cpu/intel_pstate/no_turbo)\"," > $T/ISOLATION_ORIGINAL.json
    for s in $SYS; do echo "\"sys:$s\":\"$(systemctl is-active $s)\"," >> $T/ISOLATION_ORIGINAL.json; done
    for s in $USR; do echo "\"usr:$s\":\"$($U is-active $s)\"," >> $T/ISOLATION_ORIGINAL.json; done
    echo "\"utc\":\"$(date -u +%FT%TZ)\"}" >> $T/ISOLATION_ORIGINAL.json; }
  for s in $USR; do $U stop $s; done
  # runtime mask (cleared on reboot) so nothing can restart them during measurement, as Codex did for C18
  # /etc units ignore a /run mask, so use a /run drop-in condition + marker (the method Codex used for C18)
  touch $T/ISOLATION_ACTIVE
  for s in $SYS; do mkdir -p /run/systemd/system/$s.service.d
    printf '[Unit]\nConditionPathExists=!%s/ISOLATION_ACTIVE\n' $T > /run/systemd/system/$s.service.d/99-t1-isolation.conf; done
  systemctl daemon-reload
  for s in $SYS; do systemctl stop $s; done; /usr/local/bin/k3s-killall.sh >/dev/null 2>&1
  echo 1 > /sys/devices/system/cpu/intel_pstate/no_turbo
elif [ "$1" = off ]; then
  O=$T/ISOLATION_ORIGINAL.json
  python3 - "$O" <<'PY'
import json,subprocess,sys
o=json.load(open(sys.argv[1]))
open('/sys/devices/system/cpu/intel_pstate/no_turbo','w').write(o['no_turbo'])
import os
T='/home/admin1/Documents/sentry-validation-20261116-t2'
for k in o:
    if k.startswith('sys:'):
        subprocess.run(['systemctl','unmask','--runtime',k[4:]])
        try: os.remove('/run/systemd/system/%s.service.d/99-t1-isolation.conf'%k[4:])
        except FileNotFoundError: pass
try: os.remove(T+'/ISOLATION_ACTIVE')
except FileNotFoundError: pass
subprocess.run(['systemctl','daemon-reload'])
U=['sudo','-u','admin1','XDG_RUNTIME_DIR=/run/user/1000','systemctl','--user']
for k,v in o.items():
    if k.startswith('sys:') and v=='active': subprocess.run(['systemctl','start',k[4:]])
    if k.startswith('usr:') and v=='active': subprocess.run(U+['start',k[4:]])
PY
fi
echo "turbo_off=$(cat /sys/devices/system/cpu/intel_pstate/no_turbo)"; for s in $SYS; do echo "$s=$(systemctl is-active $s)"; done; for s in $USR; do echo "$s=$($U is-active $s)"; done
