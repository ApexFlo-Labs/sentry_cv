"""SCRFD (scrfd_500m) for DL Streamer that outputs the TIGHT face box AND its 5 landmarks, so no second SCRFD is needed.

gvadetect's yolo_v26 converter reads rows [x1, y1, x2, y2, score, class] only, so the model outputs 6 rows [1, 6, 6]:
  row 0: the best face box, tight (no 1.6x), class 0 "face"
  rows 1-5: the 5 landmarks of that same face as small boxes (landmark +-HALF px in the 320 input), classes 1..5 "lm0".."lm4"
gvadetect maps all rows back to frame coordinates (its own resize/padding), so each landmark = centre of its box.
Input U8 BGR [1,3,320,320], same pre-processing as scrfd_top1x_u8 (prepare_align.py part 1).
    prepare_top1lm.py MODELS_DIR OUT_DIR
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

STRIDES = (8, 16, 32)
SCORES, BOXES, POINTS = ("443", "468", "493"), ("446", "471", "496"), ("449", "474", "499")
HALF = 2.0
models, out = Path(sys.argv[1]), Path(sys.argv[2])
core = ov.Core()
i64 = lambda v: ops.constant(np.asarray(v, np.int64))
f32 = lambda v: ops.constant(np.asarray(v, np.float32))


def centers(size, stride):
    n = size // stride
    gy, gx = np.mgrid[0:n, 0:n]
    return np.repeat(np.stack([gx, gy], axis=-1).reshape(-1, 2).astype(np.float32) * stride, 2, axis=0)


model = core.read_model(models / "face/openvino/scrfd_500m.onnx")
model.reshape({model.input(0): [1, 3, 320, 320]})
outputs = {o.get_any_name(): o.get_node().input_value(0) for o in model.outputs}
scores_all, boxes_all, points_all = [], [], []
for level, stride in enumerate(STRIDES):
    c = centers(320, stride)
    scores_all.append(ops.reshape(outputs[SCORES[level]], i64([-1]), False))
    boxes_all.append(ops.add(f32(np.concatenate([c, c], 1)), ops.multiply(outputs[BOXES[level]], f32([-stride, -stride, stride, stride]))))
    points_all.append(ops.add(f32(np.tile(c, 5)), ops.multiply(outputs[POINTS[level]], f32(stride))))
top = ops.topk(ops.concat(scores_all, 0), i64(1), 0, "max", "value")
score = ops.reshape(top.output(0), i64([1, 1]), False)                                  # [1,1]
box = ops.gather(ops.concat(boxes_all, 0), top.output(1), i64(0))                       # [1,4] tight
pts = ops.reshape(ops.gather(ops.concat(points_all, 0), top.output(1), i64(0)), i64([5, 2]), False)
lm_boxes = ops.concat([ops.subtract(pts, f32(HALF)), ops.add(pts, f32(HALF))], 1)       # [5,4]
rows_box = ops.concat([box, lm_boxes], 0)                                               # [6,4]
rows_score = ops.broadcast(score, i64([6, 1]))                                          # [6,1]
rows_class = f32(np.arange(6, dtype=np.float32).reshape(6, 1))
result = ops.unsqueeze(ops.concat([rows_box, rows_score, rows_class], 1), i64([0]))     # [1,6,6]
result.output(0).set_names({"output0"})
m = ov.Model([result], model.get_parameters(), "scrfd_top1lm")
ppp = PrePostProcessor(m)
inp = ppp.input()
inp.tensor().set_element_type(Type.u8).set_layout(Layout("NCHW"))
inp.model().set_layout(Layout("NCHW"))
inp.preprocess().convert_element_type(Type.f32).reverse_channels().mean(127.5).scale(128.0)
ov.save_model(ppp.build(), out / "scrfd_top1lm_u8.xml")
print("saved", out / "scrfd_top1lm_u8.xml")
