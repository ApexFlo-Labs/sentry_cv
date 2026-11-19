"""T2-FIX3 (user rule: no code inside models): gait runs on the PLAIN gaitbase_int8 model, not the montage wrapper
(gaitbase_dlstreamer_montage = ops added in front of GaitBase). Parity: 40/40 sequences bit-identical on NPU
(tests/parity/gait_plain/result.txt). One compiled model per (path, device) shared by all cameras of the process;
every call uses its own infer request (thread-safe). SENTINEL_GAIT_PLAIN=0 restores the montage path."""
import sys
d = sys.argv[1]
def patch(p, old, new):
    s = open(p).read(); assert s.count(old) == 1, (p, old[:70]); open(p, 'w').write(s.replace(old, new))
patch(d + '/person_reid.py', "class OpenVINOGaitExtractor:\n", '''_SHARED_GAIT = {}


class _SharedGaitModel:
    """One compiled plain GaitBase per (path, device); a new infer request per call (safe across camera threads)."""

    def __init__(self, compiled):
        self.compiled = compiled

    def input(self, index=0):
        return self.compiled.input(index)

    def output(self, index=0):
        return self.compiled.output(index)

    def __call__(self, inputs):
        request = self.compiled.create_infer_request()
        request.infer(inputs)
        return {port: request.get_tensor(port).data.copy() for port in self.compiled.outputs}


class OpenVINOGaitExtractor:
''')
patch(d + '/person_reid.py', "        self.gait = core.compile_model(core.read_model(str(gait_path)), gait_device)\n",
      "        key = (str(gait_path), str(gait_device))\n"
      "        if key not in _SHARED_GAIT:\n"
      "            _SHARED_GAIT[key] = _SharedGaitModel(core.compile_model(core.read_model(str(gait_path)), gait_device))\n"
      "        self.gait = _SHARED_GAIT[key]\n")
patch(d + '/surface_stream.py', "            if dls_secondaries:\n                from pipeline.dls_secondary import DlsGaitExtractor\n",
      "            if dls_secondaries and os.getenv('SENTINEL_GAIT_PLAIN', '1') == '0':\n                from pipeline.dls_secondary import DlsGaitExtractor\n")
patch(d + '/surface_stream.py', '            gait_extractor.actual_device\n            if dls_secondaries else sf.GAIT_DEVICE\n',
      '            getattr(gait_extractor, "actual_device", sf.GAIT_DEVICE)\n            if dls_secondaries else sf.GAIT_DEVICE\n')
print('ok')
