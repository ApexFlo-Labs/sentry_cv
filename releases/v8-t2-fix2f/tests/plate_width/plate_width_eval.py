"""Is the 80 px vehicle-width plate rule right? 71 human-verified plates (IMAGE_TESTS live-anpr-ground-truth).
Each source vehicle image is shrunk to a vehicle width W (as if farther away / lower-res camera), then the exact
sentinelplate chain runs: letterbox 224 (114 pad) -> plate model (NPU) -> first box >= 0.35 -> size/aspect checks
(16x5, 0.8..12) -> crop -> 128x64 INTER_LINEAR RGB 0..255 -> OCR (NPU) -> runtime decode_logits.
Scored: exact text match with the human label; also confidence bands used by the v8 confirmation rule (0.35 / 0.82)."""
import sys, json, glob, os, collections
import numpy as np, cv2, openvino as ov
sys.path.insert(0, '/opt/traffic-pilot')
from services.worker.detectors.backends.openvino_ocr_async import decode_logits
GT = json.load(open('/gt/ground_truth.json'))['samples']
core = ov.Core()
PL = core.compile_model('/models/traffic/openvino/license_plate.xml', 'NPU')
OCR = core.compile_model('/models/traffic/openvino/ocr.xml', 'NPU')
def letterbox(img, size=224):
    h, w = img.shape[:2]; s = size / max(h, w); nw, nh = round(w * s), round(h * s)
    r = cv2.resize(img, (nw, nh), interpolation=cv2.INTER_LINEAR)
    c = np.full((size, size, 3), 114, np.uint8); px, py = (size - nw) // 2, (size - nh) // 2
    c[py:py + nh, px:px + nw] = r; return c, s, px, py
def read(vehicle):
    H, W = vehicle.shape[:2]; c, s, px, py = letterbox(vehicle)
    x = c[:, :, ::-1].transpose(2, 0, 1)[None].astype(np.float32) / 255.0
    out = np.asarray(PL(x)[0]).reshape(-1, 6)
    for row in out:
        if row[4] < 0.35: break
        x1, y1, x2, y2 = [int(max(0, min(lim, (v - p) / s))) for v, p, lim in ((row[0], px, W), (row[1], py, H), (row[2], px, W), (row[3], py, H))]
        if x2 <= x1 or y2 <= y1: continue
        pw, ph = x2 - x1, y2 - y1
        if pw < 16 or ph < 5 or not (0.8 <= pw / max(ph, 1) <= 12): return None, 0.0, pw
        o = cv2.resize(vehicle[y1:y2, x1:x2], (128, 64), interpolation=cv2.INTER_LINEAR)[:, :, ::-1].astype(np.float32)
        txt, conf = decode_logits(np.asarray(OCR(o.transpose(2, 0, 1)[None])[0]).reshape(-1, 37)); return txt, float(conf), pw
    return None, 0.0, 0
widths = ['orig', 1000, 800, 600, 500, 400, 320, 280, 240, 200, 160, 120, 100, 80, 60]
res = collections.defaultdict(lambda: collections.Counter()); rows = []
for smp in GT:
    img = cv2.imread('/src/' + smp['source_image']);  truth = smp['ground_truth_text']
    if img is None: continue
    for W in widths:
        v = img if W == 'orig' else cv2.resize(img, (W, max(1, round(img.shape[0] * W / img.shape[1]))), interpolation=cv2.INTER_AREA)
        if W != 'orig' and W > img.shape[1]: continue
        txt, conf, pw = read(v); ok = txt == truth
        r = res[str(W)]; r['n'] += 1; r['plate_found'] += txt is not None; r['exact'] += ok
        r['exact_conf>=0.82'] += ok and conf >= 0.82; r['exact_conf>=0.35'] += ok and conf >= 0.35
        r['wrong_conf>=0.82'] += (txt is not None and not ok and conf >= 0.82)
        rows.append(dict(img=smp['source_image'], orig_w=img.shape[1], W=W, truth=truth, text=txt, conf=round(conf, 3), plate_w=pw))
json.dump(dict(summary={k: dict(v) for k, v in res.items()}, rows=rows), open('/w/plate_width_results.json', 'w'), indent=1)
print('orig vehicle widths: min/median/max', min(r['orig_w'] for r in rows), int(np.median([r['orig_w'] for r in rows])), max(r['orig_w'] for r in rows))
for W in widths:
    r = res[str(W)]
    if r['n']: print(f"vehicle {str(W):>4} px: n={r['n']:2d} plate found {r['plate_found']:2d}  exact text {r['exact']:2d} ({100*r['exact']/r['n']:.0f}%)  exact & conf>=0.82 {r['exact_conf>=0.82']:2d}  WRONG text conf>=0.82 {r['wrong_conf>=0.82']}")
