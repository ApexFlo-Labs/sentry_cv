#!/usr/bin/env python3
"""Compare two box dumps frame by frame: same class and IoU>=0.5 counts as the same detection."""
import json
import sys
from collections import Counter

NAMES = "bus car minivan motorcycle rickshaw truck bicycle person others".split()
ref_path, test_path = sys.argv[1:3]
THRESHOLD = 0.30  # the DL Streamer pipeline's threshold


def load(path):
    with open(path) as f:
        return [[b for b in json.loads(line)["boxes"] if b[4] >= THRESHOLD] for line in f]


def iou(a, b):
    w = max(0, min(a[2], b[2]) - max(a[0], b[0]))
    h = max(0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = w * h
    return inter / ((a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter + 1e-9)


def compare(ref, test, offset):
    matched, ious, lone = 0, [], []
    pairs = [(ref[i], test[i + offset]) for i in range(len(ref)) if 0 <= i + offset < len(test)]
    for r, t in pairs:
        used = set()
        for a in sorted(r, key=lambda b: -b[4]):
            best = max(((iou(a, b), j) for j, b in enumerate(t) if j not in used and int(b[5]) == int(a[5])), default=(0, -1))
            if best[0] >= 0.5:
                used.add(best[1])
                matched += 1
                ious.append(best[0])
            else:
                lone.append(a)
        lone += [b for j, b in enumerate(t) if j not in used]
    return matched, ious, lone, pairs


ref, test = load(ref_path), load(test_path)
offset = max(range(-3, 4), key=lambda o: compare(ref, test, o)[0])
matched, ious, lone, pairs = compare(ref, test, offset)
a = sum(len(r) for r, _ in pairs)
b = sum(len(t) for _, t in pairs)
print(f"frames: ref {len(ref)}, test {len(test)}, compared {len(pairs)}, best offset {offset}")
print(f"boxes: ref {a}, test {b}, matched {matched}, unmatched {len(lone)} ({100 * len(lone) / max(1, matched + len(lone)):.1f}%)")
print(f"mean IoU of matches {sum(ious) / max(1, len(ious)):.3f}; unmatched with conf<0.40: {100 * sum(x[4] < 0.40 for x in lone) / max(1, len(lone)):.0f}%")
cr, ct = Counter(int(x[5]) for r, _ in pairs for x in r), Counter(int(x[5]) for _, t in pairs for x in t)
for c in sorted(set(cr) | set(ct)):
    print(f"   {NAMES[c]:10s} ref {cr[c]:6d}  test {ct[c]:6d}  ({100 * (ct[c] - cr[c]) / max(1, cr[c]):+.1f}%)")
