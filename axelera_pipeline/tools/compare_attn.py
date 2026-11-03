#!/usr/bin/env python3
"""Compare how two YOLO26 ONNX exports build the attention block of model.10 (the part QTools could not quantize)."""
import sys

import onnx
from onnx import numpy_helper

for path in sys.argv[1:]:
    m = onnx.load(path)
    inits = {i.name: numpy_helper.to_array(i) for i in m.graph.initializer}
    print(f"== {path}\n   producer {m.producer_name} {m.producer_version} | opset {[(o.domain or 'ai.onnx', o.version) for o in m.opset_import]} | ir {m.ir_version}")
    for meta in m.metadata_props:
        if meta.key in ("version", "date", "imgsz", "args"):
            print(f"   {meta.key}: {meta.value[:120]}")
    for n in m.graph.node:
        if "/model.10/" in n.name and "attn" in n.name:
            consts = [f"{inits[i].ravel()[:3]}" for i in n.input if i in inits and inits[i].size <= 4]
            print(f"   {n.op_type:10s} {n.name}  {' '.join(consts)}")
