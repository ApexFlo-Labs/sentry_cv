"""Run the T2 detector + tracker (same model, model-proc, threshold, device, gvatrack zero-term as the runtime graph)
on MOT17 frames sampled at 8 FPS, and the runtime body-ReID model on every person box (NPU).
Writes one JSON per sequence: per frame -> boxes (label, x, y, w, h, track id, conf) and person body vectors + quality.
usage (in the T2 image): replay_mot.py SEQ_DIR OUT_JSON FPS"""
import sys, json, glob, configparser
import numpy as np, cv2, gi
gi.require_version('Gst', '1.0')
from gi.repository import Gst
from gstgva import VideoFrame
sys.path.insert(0, '/opt/traffic-pilot')
from services.worker.pipeline.person_reid import OpenVINOBodyExtractor

seq, out, FPS = sys.argv[1], sys.argv[2], int(sys.argv[3])
info = configparser.ConfigParser(); info.read(seq + '/seqinfo.ini'); s = info['Sequence']
fr, n, W, H = float(s['frameRate']), int(s['seqLength']), int(s['imWidth']), int(s['imHeight'])
sel = []; i = 0
while True:
    f = int(round(i * fr / FPS)) + 1
    if f > n: break
    sel.append(f); i += 1
Gst.init([])
CFG = '/opt/traffic-pilot/config'
P = (f'appsrc name=src format=time is-live=false block=true caps=video/x-raw,format=BGR,width={W},height={H},framerate={FPS}/1 ! '
     'videoconvert ! video/x-raw,format=NV12 ! vapostproc ! video/x-raw(memory:VAMemory),format=NV12 ! '
     'gvadetect model=/models/traffic/openvino/vehicle_person_qaft_int8/model_static_b1.xml '
     f'model-proc={CFG}/vehicle_yolo26_model_proc.json device=GPU pre-process-backend=va-surface-sharing threshold=0.3 nireq=4 ! '
     'gvatrack tracking-type=zero-term ! vapostproc ! video/x-raw,format=BGRx ! appsink name=out sync=false max-buffers=64 drop=false')
p = Gst.parse_launch(P); src, sink = p.get_by_name('src'), p.get_by_name('out'); p.set_state(Gst.State.PLAYING)
body = OpenVINOBodyExtractor('/models/surveillance/transreid_ssl_int8.xml', 'NPU')
def quality(img):  # v8_events._body_quality
    g = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    return max(0., min(1., .4 * min(1., img.shape[0] * img.shape[1] / 120000.) + .4 * min(1., cv2.Laplacian(g, cv2.CV_64F).var() / 800.)
                       + .2 * max(0., 1 - abs(g.mean() - 127.5) / 127.5)))
frames, imgs = [], {}
def pull(block):
    smp = sink.emit('try-pull-sample', (5 * Gst.SECOND) if block else 0)
    if smp is None: return False
    b = smp.get_buffer(); k = int(round(b.pts * FPS / Gst.SECOND)); img = imgs.pop(k)
    boxes, crops, idx = [], [], []
    for r in VideoFrame(b, caps=smp.get_caps()).regions():
        x, y, w, h = r.rect(); lab = r.label()
        boxes.append([lab, int(x), int(y), int(w), int(h), int(r.object_id() or 0), round(float(r.confidence()), 3)])
        if lab == 'person':
            c = img[max(y, 0):y + h, max(x, 0):x + w]
            if c.size: crops.append(c); idx.append(len(boxes) - 1)
    vec = {}
    if crops:
        V = body.embed(crops)
        for j, v, c in zip(idx, V, crops):
            vec[j] = dict(v=np.asarray(v, np.float32).round(4).tolist(), q=round(quality(c), 3), wh=[c.shape[1], c.shape[0]])
    frames.append(dict(k=k, src=sel[k], boxes=boxes, body=vec)); return True
for k, f in enumerate(sel):
    img = cv2.imread(f'{seq}/img1/{f:06d}.jpg'); imgs[k] = img
    buf = Gst.Buffer.new_wrapped(img.tobytes()); buf.pts = k * Gst.SECOND // FPS; buf.duration = Gst.SECOND // FPS
    src.emit('push-buffer', buf)
    while pull(False): pass
src.emit('end-of-stream')
while len(frames) < len(sel) and pull(True): pass
p.set_state(Gst.State.NULL)
frames.sort(key=lambda x: x['k'])
json.dump(dict(seq=seq.split('/')[-1], fps=FPS, W=W, H=H, selected=sel, frames=frames), open(out, 'w'))
print('DONE', seq, len(frames), 'of', len(sel), 'frames', sum(len(x['boxes']) for x in frames), 'boxes', flush=True)
