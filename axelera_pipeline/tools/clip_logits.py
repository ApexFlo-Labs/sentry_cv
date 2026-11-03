#!/usr/bin/env python3
"""Write a copy of a YOLO26 ONNX with the class outputs of the one2one head clipped to a fixed range."""
import sys

import onnx
from onnx import TensorProto, helper

src, dst, limit = sys.argv[1], sys.argv[2], float(sys.argv[3])
m = onnx.load(src)
g = m.graph
g.initializer.extend([helper.make_tensor("clip_min", TensorProto.FLOAT, [], [-limit]),
                      helper.make_tensor("clip_max", TensorProto.FLOAT, [], [limit])])
nodes = []
done = 0
for n in g.node:
    nodes.append(n)
    if n.op_type == "Conv" and "/model.23/one2one_cv3." in n.name and n.name.endswith(".2/Conv"):
        out = n.output[0]
        n.output[0] = out + "_unclipped"
        nodes.append(helper.make_node("Clip", [n.output[0], "clip_min", "clip_max"], [out], name=n.name + "_clip"))
        done += 1
        print("clipped", n.name)
if done != 3:
    sys.exit(f"expected 3 class outputs, found {done}; nothing saved")
del g.node[:]
g.node.extend(nodes)
del g.value_info[:]
onnx.checker.check_model(m, full_check=True)
onnx.save(m, dst)
print("saved", dst)
