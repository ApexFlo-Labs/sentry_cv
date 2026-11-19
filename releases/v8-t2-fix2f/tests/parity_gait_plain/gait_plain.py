"""Plain gaitbase_int8 (30x64x44, values 0..1, production OpenVINOGaitExtractor input) vs the montage wrapper model
(gaitbase_dlstreamer_montage: u8 montage 320x264 -> /255 inside the model) on the SAME silhouette sequences, NPU.
Wrapper input is fed exactly as _DlsGaitCallable builds it (tile = clip(s*255) uint8, BGR copies, float tensor)."""
import numpy as np, openvino as ov, cv2, glob
core = ov.Core()
plain = core.compile_model('/models/surveillance/gaitbase_int8.xml', 'NPU')
mont = core.compile_model('/models/surveillance/gaitbase_dlstreamer_montage.xml', 'NPU')
rng = np.random.default_rng(0); seqs = []
for i in range(40):  # binary person-like silhouettes: ellipse body that moves, random holes
    s = np.zeros((30, 64, 44), np.float32)
    for t in range(30):
        m = np.zeros((64, 44), np.uint8)
        cv2.ellipse(m, (22 + int(4 * np.sin(t / 3 + i)), 32), (8 + i % 5, 28), 0, 0, 360, 255, -1)
        cv2.line(m, (22, 40), (14 + int(8 * np.sin(t / 2)), 63), 255, 4); cv2.line(m, (22, 40), (30 - int(8 * np.sin(t / 2)), 63), 255, 4)
        m[rng.random((64, 44)) < 0.02] = 0
        s[t] = m / 255.0
    seqs.append(s)
cos = []; maxd = []
for s in seqs:
    a = np.asarray(plain(s[None])[0]).reshape(-1)
    mon = np.zeros((320, 264, 3), np.uint8)
    for k, sil in enumerate(s):
        r, c = divmod(k, 6); mon[r * 64:(r + 1) * 64, c * 44:(c + 1) * 44] = np.clip(sil * 255.0, 0, 255).astype(np.uint8)[:, :, None]
    x = mon.transpose(2, 0, 1)[None].astype(np.float32)
    b = np.asarray(mont(x)[0]).reshape(-1)
    cos.append(float(a @ b / np.linalg.norm(a) / np.linalg.norm(b))); maxd.append(float(np.abs(a - b).max()))
print('sequences', len(seqs), 'cosine min %.6f median %.6f' % (min(cos), np.median(cos)), 'max abs diff %.6g' % max(maxd), 'identical', sum(d == 0 for d in maxd))
