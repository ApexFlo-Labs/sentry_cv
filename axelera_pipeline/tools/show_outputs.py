#!/usr/bin/env python3
"""Print the last Conv outputs of a YOLO26 ONNX head, in graph output order, plus the graph outputs."""
import sys

import onnx
from onnx import shape_inference

for path in sys.argv[1:]:
    m = shape_inference.infer_shapes(onnx.load(path))
    shapes = {v.name: [d.dim_value for d in v.type.tensor_type.shape.dim] for v in list(m.graph.value_info) + list(m.graph.output)}
    print(f"== {path}")
    print("   graph outputs:", [(o.name, shapes.get(o.name)) for o in m.graph.output])
    for n in m.graph.node:
        if n.op_type == "Conv" and "/model.23/" in n.name and shapes.get(n.output[0], [0, 0])[1] in (4, 9, 80):
            print(f"   {n.name}: {shapes[n.output[0]]}")
