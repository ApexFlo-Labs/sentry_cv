"""Counting accuracy proxy on MOT17 (8 FPS): a vertical line at the frame centre and a zone = left half.
Ground truth = labelled pedestrians (class 1, visible). Prediction = T2 tracker IDs (raw) and runtime-style presence IDs.
Line crossing: bottom-centre of the box moves from one side to the other; counted once per ID per direction (as a
unique-person line count). Zone entries: unique IDs whose bottom-centre is ever inside the left half.
Unique people: number of distinct IDs seen."""
import json, glob, csv, collections
def gt_tracks(seq, sel):
    m = {f: i for i, f in enumerate(sel)}; T = collections.defaultdict(dict)
    for r in csv.reader(open(f'/home/admin1/Documents/IMAGE_TESTS/datasets/mot17/MOT17/train/{seq}/gt/gt.txt')):
        f, tid, x, y, w, h, flag, cls, vis = int(r[0]), int(r[1]), *map(float, r[2:6]), int(r[6]), int(r[7]), float(r[8])
        if f in m and flag == 1 and cls == 1 and vis >= 0.25: T[tid][m[f]] = (x + w / 2, y + h)
    return T
def pred_tracks(path):
    T = collections.defaultdict(dict)
    for l in open(path):
        k, pid, x, y, w, h = l.split(',')[:6]; T[pid][int(k) - 1] = (float(x) + float(w) / 2, float(y) + float(h))
    return T
def measure(T, W):
    cx = W / 2; cross = collections.Counter(); zone = 0
    for tid, pts in T.items():
        ks = sorted(pts); sides = [pts[k][0] < cx for k in ks]
        dirs = {('L2R' if a else 'R2L') for a, b in zip(sides, sides[1:]) if a != b}
        for d in dirs: cross[d] += 1
        zone += any(sides)
    return dict(unique=len(T), cross_L2R=cross['L2R'], cross_R2L=cross['R2L'], zone_entries=zone)
tot = collections.defaultdict(collections.Counter); per = {}
for f in sorted(glob.glob('out/MOT17-*.json')):
    d = json.load(open(f)); s = d['seq']
    res = {'ground_truth': measure(gt_tracks(s, d['selected']), d['W'])}
    for v in ('raw', 'recovery_tolerant'):
        res[v] = measure(pred_tracks(f'scored/trackers/{v}/data/{s}.txt'), d['W'])
    per[s] = res
    for v, r in res.items(): tot[v].update(r)
json.dump(dict(per_sequence=per, total={k: dict(v) for k, v in tot.items()}), open('count_check.json', 'w'), indent=1)
for v, r in tot.items(): print(f'{v:18s}', dict(r))
for s, r in per.items(): print(s, {v: (x['cross_L2R'] + x['cross_R2L'], x['unique']) for v, x in r.items()})
