"""Writes secondary results, one JSON line per processed frame; one copy shared by all gvapython elements."""
import json
import os
import threading

import numpy as np

_names = [c["name"] for c in json.load(open(os.environ["INGEST_CONFIG"]))["cameras"]]
_dir = os.environ["INGEST_RESULTS"]
_files = {}
_lock = threading.Lock()


def _plain(value):
    if isinstance(value, np.ndarray):
        return value.tolist() if value.size <= 4096 else {"shape": list(value.shape)}
    if isinstance(value, np.generic):
        return value.item()
    return str(value)


def write(key, payload):
    line = json.dumps({"camera": _names[int(key)], **payload}, default=_plain) + "\n"
    with _lock:
        f = _files.get(key)
        if f is None:
            path = os.devnull if _dir == os.devnull else os.path.join(_dir, f"{_names[int(key)]}.secondary.jsonl")
            f = _files[key] = open(path, "a", buffering=1)
        f.write(line)
