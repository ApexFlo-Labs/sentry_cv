#!/usr/bin/env python3
"""Scene-slot check: per camera, scene_sample times; across cameras, how close the samples fall to each other.
usage: score_scene.py RUN_DIR [NOMINAL_INTERVAL_S]
Phases use the CONFIGURED interval (a measured median smears the phases when the true period is exact)."""
import json, sys, collections, statistics
from datetime import datetime

def t(p): return datetime.fromisoformat(p['observed_at'].replace('Z', '+00:00')).timestamp()

L = [json.loads(l) for l in open(f'{sys.argv[1]}/packages.jsonl')]
S = sorted((t(p), p['camera_id']) for p in L if p['event_type'] == 'scene_sample')
per = collections.defaultdict(list)
for tt, c in S: per[c].append(tt)
gaps = [b[0] - a[0] for a, b in zip(S, S[1:])]
iv = [b - a for c in per for a, b in zip(per[c], per[c][1:])]
print(json.dumps(dict(samples=len(S), cameras=len(per), per_camera={c: len(v) for c, v in sorted(per.items())},
    same_camera_interval_median_s=round(statistics.median(iv), 2) if iv else None,
    next_sample_any_camera_gap_s=dict(median=round(statistics.median(gaps), 2) if gaps else None,
        under_0_25s=sum(g < .25 for g in gaps), under_0_5s=sum(g < .5 for g in gaps), total=len(gaps)))))
# phase of each camera inside the sampling interval (0..1); even slots -> sorted gaps all ~1/n
I = float(sys.argv[2]) if len(sys.argv) > 2 else statistics.median(iv)
ph = {c: statistics.median((x % I) / I for x in v) for c, v in per.items()}
srt = sorted(ph.values()); n = len(srt)
g = [b - a for a, b in zip(srt, srt[1:])] + [1 - srt[-1] + srt[0]]
print(json.dumps(dict(interval_s=round(I, 3), phases={c: round(p, 3) for c, p in sorted(ph.items())},
    phase_gap_ideal=round(1 / n, 3), phase_gap_min=round(min(g), 3), phase_gap_max=round(max(g), 3),
    pairs_within_1pct=sum(x < .01 for x in g))))
