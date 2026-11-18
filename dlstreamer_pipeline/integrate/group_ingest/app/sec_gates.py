"""The secondary analytics of sec_shared.py with the plate and body gates added (gates_shared.py).

sec_shared.py and the secondary code it runs are not changed: the gates are added around the
shared secondary pipeline once, when the first camera's element is made.
"""
import json
import os

import gates_shared
import sec_shared

_cameras = json.load(open(os.environ["INGEST_CONFIG"]))["cameras"]
_scale = {f"camera-{i + 1:02d}": 640.0 / c["width"] for i, c in enumerate(_cameras)}
_gated = []

Stash = sec_shared.Stash


class Secondary(sec_shared.Secondary):
    def __init__(self, key):
        super().__init__(key)
        with sec_shared._lock:
            if not _gated:
                gates_shared.gate_secondary(sec_shared._pipeline, lambda camera_id: _scale.get(camera_id, 1.0))
                _gated.append(True)
