"""Why plain face path gives fewer face samples than the T2 graph chain (f2b-SA-1x8-plain: 10 vs 26-28).
The chain's scrfd_top1x_u8 outputs the face box enlarged 1.6x (square) and the bridge uses that box as face width
and as pose-crop base; the plain path uses the real box with FACE_MIN_SIZE=28.
For MOT17 ground-truth people (h>120, vis>.5), every 3rd frame: plain SCRFD detection on the person crop, best face,
real width; then counts: no face / real width < 28 / real width >= 28; and how many < 28 would pass at 1.6x width.
Then pose gate on both crop bases (real box + 0.2 context vs 1.6x square + 0.2 context).
usage (inside the image): face_size_rule.py SEQ"""
import sys, csv, collections, importlib.util, numpy as np, cv2
sys.path.insert(0, '/opt/traffic-pilot/services/worker'); sys.path.insert(0, '/opt/traffic-pilot')
spec = importlib.util.spec_from_file_location('fx', '/opt/traffic-pilot/services/worker/detectors/backends/openvino_face.py')
fx = importlib.util.module_from_spec(spec); sys.modules['fx'] = fx; spec.loader.exec_module(fx)
M = '/models/face/openvino/'
E = fx.OpenVINOFaceExtractor(M + 'scrfd_500m.onnx', M + 'adaface_ir101_int8.xml', M + 'best_a0_fp16.xml')
SEQ = sys.argv[1]
gt = collections.defaultdict(list)
for r in csv.reader(open(f'/mot/MOT17/train/{SEQ}/gt/gt.txt')):
    f, tid, x, y, w, h, flag, cls, vis = int(r[0]), int(r[1]), *map(float, r[2:6]), int(r[6]), int(r[7]), float(r[8])
    if flag == 1 and cls == 1 and vis > .5 and h > 120: gt[f].append((x, y, x + w, y + h))
c = collections.Counter(); widths = []
def pose_ok(img, box, ctx):
    x1, y1, x2, y2 = box; pw, ph = (x2 - x1) * ctx, (y2 - y1) * ctx
    H, W = img.shape[:2]
    crop = img[max(0, int(y1 - ph)):min(H, int(y2 + ph)), max(0, int(x1 - pw)):min(W, int(x2 + pw))]
    if crop.size == 0: return False
    p, yw, r = E._pose.estimate(crop)
    return abs(p) <= E.max_pitch and abs(yw) <= E.max_yaw and abs(r) <= E.max_roll
for f in sorted(gt)[::3]:
    img = cv2.imread(f'/mot/MOT17/train/{SEQ}/img1/{f:06d}.jpg'); H, W = img.shape[:2]
    for (x1, y1, x2, y2) in gt[f]:
        x1, y1, x2, y2 = int(max(0, x1)), int(max(0, y1)), int(min(W, x2)), int(min(H, y2))
        c['people'] += 1
        if x2 - x1 < E.min_face_px or y2 - y1 < E.min_face_px * 2: c['person_too_small'] += 1; continue
        d = E._detect(img[y1:y2, x1:x2])
        if not d: c['no_face_detected'] += 1; continue
        box, _, score = max(d, key=lambda t: t[2]); fw = float(box[2] - box[0]); widths.append(fw)
        real = (x1 + box[0], y1 + box[1], x1 + box[2], y1 + box[3])
        cx, cy, s = (real[0] + real[2]) / 2, (real[1] + real[3]) / 2, 1.6 * max(real[2] - real[0], real[3] - real[1]) / 2
        sq = (cx - s, cy - s, cx + s, cy + s)
        if fw >= 28:
            c['face_ge_28'] += 1
            c['ge28_pose_ok_real'] += pose_ok(img, real, .2); c['ge28_pose_ok_square'] += pose_ok(img, sq, .2)
        else:
            c['face_lt_28'] += 1
            if 1.6 * max(real[2] - real[0], real[3] - real[1]) >= 28:
                c['lt28_but_square_ge_28'] += 1; c['lt28_square_pose_ok'] += pose_ok(img, sq, .2)
print(SEQ, dict(c))
print('face width percentiles (real px) 10/25/50/75/90:', [round(float(np.percentile(widths, q)), 1) for q in (10, 25, 50, 75, 90)] if widths else None)
