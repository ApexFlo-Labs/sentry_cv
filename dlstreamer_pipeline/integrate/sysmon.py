#!/usr/bin/env python3
"""Root-free system monitor for one test run (measurement only, changes nothing).

Usage: sysmon.py ENGINE_SCRIPT START_DELAY_S DURATION_S OUT.txt
Samples every 2 s between START_DELAY and START_DELAY+DURATION:
  - per-core busy % and clock (P cores 0-5, E cores 6-13, LP-E 14-15)
  - PSI: /proc/pressure/cpu, memory, io ("some" = % of time at least one task was stalled waiting)
  - package temperature
  - GPU busy per engine class for the engine process (DRM fdinfo drm-engine-*), plus gt0/gt1 frequency
  - engine process threads: CPU % per thread-name group, and which core type they ran on
  - CPU % of the engine's child processes (rules processes, model server, handoff) and of all other processes
"""
import collections
import glob
import os
import re
import sys
import time

script, delay, dur, out = sys.argv[1], float(sys.argv[2]), float(sys.argv[3]), sys.argv[4]
HZ = os.sysconf("SC_CLK_TCK")
CORE = lambda c: "P" if c <= 5 else "E" if c <= 13 else "LP"


def engine_pid():
    for p in glob.glob("/proc/[0-9]*/cmdline"):
        try:
            a = open(p, "rb").read().split(b"\0")
        except OSError:
            continue
        pid = int(p.split("/")[2])
        if pid == os.getpid() or any(b"sysmon.py" in x for x in a):
            continue
        if len(a) > 2 and a[0].endswith(b"python3") and any(x.endswith(script.encode()) for x in a[1:3]):
            return pid
    return None


def cpu_times():
    t = {}
    for line in open("/proc/stat"):
        if line.startswith("cpu") and line[3].isdigit():
            f = line.split()
            v = list(map(int, f[1:]))
            t[int(f[0][3:])] = (sum(v), v[3] + v[4])
    return t


def psi(name):
    try:
        line = open(f"/proc/pressure/{name}").readline()
        return int(re.search(r"total=(\d+)", line).group(1))
    except OSError:
        return 0


def freq():
    return {c: int(open(f"/sys/devices/system/cpu/cpu{c}/cpufreq/scaling_cur_freq").read()) for c in range(16)}


def temp():
    for z in glob.glob("/sys/class/thermal/thermal_zone*"):
        if open(z + "/type").read().strip() == "x86_pkg_temp":
            return int(open(z + "/temp").read()) / 1000
    return 0


def gpu_engines(pid):
    """sum over the process's DRM fds of drm-engine-<class>: <ns> (one client per fd, de-duplicated by client id)."""
    tot, seen = collections.Counter(), set()
    for fd in glob.glob(f"/proc/{pid}/fdinfo/*"):
        try:
            txt = open(fd).read()
        except OSError:
            continue
        if "drm-driver" not in txt:
            continue
        cid = re.search(r"drm-client-id:\s*(\d+)", txt)
        if cid and cid.group(1) in seen:
            continue
        if cid:
            seen.add(cid.group(1))
        for k, v in re.findall(r"drm-engine-([\w-]+):\s*(\d+)\s*ns", txt):
            tot[k] += int(v)
    return tot


def threads(pid):
    r = {}
    for t in glob.glob(f"/proc/{pid}/task/*"):
        try:
            s = open(t + "/stat").read()
        except OSError:
            continue
        name = s[s.index("(") + 1:s.rindex(")")]
        f = s[s.rindex(")") + 2:].split()
        r[int(t.split("/")[-1])] = (re.sub(r"\d+$", "#", name), int(f[11]) + int(f[12]), int(f[36]))
    return r


def procs():
    r = {}
    for p in glob.glob("/proc/[0-9]*/stat"):
        try:
            s = open(p).read()
        except OSError:
            continue
        f = s[s.rindex(")") + 2:].split()
        r[int(p.split("/")[2])] = (s[s.index("(") + 1:s.rindex(")")], int(f[1]), int(f[11]) + int(f[12]))
    return r


def gt():
    v = {}
    for g in ("gt0", "gt1"):
        try:
            v[g] = int(open(f"/sys/class/drm/card1/gt/{g}/rps_act_freq_mhz").read())
        except OSError:
            pass
    return v


time.sleep(delay)
pid = engine_pid()
if pid is None:
    open(out, "w").write(f"engine {script} not found\n")
    sys.exit(0)
end = time.time() + dur
S = collections.defaultdict(list)
th_cpu, th_core = collections.Counter(), collections.defaultdict(collections.Counter)
proc_cpu = collections.Counter()
c0, p0 = cpu_times(), {k: psi(k) for k in ("cpu", "memory", "io")}
g0, t0, pr0, w0 = gpu_engines(pid), threads(pid), procs(), time.time()
first_w = w0
first = dict(c=c0, p=p0, g=g0, w=w0)
while time.time() < end:
    time.sleep(2)
    c1, w1 = cpu_times(), time.time()
    busy = {c: 100.0 * (1 - (c1[c][1] - c0[c][1]) / max(1, c1[c][0] - c0[c][0])) for c in c1}
    for kind in ("P", "E", "LP"):
        S["busy_" + kind].append(sum(busy[c] for c in busy if CORE(c) == kind) / sum(1 for c in busy if CORE(c) == kind))
    f = freq()
    for kind in ("P", "E", "LP"):
        S["mhz_" + kind].append(sum(f[c] for c in f if CORE(c) == kind) / sum(1 for c in f if CORE(c) == kind) / 1000)
    S["temp"].append(temp())
    for g, v in gt().items():
        S[g + "_mhz"].append(v)
    t1 = threads(pid)
    for tid, (name, cpu, core) in t1.items():
        d = cpu - t0.get(tid, (name, cpu, core))[1]
        th_cpu[name] += d
        th_core[name][CORE(core)] += d
    t0 = t1
    pr1 = procs()
    for p, (name, ppid, cpu) in pr1.items():
        d = cpu - pr0.get(p, (name, ppid, cpu))[2]
        if p == pid:
            key = "engine"
        elif ppid == pid:
            key = "engine children (rules/model server/handoff)"
        else:
            key = name
        proc_cpu[key] += d
    pr0, c0 = pr1, c1
span = time.time() - first_w
p1 = {k: psi(k) for k in ("cpu", "memory", "io")}
g1 = gpu_engines(pid)
kids = [p for p, (n, pp, c) in procs().items() if pp == pid]
gk = collections.Counter()
for k in kids:
    gk.update(gpu_engines(k))
avg = lambda k: sum(S[k]) / max(1, len(S[k]))
L = [f"window {span:.0f} s, engine pid {pid} ({script})",
     f"CPU busy %: P-cores {avg('busy_P'):.1f}  E-cores {avg('busy_E'):.1f}  LP-cores {avg('busy_LP'):.1f}",
     f"CPU clock MHz: P {avg('mhz_P'):.0f}  E {avg('mhz_E'):.0f}  LP {avg('mhz_LP'):.0f}   pkg temp avg {avg('temp'):.1f} C max {max(S['temp'] or [0]):.0f} C",
     "PSI (% of time some task stalled): " + "  ".join(f"{k} {100 * (p1[k] - first['p'][k]) / 1e6 / span:.1f}%" for k in p1),
     "GPU busy % (engine process, per engine class): " + "  ".join(f"{k} {100 * (g1[k] - first['g'].get(k, 0)) / 1e9 / span:.1f}" for k in sorted(g1)),
     "GPU busy % used by the engine's CHILD processes (rules/model server/handoff), total since they started: "
     + ("  ".join(f"{k} {v / 1e9:.1f} s" for k, v in sorted(gk.items())) or "none (no GPU use)"),
     "GPU clock MHz: " + "  ".join(f"{g} {avg(g + '_mhz'):.0f}" for g in ("gt0", "gt1") if S[g + "_mhz"]),
     "processes (cores):"]
for k, v in proc_cpu.most_common(8):
    L.append(f"   {v / HZ / span:6.2f}  {k}")
L.append("engine threads (cores, and share on P/E/LP):")
for k, v in th_cpu.most_common(14):
    tot = max(1, sum(th_core[k].values()))
    L.append(f"   {v / HZ / span:6.2f}  {k:28s} P {100 * th_core[k]['P'] / tot:3.0f}%  E {100 * th_core[k]['E'] / tot:3.0f}%  LP {100 * th_core[k]['LP'] / tot:3.0f}%")
open(out, "w").write("\n".join(L) + "\n")
