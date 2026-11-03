#!/usr/bin/env python3
"""Write a copy of a YOLO26 ONNX with each attention Mul moved from before Transpose/MatMul to just before Softmax."""
import sys

import onnx

src, dst = sys.argv[1:3]
m = onnx.load(src)
g = m.graph
users = {}
for n in g.node:
    for i in n.input:
        users.setdefault(i, []).append(n)


def only_user(node, op):
    found = users.get(node.output[0], [])
    if len(found) != 1 or found[0].op_type != op:
        sys.exit(f"unexpected graph after {node.name}: {[f.op_type for f in found]}")
    return found[0]


nodes = list(g.node)
moved = 0
for mul in [n for n in nodes if n.op_type == "Mul" and "/attn/" in n.name]:
    transpose = only_user(mul, "Transpose")
    matmul = only_user(transpose, "MatMul")
    softmax = only_user(matmul, "Softmax")
    transpose.input[0] = mul.input[0]
    mul.input[0] = matmul.output[0]
    softmax.input[0] = mul.output[0]
    nodes.remove(mul)
    nodes.insert(nodes.index(matmul) + 1, mul)
    moved += 1
    print("moved", mul.name)
if moved != 2:
    sys.exit(f"expected 2 attention blocks, found {moved}; nothing saved")
del g.node[:]
g.node.extend(nodes)
del g.value_info[:]
onnx.checker.check_model(m, full_check=True)
onnx.save(m, dst)
print("saved", dst)
