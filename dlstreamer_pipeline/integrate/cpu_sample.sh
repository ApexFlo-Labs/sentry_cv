#!/bin/bash
# cpu_sample.sh PIPE_SCRIPT T1 T2 OUT: between T1 and T2 s from now, CPU of the integrate process and its event
# processes (/proc utime+stime), their memory (RSS at T2), whole-box CPU, NPU busy %, GPU media-engine busy %
# (gt1, 1 - rc6 idle) and GPU render clock (gt0, sampled every 5 s). Read-only.
# PIPE_SCRIPT=none: box/GPU/NPU only (idle reference).
S=$1; T1=$2; T2=$3; OUT=$4; HZ=$(getconf CLK_TCK); NCPU=$(nproc)
NPU=/sys/class/accel/accel0/device/npu_busy_time_us; GT=/sys/class/drm/card1/gt
snap() {  # "pid ticks threads rss_mb kind"
  [ "$S" = none ] && return
  local main=""; for q in $(pgrep -u "$USER" -f "python3 -s $S"); do [ "$(cat /proc/$q/comm 2>/dev/null)" = python3 ] && main=$q; done
  [ -z "$main" ] && return
  for p in $main $(pgrep -P "$main"); do
    [ -r /proc/$p/stat ] || continue
    read -r -a f < <(sed 's/^.*) //' /proc/$p/stat)
    rss=$(awk '/VmRSS/{printf "%d", $2/1024}' /proc/$p/status)
    n=helper; tr '\0' ' ' < /proc/$p/cmdline | grep -q spawn_main && n=event; [ "$p" = "$main" ] && n=engine
    echo "$p $((f[11] + f[12])) ${f[17]} $rss $n"
  done
}
box() { awk '/^cpu /{print $2+$3+$4+$7+$8, $2+$3+$4+$5+$6+$7+$8}' /proc/stat; }
sleep "$T1"; A=$(snap); B1=$(box); N1=$(cat $NPU); R1=$(cat $GT/gt1/rc6_residency_ms); t1=$(date +%s.%N)
FREQS=""; end=$(( $(date +%s) + T2 - T1 ))
while [ "$(date +%s)" -lt "$end" ]; do FREQS="$FREQS $(cat $GT/gt0/rps_act_freq_mhz)"; sleep 5; done
B=$(snap); B2=$(box); N2=$(cat $NPU); R2=$(cat $GT/gt1/rc6_residency_ms); t2=$(date +%s.%N)
W=$(awk -v a=$t1 -v b=$t2 'BEGIN{print b-a}')
{
  echo "window $W s"
  if [ -n "$A" ]; then
    join <(echo "$A" | sort) <(echo "$B" | sort) | awk -v hz=$HZ -v w=$W \
      '{c=($6-$2)/hz/w; printf "%-6s pid %-8s cores %.3f threads %s rss %s MB\n", $5, $1, c, $7, $8;
        if ($5=="event") {ev+=c; ne++; m+=$8} else if ($5=="engine") {en+=c; em=$8} else he+=c}
       END {printf "SUM event processes %d: %.2f cores (%.3f per camera), %.0f MB (%.0f per camera) | engine %.2f cores %d MB\n", ne, ev, ev/(ne?ne:1), m, m/(ne?ne:1), en, em}'
  fi
  read -r b1u b1t <<< "$B1"; read -r b2u b2t <<< "$B2"
  awk -v u=$((b2u - b1u)) -v t=$((b2t - b1t)) -v n=$NCPU 'BEGIN {printf "BOX CPU busy %.1f%% of %d CPUs (%.2f cores)\n", 100*u/t, n, n*u/t}'
  awk -v a=$N1 -v b=$N2 -v w=$W 'BEGIN {printf "NPU busy %.1f%%\n", (b-a)/1e4/w}'
  awk -v a=$R1 -v b=$R2 -v w=$W 'BEGIN {printf "GPU media engine (gt1) busy %.1f%%\n", 100 - (b-a)/10/w}'
  echo "$FREQS" | awk '{for(i=1;i<=NF;i++){s+=$i; if($i>mx)mx=$i}; printf "GPU render clock (gt0) avg %.0f MHz max %d MHz (%d samples)\n", s/NF, mx, NF}'
} > "$OUT"
