"""Compare detector output from two devices over the same video, frame by frame.

    compare_det.py GPU.jsonl NPU.jsonl NAME

Frames are matched by timestamp. Boxes are matched greedily by IoU (>= 0.5, same label). A box
found by only one device is "borderline" when its confidence is under 0.40, i.e. it sat close to
the 0.30 threshold and a small numeric difference between the chips decided it.
"""
import json
import sys


def load(path):
    frames = {}
    for line in open(path):
        line = line.strip()
        if not line:
            continue
        record = json.loads(line)
        boxes = []
        for obj in record.get("objects", []):
            det = obj.get("detection", {})
            boxes.append((det.get("label", obj.get("label", "?")), float(det.get("confidence", 0.0)),
                          float(obj["x"]), float(obj["y"]), float(obj["w"]), float(obj["h"])))
        frames[record.get("timestamp", len(frames))] = boxes
    return frames


def iou(a, b):
    ax2, ay2, bx2, by2 = a[2] + a[4], a[3] + a[5], b[2] + b[4], b[3] + b[5]
    w = max(0.0, min(ax2, bx2) - max(a[2], b[2]))
    h = max(0.0, min(ay2, by2) - max(a[3], b[3]))
    inter = w * h
    union = a[4] * a[5] + b[4] * b[5] - inter
    return inter / union if union > 0 else 0.0


gpu, npu, name = load(sys.argv[1]), load(sys.argv[2]), sys.argv[3]
common = sorted(set(gpu) & set(npu))
frames_diff = matched = only_gpu = only_npu = borderline = 0
ious, people = [], [0, 0]
for ts in common:
    a, b = gpu[ts], npu[ts]
    people[0] += sum(box[0] == "person" for box in a)
    people[1] += sum(box[0] == "person" for box in b)
    pairs = sorted(((iou(x, y), i, j) for i, x in enumerate(a) for j, y in enumerate(b) if x[0] == y[0]), reverse=True)
    used_a, used_b = set(), set()
    for value, i, j in pairs:
        if value < 0.5 or i in used_a or j in used_b:
            continue
        used_a.add(i); used_b.add(j); ious.append(value)
    left_a = [a[i] for i in range(len(a)) if i not in used_a]
    left_b = [b[j] for j in range(len(b)) if j not in used_b]
    matched += len(used_a); only_gpu += len(left_a); only_npu += len(left_b)
    borderline += sum(box[1] < 0.40 for box in left_a + left_b)
    frames_diff += bool(left_a or left_b)

total = matched + only_gpu + only_npu
print(f"{name}: frames {len(common)} (GPU {len(gpu)}, NPU {len(npu)}) | frames with any difference "
      f"{frames_diff} ({100 * frames_diff / max(1, len(common)):.1f}%) | boxes matched {matched}, only GPU {only_gpu}, "
      f"only NPU {only_npu} ({100 * (only_gpu + only_npu) / max(1, total):.1f}% of boxes; borderline <0.40 conf: "
      f"{borderline}) | mean IoU {sum(ious) / max(1, len(ious)):.3f} | people GPU {people[0]} NPU {people[1]}")
