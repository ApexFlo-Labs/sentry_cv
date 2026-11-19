#!/usr/bin/env python3
"""Root sampler on .64 while one test case runs: every 1 s -> GPU engines (intel_gpu_top), NPU busy %, CPU % of each
thread of the busiest process (the Sentinel camera worker) and system CPU.  usage: host_sampler.py OUT_DIR"""
import json, os, sys, time, subprocess, glob, collections
out = sys.argv[1]; os.makedirs(out, exist_ok=True)
HZ = os.sysconf('SC_CLK_TCK')
gpu = subprocess.Popen(['intel_gpu_top', '-J', '-s', '1000', '-o', os.path.join(out, 'gpu_top.json')],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
NPU = glob.glob('/sys/class/accel/accel0/device/npu_busy_time_us')
def npu(): return int(open(NPU[0]).read()) if NPU else 0
def cpu_total():
    v = list(map(int, open('/proc/stat').readline().split()[1:])); return sum(v), v[3] + v[4]
def threads():
    r = {}
    for p in glob.glob('/proc/[0-9]*/task/[0-9]*/stat'):
        try:
            s = open(p).read(); comm = s[s.index('(') + 1:s.rindex(')')]; f = s[s.rindex(')') + 2:].split()
            pid = int(p.split('/')[2]); r[(pid, int(p.split('/')[4]))] = (comm, int(f[11]) + int(f[12]))
        except Exception: pass
    return r
def pcomm(pid):
    try: return open(f'/proc/{pid}/cmdline').read().replace('\0', ' ')[:160]
    except Exception: return ''
f = open(os.path.join(out, 'host_samples.jsonl'), 'w')
t0 = time.time(); n0 = npu(); c0 = cpu_total(); th0 = threads()
try:
    while True:
        time.sleep(1.0)
        t1 = time.time(); n1 = npu(); c1 = cpu_total(); th1 = threads(); dt = t1 - t0
        per = collections.defaultdict(float); rows = []
        for k, (comm, ticks) in th1.items():
            if k in th0:
                d = (ticks - th0[k][1]) / HZ / dt * 100
                if d > 0.5: rows.append((round(d, 1), k[0], k[1], comm)); per[k[0]] += d
        rows.sort(reverse=True)
        top = sorted(per.items(), key=lambda x: -x[1])[:3]
        sysbusy = 100 * (1 - (c1[1] - c0[1]) / max(c1[0] - c0[0], 1))
        f.write(json.dumps(dict(t=round(t1, 2), npu_pct=round((n1 - n0) / 1e4 / dt, 1), sys_cpu_pct=round(sysbusy, 1),
                                top_procs=[dict(pid=p, cpu_pct=round(c, 1), cmd=pcomm(p)) for p, c in top],
                                top_threads=[dict(cpu_pct=c, pid=p, tid=t, name=n) for c, p, t, n in rows[:40]])) + '\n'); f.flush()
        t0, n0, c0, th0 = t1, n1, c1, th1
except KeyboardInterrupt: pass
finally:
    gpu.terminate()
