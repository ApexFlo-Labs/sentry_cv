"""GPU engine busy % for one process, from the kernel's per-client counters in /proc/PID/fdinfo.

    gpu_busy.py PID SECONDS

Samples twice, SECONDS apart. Works with the i915 driver (drm-engine-NAME: N ns) and the xe driver
(drm-cycles-NAME / drm-total-cycles-NAME). Busy % is per engine, summed over the process's GPU clients.
"""
import glob
import re
import sys
import time

pid, seconds = int(sys.argv[1]), float(sys.argv[2])


def sample():
    ns, cycles, total = {}, {}, {}
    for path in glob.glob(f"/proc/{pid}/fdinfo/*"):
        try:
            text = open(path).read()
        except OSError:
            continue
        if "drm-driver" not in text:
            continue
        client = re.search(r"drm-client-id:\s*(\d+)", text)
        key = client.group(1) if client else path  # one client can be open on several fds
        for name, value in re.findall(r"drm-engine-([\w-]+):\s*(\d+)\s*ns", text):
            ns[(key, name)] = int(value)
        for name, value in re.findall(r"drm-cycles-(\w+):\s*(\d+)", text):
            cycles[(key, name)] = int(value)
        for name, value in re.findall(r"drm-total-cycles-(\w+):\s*(\d+)", text):
            total[(key, name)] = int(value)
    return ns, cycles, total, time.monotonic_ns()


ns0, cycles0, total0, t0 = sample()
time.sleep(seconds)
ns1, cycles1, total1, t1 = sample()
busy = {}
for key, value in ns1.items():  # i915
    busy[key[1]] = busy.get(key[1], 0.0) + 100.0 * (value - ns0.get(key, value)) / (t1 - t0)
for key, value in cycles1.items():  # xe
    span = total1.get(key, 0) - total0.get(key, 0)
    if span > 0:
        busy[key[1]] = busy.get(key[1], 0.0) + 100.0 * (value - cycles0.get(key, value)) / span
print(" ".join(f"{name} {value:.0f}%" for name, value in sorted(busy.items())) or "no GPU counters found")
