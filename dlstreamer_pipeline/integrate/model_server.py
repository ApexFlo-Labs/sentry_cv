"""ONE shared copy of the identity models (body ReID, gait, scene) for all cameras' V8 rules.

Her extractor classes (person_reid.OpenVINOBodyExtractor, OpenVINOGaitExtractor, scene_embeddings.OpenVINOSceneExtractor)
are used unchanged in each camera process; only their compiled-model attribute is a Remote proxy, so her crops, her
preprocessing, her MOG2 silhouettes and her timing decisions stay exactly as in V8, but inference runs here, once, with
4 requests in flight per model (instead of 20 cameras x 3 model copies on the NPU).
"""
import os
import time

import numpy as np

KINDS = ("body", "scene", "gait")


def serve(paths, device, requests, responses, ready):
    if os.getenv("RULES_CPUS"):  # TEST: keep this process off the P-cores (0-5) so the engine's detector threads keep them
        a, b = os.getenv("RULES_CPUS").split("-")
        os.sched_setaffinity(0, set(range(int(a), int(b) + 1)))
    import openvino as ov
    core = ov.Core()
    try:
        core.set_property({"CACHE_DIR": paths.get("cache_dir", "/tmp/ov_cache_models")})
    except Exception:
        pass
    compiled, queues, info = {}, {}, {}
    for kind in KINDS:
        compiled[kind] = core.compile_model(core.read_model(paths[kind]), device)
        info[kind] = [int(d) for d in compiled[kind].input(0).shape]
        q = ov.AsyncInferQueue(compiled[kind], 4)

        def done(request, userdata):
            cam, rid = userdata
            responses[cam].put((rid, np.array(request.get_output_tensor(0).data, copy=True)))
        q.set_callback(done)
        queues[kind] = q
    ready.put(info)
    served = {k: 0 for k in KINDS}
    last = time.time()
    while True:
        message = requests.get()
        if message is None:
            break
        cam, rid, kind, tensor = message
        queues[kind].start_async({0: tensor}, (cam, rid))
        served[kind] += 1
        if time.time() - last >= 30:
            last = time.time()
            print(f"{time.strftime('%T')} model server: served {served}", flush=True)
    for q in queues.values():
        q.wait_all()
    print(f"{time.strftime('%T')} model server stopped: served {served}", flush=True)


class Remote:
    """Stands in for an OpenVINO CompiledModel in her classes: model([tensor])[output] -> array."""

    def __init__(self, kind, cam, requests, response):
        self.kind, self.cam, self.requests, self.response, self.rid = kind, cam, requests, response, 0

    def __call__(self, inputs):
        self.rid += 1
        self.requests.put((self.cam, self.rid, self.kind, np.ascontiguousarray(inputs[0])))
        while True:
            rid, data = self.response.get(timeout=60)
            if rid == self.rid:
                return {"out": data}


def make_extractors(cam, requests, response, info):
    """Her three extractor objects, built without loading a model copy (attributes as her __init__ sets them)."""
    from collections import defaultdict
    from pipeline.person_reid import OpenVINOBodyExtractor, OpenVINOGaitExtractor
    from pipeline.scene_embeddings import OpenVINOSceneExtractor
    body = OpenVINOBodyExtractor.__new__(OpenVINOBodyExtractor)
    body.compiled, body.input, body.output = Remote("body", cam, requests, response), None, "out"
    body.height, body.width = info["body"][2], info["body"][3]
    scene = OpenVINOSceneExtractor.__new__(OpenVINOSceneExtractor)
    scene.compiled, scene.input, scene.output = Remote("scene", cam, requests, response), None, "out"
    gait = OpenVINOGaitExtractor.__new__(OpenVINOGaitExtractor)
    gait.source = "background_subtraction"
    gait.gait, gait.gait_output = Remote("gait", cam, requests, response), "out"
    gait.sequence_length = info["gait"][1]
    gait.min_sequence, gait.max_sequence, gait.minimum_motion_pixels = 20, 60, 2.0  # her constructor defaults
    gait.buffers, gait.buffer_times = defaultdict(list), defaultdict(list)
    gait.cache, gait.backgrounds, gait.centers = {}, {}, {}
    return body, scene, gait
