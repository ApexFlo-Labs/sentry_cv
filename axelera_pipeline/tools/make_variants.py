#!/usr/bin/env python3
"""From one YOLO26 ONNX, write test variants: Softmax axis -1 (same opset), and converted to opset 17."""
import sys

import onnx
from onnx import version_converter

src, axis_dst, opset_dst = sys.argv[1:4]


def set_axis(model):
    for n in model.graph.node:
        if n.op_type == "Softmax":
            for a in n.attribute:
                if a.name == "axis":
                    a.i = -1


m = onnx.load(src)
set_axis(m)
onnx.checker.check_model(m)
onnx.save(m, axis_dst)
print("saved", axis_dst)

c = version_converter.convert_version(onnx.load(src), 17)
set_axis(c)
onnx.checker.check_model(c)
onnx.save(c, opset_dst)
print("saved", opset_dst, "opset", [o.version for o in c.opset_import])
print("Softmax nodes:", [n.name for n in c.graph.node if n.op_type == "Softmax"])
