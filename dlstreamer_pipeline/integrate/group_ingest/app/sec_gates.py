"""The secondary analytics of sec_shared.py with the plate and body gates added (gates_shared.py).

sec_shared.py and the secondary code it runs are not changed: the gates are added around the
shared secondary pipeline once, when the first camera's element is made.
"""
import json
import os

import gates_shared
import sec_shared

_cameras = json.load(open(os.environ["INGEST_CONFIG"]))["cameras"]
# Plate/body size limits are in camera pixels. The branch frame is the camera's own size by default (scale 1; plates and
# OCR from the original pixels); SEC_NATIVE=0 = the earlier 640-wide copy (scale 640 / camera width).
_native = os.getenv("SEC_NATIVE", "1") != "0"


def scale_for(width):
    """Branch-frame pixels per camera pixel for a camera of this width."""
    return 1.0 if _native else 640.0 / width


_scale = {f"camera-{i + 1:02d}": scale_for(c["width"]) for i, c in enumerate(_cameras)}
_gated = []

Stash = sec_shared.Stash


class Secondary(sec_shared.Secondary):
    def __init__(self, key):
        super().__init__(key)
        with sec_shared._lock:
            if not _gated:
                gates_shared.gate_secondary(sec_shared._pipeline, lambda camera_id: _scale.get(camera_id, 1.0))
                _gated.append(True)
