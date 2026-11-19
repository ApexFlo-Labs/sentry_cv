"""Patched OpenVINOGaitExtractor: shared compiled model, per-call request; output == direct plain model; 8 threads at once."""
import sys, numpy as np, threading, openvino as ov
sys.path.insert(0, '/opt/traffic-pilot'); import services.worker.pipeline.person_reid as pr
a = pr.OpenVINOGaitExtractor('/models/surveillance/gaitbase_int8.xml', 'NPU'); b = pr.OpenVINOGaitExtractor('/models/surveillance/gaitbase_int8.xml', 'NPU')
print('shared compiled model:', a.gait is b.gait); a.warmup()
ref = ov.Core().compile_model('/models/surveillance/gaitbase_int8.xml', 'NPU')
rng = np.random.default_rng(1); xs = [(rng.random((1, 30, 64, 44)) > .5).astype(np.float32) for _ in range(16)]
exp = [np.asarray(ref(x)[0]) for x in xs]; got = [None] * 16
def run(i): got[i] = np.asarray(a.gait([xs[i]])[a.gait_output])
ts = [threading.Thread(target=run, args=(i,)) for i in range(16)]; [t.start() for t in ts]; [t.join() for t in ts]
print('16 concurrent calls identical to direct plain model:', all(np.array_equal(e, g) for e, g in zip(exp, got)))
