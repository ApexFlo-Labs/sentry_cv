#!/usr/bin/env python3
"""List Metis's discrete score levels near the threshold, and class counts at fine thresholds against FP16 at 0.30."""
import json
import sys
from collections import Counter

NAMES = "bus car minivan motorcycle rickshaw truck bicycle person others".split()
clip = sys.argv[1]


def boxes(path):
    with open(path) as f:
        return [b for line in f for b in json.loads(line)["boxes"]]


ref = boxes(f"check/out/ref_{clip}.jsonl")
test = boxes(f"check/out/clip_{clip}.jsonl")
levels = Counter(round(b[4], 4) for b in test if b[4] < 0.46)
print(f"== {clip}: Metis score levels (score: boxes)")
print("   " + "  ".join(f"{k:.4f}:{v}" for k, v in sorted(levels.items())))
ref_count = Counter(int(b[5]) for b in ref if b[4] >= 0.30)
for thr in (0.30, 0.31, 0.32, 0.33, 0.34, 0.35):
    count = Counter(int(b[5]) for b in test if b[4] >= thr)
    print(f"   {thr:.2f} " + "  ".join(f"{NAMES[c]} {100 * (count[c] - ref_count[c]) / ref_count[c]:+.0f}%" for c in sorted(ref_count)))
trucks = sorted(round(b[4], 2) for b in ref if int(b[5]) == 5 and b[4] >= 0.30)
if trucks:
    print(f"   FP16 trucks: {len(trucks)}, score min {trucks[0]}, median {trucks[len(trucks) // 2]}, max {trucks[-1]}")
