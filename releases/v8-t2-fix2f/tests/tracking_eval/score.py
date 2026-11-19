"""Score T2 tracking on MOT17 (8 FPS) with TrackEval:
  raw      = gvatrack zero-term IDs (person boxes, as the runtime gets them)
  recovery = runtime presence IDs: replica of v8_events person recovery (lost after >=2 missing evaluations and
             unobserved_after s; re-join a lost presence if body cosine >= 0.78, centre within the distance limit,
             not past ended_after, top-2 margin >= 0.04; body vector only if box >= min size and quality >= floor;
             a new track is only matched on its FIRST frame, exactly as the runtime does)
  recovery_anyframe = same rules, but a new track may be matched on any of its frames (what-if: fixes the first-frame gap)
Modes: balanced and tolerant_collection policy values (adaptive_policy.py)."""
import json, glob, sys, math, csv, configparser, os, collections
import numpy as np
IT = '/home/admin1/Documents/IMAGE_TESTS'
sys.path.insert(0, IT + '/vendor/TrackEval'); sys.path.insert(0, IT)
from compare_sentry_trackers import write_gt
MODES = {'balanced': dict(unobs=5.0, ended=18.0, minw=32, minh=64, qfloor=0.20, hits=3),
         'tolerant_collection': dict(unobs=5.0, ended=20.0, minw=28, minh=56, qfloor=0.20, hits=4)}
OUT = 'scored'; os.makedirs(OUT, exist_ok=True)

def timeouts(m, box, W, H):
    bw, bh = max(1, box[2] - box[0]), max(1, box[3] - box[1]); nw, nh = bw / W, bh / H
    if max(nw, nh) < .12 or min(nw, nh) < .05: return min(10., m['unobs'] * 2), min(22., m['ended'] * 1.1)
    return m['unobs'], m['ended']
def dist_limit(old, new, W, H):
    fs = max(160., .17 * math.hypot(W, H))
    sz = 2.5 * max(old[2] - old[0], old[3] - old[1], new[2] - new[0], new[3] - new[1])
    return min(520., max(120., fs, sz, 320.))
def centre(b): return ((b[0] + b[2]) / 2, (b[1] + b[3]) / 2)

def presence_ids(d, m, anyframe):
    W, H, fps = d['W'], d['H'], d['fps']; pres = {}; tracks = {}; nid = 0; out = []; stats = collections.Counter()
    for fr in d['frames']:
        now = fr['k'] / fps; active = set(); rows = []
        for j, (lab, x, y, w, h, tid, conf) in enumerate(fr['boxes']):
            if lab != 'person' or not tid: continue
            box = (x, y, x + w, y + h); active.add(tid)
            bv = fr['body'].get(str(j)) or fr['body'].get(j)
            vec = None
            if bv and bv['wh'][0] >= m['minw'] and bv['wh'][1] >= m['minh'] and bv['q'] >= m['qfloor']:
                vec = np.asarray(bv['v'], np.float32); vec /= max(np.linalg.norm(vec), 1e-6)
            st = tracks.get(tid)
            first = st is None
            if first or (anyframe and st.get('fresh') and st['hits'] < 8):
                if vec is not None:
                    cands = []
                    for p in pres.values():
                        if not p['lost'] or p['tid'] in active or p['bv'] is None: continue
                        if now - p['last'] > timeouts(m, p['box'], W, H)[1]: continue
                        if np.linalg.norm(np.subtract(centre(box), centre(p['box']))) > dist_limit(p['box'], box, W, H): continue
                        sim = float(vec @ p['bv'])
                        if sim >= .78: cands.append((sim, p))
                    cands.sort(key=lambda c: -c[0])
                    if cands and not (len(cands) > 1 and cands[0][0] - cands[1][0] < .04):
                        p = cands[0][1]
                        if not first:  # what-if: merge the fresh presence into the old one
                            old = st; pres.pop(old['id'], None); stats['late_rejoin'] += 1
                        tracks.pop(p['tid'], None); p['tid'] = tid; tracks[tid] = p; p['lost'] = False; st = p
                        stats['rejoined'] += 1; first = False
                    elif first: stats['new_no_match'] += 1
                elif first: stats['new_no_vector'] += 1
                if st is None or st.get('id') is None or first:
                    nid += 1; st = dict(id=nid, tid=tid, lost=False, bv=None, box=box, last=now, hits=0, fresh=True, miss=0, miss_since=None)
                    pres[nid] = st; tracks[tid] = st
            st['hits'] += 1; st['last'] = now; st['box'] = box; st['lost'] = False; st['miss'] = 0; st['miss_since'] = None
            if st['hits'] >= 8: st['fresh'] = False
            if vec is not None: st['bv'] = vec
            rows.append((st['id'], box, conf))
        # lost / ended (v8_events._lost_and_unknown, confirmed presences)
        for tid, st in list(tracks.items()):
            if tid in active: continue
            if st['hits'] < m['hits']:
                if now - st['last'] >= 8: tracks.pop(tid, None); pres.pop(st['id'], None)
                continue
            if st['miss_since'] is None: st['miss_since'] = now; st['miss'] = 1; continue
            st['miss'] += 1; un, en = timeouts(m, st['box'], W, H); gap = now - st['miss_since']
            if st['miss'] >= 2 and gap >= un and not st['lost']: st['lost'] = True; stats['lost'] += 1
            if st['miss'] >= 2 and gap >= en: tracks.pop(tid, None); pres.pop(st['id'], None); stats['ended'] += 1
        out.append((fr['k'] + 1, rows))
    return out, stats

def write_mot(path, frames):
    with open(path, 'w') as f:
        for k, rows in frames:
            for pid, (x1, y1, x2, y2), conf in rows: f.write(f'{k},{pid},{x1},{y1},{x2 - x1},{y2 - y1},{conf},-1,-1,-1\n')

def main():
    import trackeval
    for old, b in (('float', float), ('int', int)):
        if old not in np.__dict__: setattr(np, old, b)
    seqs = sorted(glob.glob('out/MOT17-*.json')); seq_info = {}; allstats = {}
    names = ['raw', 'recovery_balanced', 'recovery_tolerant', 'recovery_anyframe_tolerant']
    for f in seqs:
        d = json.load(open(f)); name = d['seq']
        write_gt(name, d['selected'], __import__('pathlib').Path(OUT) / 'gt', d['fps']); seq_info[name] = len(d['selected'])
        raw = [(fr['k'] + 1, [(b[5], (b[1], b[2], b[1] + b[3], b[2] + b[4]), b[6]) for b in fr['boxes'] if b[0] == 'person' and b[5]]) for fr in d['frames']]
        variants = {'raw': (raw, {}),
                    'recovery_balanced': presence_ids(d, MODES['balanced'], False),
                    'recovery_tolerant': presence_ids(d, MODES['tolerant_collection'], False),
                    'recovery_anyframe_tolerant': presence_ids(d, MODES['tolerant_collection'], True)}
        for v, (frames, stt) in variants.items():
            os.makedirs(f'{OUT}/trackers/{v}/data', exist_ok=True); write_mot(f'{OUT}/trackers/{v}/data/{name}.txt', frames)
            allstats.setdefault(v, collections.Counter()).update(stt)
    ds = trackeval.datasets.MotChallenge2DBox({'GT_FOLDER': OUT + '/gt', 'TRACKERS_FOLDER': OUT + '/trackers', 'TRACKERS_TO_EVAL': names,
                                               'SKIP_SPLIT_FOL': True, 'SEQ_INFO': seq_info, 'PRINT_CONFIG': False})
    ev = trackeval.Evaluator({'PRINT_CONFIG': False, 'PLOT_CURVES': False, 'DISPLAY_LESS_PROGRESS': True, 'TIME_PROGRESS': False,
                              'OUTPUT_SUMMARY': False, 'OUTPUT_DETAILED': False, 'USE_PARALLEL': False, 'PRINT_RESULTS': False})
    res, msg = ev.evaluate([ds], [trackeval.metrics.HOTA(), trackeval.metrics.CLEAR(), trackeval.metrics.Identity()])
    R = {}
    for v in names:
        r = res[ds.get_name()][v]
        def pick(c): return dict(HOTA=round(float(np.mean(c['HOTA']['HOTA'])) * 100, 2), IDF1=round(c['Identity']['IDF1'] * 100, 2),
                                 MOTA=round(c['CLEAR']['MOTA'] * 100, 2), IDSW=int(c['CLEAR']['IDSW']), Frag=int(c['CLEAR']['Frag']),
                                 GT_IDs=int(c['Count']['GT_IDs']) if 'Count' in c else None, pred_IDs=int(c['Identity']['IDTP'] and 0) or None,
                                 recall=round(c['CLEAR']['CLR_Re'] * 100, 2), precision=round(c['CLEAR']['CLR_Pr'] * 100, 2))
        R[v] = dict(combined=pick(r['COMBINED_SEQ']['pedestrian']), per_seq={s: pick(r[s]['pedestrian']) for s in seq_info},
                    rule_stats=dict(allstats.get(v, {})))
        # count predicted IDs
        R[v]['pred_IDs'] = sum(len({l.split(',')[1] for l in open(f'{OUT}/trackers/{v}/data/{s}.txt')}) for s in seq_info)
    json.dump(R, open('tracking_scores.json', 'w'), indent=1)
    for v in names: print(v, R[v]['combined'], 'pred_IDs', R[v]['pred_IDs'], R[v]['rule_stats'])
main()
