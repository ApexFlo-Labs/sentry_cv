"""T2-FIX4b: production face extractor compiles SCRFD / AdaFace / SixDRepNet ONCE per process (shared by all cameras)
instead of once per camera; each call uses its own infer request (thread-safe). Models, preprocessing, decode, gates and
outputs unchanged."""
import sys
p = sys.argv[1]; s = open(p).read()
def rep(old, new, n=1):
    global s
    assert s.count(old) == n, (s.count(old), old[:70]); s = s.replace(old, new)
rep("class OpenVINOHeadPoseEstimator:\n", '''_SHARED_COMPILED = {}
_SHARED_LOCK = __import__("threading").Lock()


class _SharedCompiled:
    """Compiled model shared by all cameras of the process; a new infer request per call."""

    def __init__(self, compiled):
        self.compiled = compiled
        self.outputs = compiled.outputs

    def output(self, index=0):
        return self.compiled.output(index)

    def input(self, index=0):
        return self.compiled.input(index)

    def __call__(self, inputs):
        request = self.compiled.create_infer_request()
        request.infer(inputs)
        return {port: request.get_tensor(port).data.copy() for port in self.compiled.outputs}


def _shared_compile(key, factory):
    with _SHARED_LOCK:
        if key not in _SHARED_COMPILED:
            _SHARED_COMPILED[key] = _SharedCompiled(factory())
        return _SHARED_COMPILED[key]


class OpenVINOHeadPoseEstimator:
''')
rep("        self._compiled = core.compile_model(model, self.device, config)\n",
    "        self._compiled = _shared_compile((\"pose\", model_path, self.device),\n"
    "                                         lambda: core.compile_model(model, self.device, config))\n")
rep("        self._detector = core.compile_model(detector, detector_device, {\"PERFORMANCE_HINT\": \"LATENCY\"})\n",
    "        self._detector = _shared_compile((\"face-det\", detector_model, detector_device),\n"
    "                                         lambda: core.compile_model(detector, detector_device, {\"PERFORMANCE_HINT\": \"LATENCY\"}))\n")
rep("""        self._embedder = core.compile_model(core.read_model(embedder_model), embedder_device,
                                            {"PERFORMANCE_HINT": "LATENCY"})
""", """        self._embedder = _shared_compile(("face-embed", embedder_model, embedder_device),
                                         lambda: core.compile_model(core.read_model(embedder_model), embedder_device,
                                                                    {"PERFORMANCE_HINT": "LATENCY"}))
""")
open(p, 'w').write(s); print('ok')
