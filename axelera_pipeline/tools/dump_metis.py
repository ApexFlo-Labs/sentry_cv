#!/usr/bin/env python3
"""Run a model on Metis through the Voyager stream and save every detection, one JSON line per frame."""
import json
import sys
from pathlib import Path

sys.path.append("/home/admin1/voyager-sdk-latest")
from axelera.app import config, create_inference_stream  # noqa: E402

network, source, frames, out, build_root = sys.argv[1:6]
stream = create_inference_stream(
    network=network,
    sources=[source],
    frames=int(frames),
    aipu_cores=4,  # the Python default is 1; inference.py uses 4
    metis=config.Metis.m2,
    allow_hardware_codec=True,
    build_root=Path(build_root),
    data_root=Path.home() / "chandana_axelera/data",
)
count = boxes = 0
try:
    with open(out, "w") as f:
        for result in stream:
            found = [[*map(float, d.box), float(d.score), int(d.class_id)] for d in result.detections]
            f.write(json.dumps({"frame": count, "boxes": found}) + "\n")
            count += 1
            boxes += len(found)
finally:
    stream.stop()
print(f"{network}: {count} frames, {boxes} boxes -> {out}")
