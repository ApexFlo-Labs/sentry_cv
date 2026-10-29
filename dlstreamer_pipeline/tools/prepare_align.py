"""Face alignment inside the model, for DL Streamer (whose own alignment fails on this version).

Builds two models in OUT:
  scrfd_top1x_u8.xml - the face detector of prepare_top1.py, but the box it outputs is a square
                       EXPAND times the face size, so the next model gets the face with margin;
  face_align_u8.xml  - runs on that square crop (resized to CROP x CROP): SCRFD finds the 5
                       landmarks, the similarity transform to SentryCV's 112x112 template is
                       solved by least squares, the crop is warped with GridSample (bilinear,
                       zero border, like cv2.warpAffine), and AdaFace makes the 512-d embedding.
openvino_face.py does the same steps in numpy/OpenCV; the self-check at the end runs both on one
image and prints the cosine between the two embeddings.

    prepare_align.py MODELS_DIR OUT_DIR [CROP] [EXPAND] [SUFFIX] [SWAP]
SWAP=1 builds both models for RGB input (for the case where DL Streamer hands over RGB, not BGR).
CROP (192) and EXPAND (1.6) can be changed for experiments; SUFFIX names the output files
(scrfd_top1x_u8SUFFIX.xml, face_align_u8SUFFIX.xml).
"""
import sys
from pathlib import Path

import cv2
import numpy as np
import openvino as ov
from openvino import Layout, Type
from openvino.preprocess import PrePostProcessor

try:
    import openvino.opset13 as ops
    import openvino.opset9 as ops9
except ImportError:
    from openvino.runtime import opset13 as ops
    from openvino.runtime import opset9 as ops9

CROP = int(sys.argv[3]) if len(sys.argv) > 3 else 192
EXPAND = float(sys.argv[4]) if len(sys.argv) > 4 else 1.6
SUFFIX = sys.argv[5] if len(sys.argv) > 5 else ""
SWAP = len(sys.argv) > 6 and sys.argv[6] == "1"
CHIP, STRIDES = 112, (8, 16, 32)
SCORES, BOXES, POINTS = ("443", "468", "493"), ("446", "471", "496"), ("449", "474", "499")
TEMPLATE = np.asarray([[38.2946, 51.6963], [73.5318, 51.5014], [56.0252, 71.7366],
                       [41.5493, 92.3655], [70.7299, 92.2041]], np.float32)

models, out = Path(sys.argv[1]), Path(sys.argv[2])
core = ov.Core()
i64 = lambda value: ops.constant(np.asarray(value, np.int64))
f32 = lambda value: ops.constant(np.asarray(value, np.float32))


def centers(size, stride):
    n = size // stride
    gy, gx = np.mgrid[0:n, 0:n]
    return np.repeat(np.stack([gx, gy], axis=-1).reshape(-1, 2).astype(np.float32) * stride, 2, axis=0)


def scrfd(size):
    model = core.read_model(models / "face/openvino/scrfd_500m.onnx")
    model.reshape({model.input(0): [1, 3, size, size]})
    return model, {o.get_any_name(): o.get_node().input_value(0) for o in model.outputs}


def u8_input(model, rgb_mean_scale=None):
    ppp = PrePostProcessor(model)
    inp = ppp.input()
    inp.tensor().set_element_type(Type.u8).set_layout(Layout("NCHW"))
    inp.model().set_layout(Layout("NCHW"))
    steps = inp.preprocess().convert_element_type(Type.f32)
    if rgb_mean_scale:
        # the detector wants RGB: flip BGR input, or keep it as is when the input is already RGB (SWAP)
        if not SWAP:
            steps.reverse_channels()
        steps.mean(rgb_mean_scale[0]).scale(rgb_mean_scale[1])
    return ppp.build()


# ---- 1. face detector whose box is a square EXPAND x the face, same centre ----
det, outputs = scrfd(320)
scores_all, boxes_all = [], []
for level, stride in enumerate(STRIDES):
    c = centers(320, stride)
    scores_all.append(ops.reshape(outputs[SCORES[level]], i64([-1]), False))
    boxes_all.append(ops.add(f32(np.concatenate([c, c], 1)),
                             ops.multiply(outputs[BOXES[level]], f32([-stride, -stride, stride, stride]))))
top = ops.topk(ops.concat(scores_all, 0), i64(1), 0, "max", "value")
box = ops.gather(ops.concat(boxes_all, 0), top.output(1), i64(0))            # [1, 4] x1 y1 x2 y2
x1y1, x2y2 = ops.split(box, i64(1), 2).outputs()
centre = ops.multiply(ops.add(x1y1, x2y2), f32(0.5))
half = ops.multiply(ops.reduce_max(ops.subtract(x2y2, x1y1), i64([1]), True), f32(EXPAND / 2))
square = ops.concat([ops.subtract(centre, half), ops.add(centre, half)], 1)
result = ops.unsqueeze(ops.concat([square, ops.unsqueeze(top.output(0), i64([1])), f32(np.zeros((1, 1)))], 1), i64([0]))
result.output(0).set_names({"output0"})
detx = u8_input(ov.Model([result], det.get_parameters(), "scrfd_top1x"), (127.5, 128.0))
ov.save_model(detx, out / f"scrfd_top1x_u8{SUFFIX}.xml")

# ---- 2. landmarks -> similarity transform -> warp -> AdaFace, on the square crop ----
lmk_model, outputs = scrfd(CROP)
crop_in = ops.parameter([1, 3, CROP, CROP], Type.f32, name="crop")              # BGR 0..255 (RGB if SWAP)
crop = ops.gather(crop_in, i64([2, 1, 0]), i64(1)) if SWAP else crop_in          # always BGR from here on
rgb = ops.gather(crop, i64([2, 1, 0]), i64(1))
scrfd_in = ops.divide(ops.subtract(rgb, f32(127.5)), f32(128.0))
for target in list(lmk_model.get_parameters()[0].output(0).get_target_inputs()):
    target.replace_source_output(scrfd_in.output(0))
scores_all, points_all = [], []
for level, stride in enumerate(STRIDES):
    c = centers(CROP, stride)
    scores_all.append(ops.reshape(outputs[SCORES[level]], i64([-1]), False))
    points_all.append(ops.add(f32(np.tile(c, 5)), ops.multiply(outputs[POINTS[level]], f32(stride))))
top = ops.topk(ops.concat(scores_all, 0), i64(1), 0, "max", "value")
src = ops.reshape(ops.gather(ops.concat(points_all, 0), top.output(1), i64(0)), i64([5, 2]), False)

# least-squares similarity src -> template: [x', y'] = [[a, -b], [b, a]] [x, y] + t
src_mean = ops.reduce_mean(src, i64([0]), True)                                  # [1, 2]
s = ops.subtract(src, src_mean)
sx, sy = ops.split(s, i64(1), 2).outputs()                                       # [5, 1] each
d = TEMPLATE - TEMPLATE.mean(0, keepdims=True)
dx, dy = f32(d[:, :1]), f32(d[:, 1:])
norm = ops.reduce_sum(ops.add(ops.multiply(sx, sx), ops.multiply(sy, sy)), i64([0, 1]), False)
a = ops.divide(ops.reduce_sum(ops.add(ops.multiply(sx, dx), ops.multiply(sy, dy)), i64([0, 1]), False), norm)
b = ops.divide(ops.reduce_sum(ops.subtract(ops.multiply(sx, dy), ops.multiply(sy, dx)), i64([0, 1]), False), norm)
mx, my = ops.split(src_mean, i64(1), 2).outputs()
tx = ops.subtract(f32(TEMPLATE.mean(0)[0]), ops.subtract(ops.multiply(a, mx), ops.multiply(b, my)))
ty = ops.subtract(f32(TEMPLATE.mean(0)[1]), ops.add(ops.multiply(b, mx), ops.multiply(a, my)))

# inverse map for every chip pixel: source = R^-1 (p - t), then GridSample coordinates
gy, gx = np.mgrid[0:CHIP, 0:CHIP].astype(np.float32)
qx = ops.subtract(f32(gx.reshape(1, CHIP, CHIP, 1)), tx)
qy = ops.subtract(f32(gy.reshape(1, CHIP, CHIP, 1)), ty)
k = ops.add(ops.multiply(a, a), ops.multiply(b, b))
src_x = ops.divide(ops.add(ops.multiply(a, qx), ops.multiply(b, qy)), k)
src_y = ops.divide(ops.subtract(ops.multiply(a, qy), ops.multiply(b, qx)), k)
to_grid = lambda value: ops.subtract(ops.divide(ops.add(ops.multiply(value, f32(2.0)), f32(1.0)), f32(CROP)), f32(1.0))
grid = ops.concat([to_grid(src_x), to_grid(src_y)], 3)                           # [1, 112, 112, 2]
chip = ops9.grid_sample(crop, grid, {"align_corners": False, "mode": "bilinear", "padding_mode": "zeros"})

ada = core.read_model(models / "face/openvino/adaface_ir101_int8.xml")
ada_in = ops.divide(ops.subtract(chip, f32(127.5)), f32(127.5))                  # (x/255 - 0.5) / 0.5, BGR
for target in list(ada.get_parameters()[0].output(0).get_target_inputs()):
    target.replace_source_output(ada_in.output(0))
feat = ada.outputs[0].get_node().input_value(0)
feat_result = ops.result(feat)
feat_result.output(0).set_names({"feat"})
fused = ov.Model([feat_result], [crop_in], "face_align")
# compile the float model for the check before the u8 step rewires the input
fused_f32 = core.compile_model(fused.clone(), "CPU")
ov.save_model(u8_input(fused), out / f"face_align_u8{SUFFIX}.xml")

# ---- self-check: the same steps as openvino_face.py, in numpy + OpenCV ----
rng = np.random.default_rng(3)
image = cv2.GaussianBlur(rng.integers(0, 255, (CROP, CROP, 3), dtype=np.uint8), (0, 0), 3)
raw_scrfd = core.compile_model(scrfd(CROP)[0], "CPU")
x = ((image[:, :, ::-1].astype(np.float32) - 127.5) / 128.0).transpose(2, 0, 1)[None]
r = raw_scrfd([x])
all_scores, all_points = [], []
for level, stride in enumerate(STRIDES):
    all_scores.append(np.asarray(r[SCORES[level]]).reshape(-1))
    all_points.append(np.tile(centers(CROP, stride), 5) + np.asarray(r[POINTS[level]]).reshape(-1, 10) * stride)
landmarks = np.vstack(all_points)[int(np.concatenate(all_scores).argmax())].reshape(5, 2)
# least squares similarity (openvino_face.py uses LMEDS; with 5 clean points they agree closely,
# and the real-data check against openvino_face.py itself is face_check.sh)
sc, dc = landmarks - landmarks.mean(0), TEMPLATE - TEMPLATE.mean(0)
la = float((sc * dc).sum() / (sc ** 2).sum())
lb = float((sc[:, 0] * dc[:, 1] - sc[:, 1] * dc[:, 0]).sum() / (sc ** 2).sum())
rot = np.array([[la, -lb], [lb, la]], np.float32)
matrix = np.hstack([rot, (TEMPLATE.mean(0) - rot @ landmarks.mean(0))[:, None]])
ref_chip = cv2.warpAffine(image.astype(np.float32), matrix, (CHIP, CHIP), flags=cv2.INTER_LINEAR, borderValue=0.0)
raw_ada = core.compile_model(core.read_model(models / "face/openvino/adaface_ir101_int8.xml"), "CPU")
ref = np.asarray(raw_ada([((ref_chip - 127.5) / 127.5).transpose(2, 0, 1)[None]])[0]).reshape(-1)
fed = image[:, :, ::-1] if SWAP else image                                      # what DL Streamer would hand over
got = np.asarray(fused_f32([fed.astype(np.float32).transpose(2, 0, 1)[None]])[0]).reshape(-1)
cosine = float(ref @ got / (np.linalg.norm(ref) * np.linalg.norm(got)))
print(f"face_align_u8{SUFFIX}.xml (crop {CROP}, expand {EXPAND}): embedding cosine vs numpy/OpenCV reference {cosine:.4f} ->",
      "OK" if cosine > 0.98 else "MISMATCH")
print("scrfd_top1x_u8.xml output", list(detx.output(0).get_partial_shape()))
