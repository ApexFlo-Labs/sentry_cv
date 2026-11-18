"""Frozen picture check, fed from the 1 fps secondary branch; one copy shared with the watchdog in ingest.py.

Decoded frames of a still picture are not byte-identical, so the picture counts as unchanged when almost no pixels moved noticeably.
"""
import time

import numpy as np

PIXEL_CHANGE, CHANGED_SHARE = 10, 0.002
_last = {}


def check(key, bgr):
    small = bgr[::8, ::8].astype(np.int16)
    now = time.time()
    seen = _last.get(int(key))
    if seen is None or seen[0].shape != small.shape or np.mean(np.abs(small - seen[0]) > PIXEL_CHANGE) >= CHANGED_SHARE:
        _last[int(key)] = (small, now, seen is not None)
    else:
        _last[int(key)] = (small, seen[1], seen[2])


def frozen_for(key):
    seen = _last.get(int(key))
    return time.time() - seen[1] if seen else 0.0


def changed(key):
    """True once the picture has really changed since the camera was last rebuilt."""
    seen = _last.get(int(key))
    return bool(seen and seen[2])


def reset(key):
    _last.pop(int(key), None)


def tracked():
    return len(_last)
