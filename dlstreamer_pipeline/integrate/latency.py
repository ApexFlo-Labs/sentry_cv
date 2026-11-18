#!/usr/bin/env python3
"""latency.py RUN_DIR [AFTER_EPOCH]: (only frames observed after AFTER_EPOCH, i.e. after warm-up) per event type, time from the frame's arrival (observed_at) to the package being saved by the agent."""
import glob, json, os, statistics, sys
from datetime import datetime
by = {}
for m in glob.glob(os.path.join(sys.argv[1], "packages/*/manifest.json")):
    try:
        o = json.load(open(m))["observation"]
    except Exception:
        continue
    t, ev = o.get("observed_at"), o.get("event_type")
    after = float(sys.argv[2]) if len(sys.argv) > 2 else 0.0
    if t and ev and datetime.fromisoformat(t.replace("Z", "+00:00")).timestamp() >= after:
        by.setdefault(ev, []).append(1000 * (os.path.getmtime(m) - datetime.fromisoformat(t.replace("Z", "+00:00")).timestamp()))
print(f"{'event':24s} {'n':>6s} {'median_ms':>10s} {'p95_ms':>9s} {'p99_ms':>9s} {'max_ms':>9s}")
for ev, v in sorted(by.items(), key=lambda x: -len(x[1])):
    v.sort()
    print(f"{ev:24s} {len(v):6d} {statistics.median(v):10.0f} {v[min(len(v) - 1, int(0.95 * len(v)))]:9.0f} {v[min(len(v) - 1, int(0.99 * len(v)))]:9.0f} {v[-1]:9.0f}")
allv = sorted(x for v in by.values() for x in v)
if allv:
    print(f"{'ALL':24s} {len(allv):6d} {statistics.median(allv):10.0f} {allv[int(0.95 * len(allv))]:9.0f} {allv[int(0.99 * len(allv))]:9.0f} {allv[-1]:9.0f}")
