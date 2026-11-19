"""Original production OpenVINOFaceExtractor (image file) vs patched (shared compiled models): same frames / person boxes
(MOT17-02 ground-truth pedestrians, 40 frames). Compares boxes, embeddings, pose, quality; then 4 threads at once."""
import sys, importlib.util, csv, collections, threading, numpy as np, cv2
sys.path.insert(0, '/opt/traffic-pilot/services/worker'); sys.path.insert(0, '/opt/traffic-pilot')
def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path); m = importlib.util.module_from_spec(spec); sys.modules[name] = m; spec.loader.exec_module(m); return m
orig = load('/opt/traffic-pilot/services/worker/detectors/backends/openvino_face.py', 'face_orig')
new = load('/w/openvino_face.py', 'face_new')
M = '/models/face/openvino/'; args = (M + 'scrfd_500m.onnx', M + 'adaface_ir101_int8.xml', M + 'best_a0_fp16.xml')
A = orig.OpenVINOFaceExtractor(*args); B = new.OpenVINOFaceExtractor(*args); B2 = new.OpenVINOFaceExtractor(*args)
print('shared across instances:', B._detector is B2._detector and B._embedder is B2._embedder)
class P:  # minimal person object (bbox + metadata.track_id), as FaceSamplePipeline passes
    def __init__(s, b, t): s.bbox = b; s.metadata = {'track_id': t}
SEQ = sys.argv[1]
gt = collections.defaultdict(list)
for r in csv.reader(open(f'/mot/MOT17/train/{SEQ}/gt/gt.txt')):
    f, tid, x, y, w, h, flag, cls, vis = int(r[0]), int(r[1]), *map(float, r[2:6]), int(r[6]), int(r[7]), float(r[8])
    if flag == 1 and cls == 1 and vis > .5 and h > 120: gt[f].append(P((x, y, x + w, y + h), tid))
frames = sorted(gt)[::4][:120]; n = 0; same = 0; diff = []
res = {}
for f in frames:
    img = cv2.imread(f'/mot/MOT17/train/{SEQ}/img1/{f:06d}.jpg')
    a = A.extract(img, gt[f]); b = B.extract(img, gt[f]); res[f] = (img, b)
    for x, y in zip(a, b):
        n += 1; ok = x.bbox == y.bbox and np.array_equal(x.embedding, y.embedding) and x.yaw_degrees == y.yaw_degrees and x.quality == y.quality
        same += ok
        if not ok: diff.append(float(x.embedding @ y.embedding))
    if len(a) != len(b): print('count mismatch frame', f, len(a), len(b))
print('faces', n, 'identical', same, 'non-identical cosines', diff[:5])
out = {}
def run(f): out[f] = B2.extract(res[f][0], gt[f])
ts = [threading.Thread(target=run, args=(f,)) for f in frames[:12]]; [t.start() for t in ts]; [t.join() for t in ts]
ok = all(len(out[f]) == len(res[f][1]) and all(np.array_equal(p.embedding, q.embedding) for p, q in zip(out[f], res[f][1])) for f in frames[:12])
print('12 frames in 12 threads at once identical:', ok)
