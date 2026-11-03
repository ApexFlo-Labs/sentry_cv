#!/usr/bin/env python3
"""Print the attention block of model.10 as data flow: inputs -> op -> outputs, with attributes and small constants."""
import sys

import onnx
from onnx import numpy_helper


def short(name):
    return name.replace("/model.10/m/m.0/attn/", "").replace("_output_0", "")


for path in sys.argv[1:]:
    m = onnx.load(path)
    values = {i.name: numpy_helper.to_array(i) for i in m.graph.initializer}
    for n in m.graph.node:
        if n.op_type == "Constant" and n.attribute:
            values[n.output[0]] = numpy_helper.to_array(n.attribute[0].t)
    print(f"== {path}")
    for n in m.graph.node:
        if "/model.10/" not in n.name or "attn" not in n.name or n.op_type == "Constant":
            continue
        inputs = [f"{short(i)}={values[i].ravel()[:4].tolist()}" if i in values and values[i].size <= 4 else short(i)
                  for i in n.input if not (i in values and values[i].size > 4)]
        attrs = {a.name: onnx.helper.get_attribute_value(a) for a in n.attribute}
        print(f"   {n.op_type:9s} {inputs} -> {[short(o) for o in n.output]} {attrs or ''}")
