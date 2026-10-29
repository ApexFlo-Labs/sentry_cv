"""SCRFD with the decode inside the model: outputs only the best face per crop, as [1, 1, 6].

The 9 raw outputs (scores, box distances, landmarks at strides 8/16/32) are decoded into boxes in
the graph exactly as openvino_face.py does in numpy, then TopK keeps the highest score. NMS never
removes the highest-scoring box, so this is the same face openvino_face.py picks. The output layout
[x1, y1, x2, y2, score, class] in input pixels is what DL Streamer's yolo_v26 converter reads, so
gvadetect turns it into a face box without Python. Landmarks are not output (speed test only).
"""
import sys
from pathlib import Path

import numpy as np
import openvino as ov
from openvino import Layout, Type
from openvino.preprocess import PrePostProcessor

try:
    import openvino.opset13 as ops
except ImportError:
    from openvino.runtime import opset13 as ops

SIZE, STRIDES = 320, (8, 16, 32)
SCORES, BOXES = ("443", "468", "493"), ("446", "471", "496")

models, out = Path(sys.argv[1]), Path(sys.argv[2])
core = ov.Core()
model = core.read_model(models / "face/openvino/scrfd_500m.onnx")
model.reshape({model.input(0): [1, 3, SIZE, SIZE]})
# build on a copy (the pre-processing step rewires the input) and attach to the layers feeding
# each output marker, not to the markers themselves, or the model cannot be saved
base = model.clone()
outputs = {o.get_any_name(): o.get_node().input_value(0) for o in base.outputs}

scores_all, boxes_all = [], []
for level, stride in enumerate(STRIDES):
    n = SIZE // stride
    gy, gx = np.mgrid[0:n, 0:n]
    centers = np.repeat(np.stack([gx, gy], axis=-1).reshape(-1, 2).astype(np.float32) * stride, 2, axis=0)
    # box = (cx - l, cy - t, cx + r, cy + b) with distances scaled by the stride
    centers4 = np.concatenate([centers, centers], axis=1)
    sign = np.array([-stride, -stride, stride, stride], np.float32)
    scores_all.append(ops.reshape(outputs[SCORES[level]], ops.constant(np.array([-1], np.int64)), False))
    boxes_all.append(ops.add(ops.constant(centers4), ops.multiply(outputs[BOXES[level]], ops.constant(sign))))
scores = ops.concat(scores_all, 0)
boxes = ops.concat(boxes_all, 0)
top = ops.topk(scores, ops.constant(np.array(1, np.int64)), 0, "max", "value")
best_box = ops.gather(boxes, top.output(1), ops.constant(np.array(0, np.int64)))
best_score = ops.unsqueeze(top.output(0), ops.constant(np.array([1], np.int64)))
face_class = ops.constant(np.zeros((1, 1), np.float32))
result = ops.unsqueeze(ops.concat([best_box, best_score, face_class], 1), ops.constant(np.array([0], np.int64)))
result.output(0).set_names({"output0"})
top1 = ov.Model([result], base.get_parameters(), "scrfd_top1")

ppp = PrePostProcessor(top1)
inp = ppp.input()
inp.tensor().set_element_type(Type.u8).set_layout(Layout("NCHW"))
inp.model().set_layout(Layout("NCHW"))
inp.preprocess().convert_element_type(Type.f32).reverse_channels().mean(127.5).scale(128.0)
top1 = ppp.build()
ov.save_model(top1, out / "scrfd_top1_u8.xml")

# check against a plain numpy decode of the raw model on random input, before trusting it
raw = core.compile_model(model, "CPU")
fused = core.compile_model(top1, "CPU")
image = np.random.default_rng(0).integers(0, 255, (1, 3, SIZE, SIZE), dtype=np.uint8)
x = ((image[:, ::-1].astype(np.float32)) - 127.5) / 128.0
r = raw([x])
ref_scores, ref_boxes = [], []
for level, stride in enumerate(STRIDES):
    n = SIZE // stride
    gy, gx = np.mgrid[0:n, 0:n]
    c = np.repeat(np.stack([gx, gy], axis=-1).reshape(-1, 2).astype(np.float32) * stride, 2, axis=0)
    d = np.asarray(r[BOXES[level]]).reshape(-1, 4) * stride
    ref_scores.append(np.asarray(r[SCORES[level]]).reshape(-1))
    ref_boxes.append(np.concatenate([c - d[:, :2], c + d[:, 2:]], axis=1))
ref_scores, ref_boxes = np.concatenate(ref_scores), np.vstack(ref_boxes)
best = int(ref_scores.argmax())
got = np.asarray(fused([image])[fused.output(0)]).reshape(-1)
want = np.concatenate([ref_boxes[best], [ref_scores[best], 0.0]])
print("scrfd_top1_u8.xml output", list(top1.output(0).get_partial_shape()),
      "| max difference vs numpy decode %.5f" % float(np.abs(got - want).max()), "->",
      "OK" if np.allclose(got, want, atol=1e-3) else "MISMATCH")
