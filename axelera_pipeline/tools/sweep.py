#!/usr/bin/env python3
"""INT8 vs FP16 details: score shift, what unmatched FP16 boxes became, and counts at higher Metis thresholds."""
import json
import sys
from collections import Counter

NAMES = "bus car minivan motorcycle rickshaw truck bicycle person others".split()
ref_path, test_path = sys.argv[1:3]
offset = int(sys.argv[3]) if len(sys.argv) > 3 else 0


def load(path, threshold):
    with open(path) as f:
        return [[b for b in json.loads(line)["boxes"] if b[4] >= threshold] for line in f]


def iou(a, b):
    w = max(0, min(a[2], b[2]) - max(a[0], b[0]))
    h = max(0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = w * h
    return inter / ((a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter + 1e-9)


ref = load(ref_path, 0.30)
test = load(test_path, 0.30)
pairs = [(ref[i], test[i + offset]) for i in range(len(ref)) if 0 <= i + offset < len(test)]
shift, became = {}, Counter()
for r, t in pairs:
    for a in r:
        same = [b for b in t if int(b[5]) == int(a[5]) and iou(a, b) >= 0.5]
        if same:
            shift.setdefault(int(a[5]), []).append(max(same, key=lambda b: iou(a, b))[4] - a[4])
            continue
        other = [b for b in t if iou(a, b) >= 0.5]
        to = NAMES[int(max(other, key=lambda b: iou(a, b))[5])] if other else "nothing"
        became[f"{NAMES[int(a[5])]}->{to}"] += 1
print("mean score change on matched boxes (Metis - FP16):")
for c, v in sorted(shift.items()):
    print(f"   {NAMES[c]:10s} {sum(v) / len(v):+.3f}  ({len(v)} boxes)")
print("FP16 boxes with no match became:", dict(became.most_common(8)))
ref_count = Counter(int(b[5]) for r, _ in pairs for b in r)
print("Metis count at higher thresholds, as % change against FP16 at 0.30:")
for thr in (0.30, 0.35, 0.40, 0.45, 0.50):
    count = Counter(int(b[5]) for _, t in pairs for b in t if b[4] >= thr)
    print(f"   {thr:.2f} " + "  ".join(f"{NAMES[c]} {100 * (count[c] - ref_count[c]) / ref_count[c]:+.0f}%" for c in sorted(ref_count)))
