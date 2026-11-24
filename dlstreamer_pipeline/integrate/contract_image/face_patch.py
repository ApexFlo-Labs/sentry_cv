#!/usr/bin/env python3
"""Face changes from the face-pipeline session (~/Documents/dls-face-trace-2026-11-20), user decision 2026-11-24:
both in the image, no switch. Applied to COPIES in the build context after t2_parity_patch.py; every replacement must
match exactly (count given), or the build stops.

    face_patch.py <context>/home/chandana_optimization/integrate

  1. Pose model WHENet FP16 (models/whenet_fp16.xml, WHENET.md) instead of SixDRepNet A0 (best_a0_fp16):
     - engine, C pose step (cgates_attach_pose_direct): face box widened x +-20 %, y +-10 %, stretched to 224x224
       (bilinear as cv2.resize), RGB 0..255, output yaw/roll/pitch in degrees; same limits |pitch|<=25, |yaw|<=35,
       |roll|<=25, still before AdaFace;
     - camera process, her quality check (DlsFaceExtractor): the same model and input (face_gate.WhenetPose).
  2. Clear-face gate (face_gate.ClearFaceGate) on the face the best-face window chose, before it is sent: mirror
     consistency (AdaFace on the aligned face and its mirror, one shared AdaFace copy in the model server) and the face
     detector's confidence, percentile-ranked against the camera's recent chosen faces, top 60 % kept.
"""
import sys
from pathlib import Path


def patch(path, edits):
    text = path.read_text(encoding="utf-8")
    for old, new, *count in edits:
        n, want = text.count(old), (count[0] if count else 1)
        if n != want:
            sys.exit(f"PATCH FAILED {path.name}: expected {want} match(es), found {n}:\n{old}")
        text = text.replace(old, new)
    path.write_text(text, encoding="utf-8")
    print(f"patched {path} ({len(edits)} edits)")


root = Path(sys.argv[1])

# 1. WHENet: model path (paths["pose"], engine pose step at start and for a live-added branch)
patch(root / "integrate9.py", [
    ('"best_a0_fp16.xml"', '"whenet_fp16.xml"', 3),
    # 2. one shared AdaFace copy in the model server for the clear-face gate (same model file as the C face step)
    ('    ms_paths = {"body": paths["body"], "gait": paths["gait"], "scene": paths["scene"], ',
     '    ms_paths = {"body": paths["body"], "gait": paths["gait"], "scene": paths["scene"], "face": ada.decode(), '),
])
patch(root / "model_server.py", [
    ('KINDS = ("body", "scene", "gait")\n',
     'KINDS = ("body", "scene", "gait", "face")  # face: AdaFace for the clear-face gate (face_gate.py)\n'),
])

# 1. WHENet in the C pose step
patch(root / "gatesc" / "cgates.c", [
    ("static int infer_pose(DirectCtx *c, float *input, double *m) {\n",
     "/* WHENet input (face_patch.py): face box widened x +-20 %, y +-10 % (clamped, truncated as the reference's int()),\n"
     "   NV12 -> RGB, stretched to 224x224 with cv2.resize INTER_LINEAR semantics, RGB 0..255 CHW float. */\n"
     "static int whenet_tensor(const unsigned char *Y, int ys, const unsigned char *UV, int uvs, gint W, gint H, const Box *f, float *t) {\n"
     "    double w = f->x2 - f->x1, h = f->y2 - f->y1;\n"
     "    double fx1 = f->x1 - 0.2 * w, fx2 = f->x2 + 0.2 * w, fy1 = f->y1 - 0.1 * h, fy2 = f->y2 + 0.1 * h;\n"
     "    if (fx1 < 0) fx1 = 0;\n"
     "    if (fy1 < 0) fy1 = 0;\n"
     "    if (fx2 > W) fx2 = W;\n"
     "    if (fy2 > H) fy2 = H;\n"
     "    int left = (int)fx1, top = (int)fy1, right = (int)fx2, bottom = (int)fy2;\n"
     "    int cw = right - left, ch = bottom - top;\n"
     "    if (cw <= 0 || ch <= 0) return 0;\n"
     "    unsigned char *rgb = malloc((size_t)cw * ch * 3);\n"
     "    nv12_to_rgb(Y, ys, UV, uvs, left, top, right, bottom, rgb);\n"
     "    double sx = (double)cw / 224.0, sy = (double)ch / 224.0;\n"
     "    for (int y = 0; y < 224; y++) {\n"
     "        double fy = (y + 0.5) * sy - 0.5; int y0 = (int)floor(fy); double wy = fy - y0;\n"
     "        if (y0 < 0) { y0 = 0; wy = 0; }\n"
     "        int y1 = y0 + 1 < ch ? y0 + 1 : ch - 1;\n"
     "        if (y0 > ch - 1) y0 = ch - 1;\n"
     "        for (int x = 0; x < 224; x++) {\n"
     "            double fx = (x + 0.5) * sx - 0.5; int x0 = (int)floor(fx); double wx = fx - x0;\n"
     "            if (x0 < 0) { x0 = 0; wx = 0; }\n"
     "            int x1 = x0 + 1 < cw ? x0 + 1 : cw - 1;\n"
     "            if (x0 > cw - 1) x0 = cw - 1;\n"
     "            for (int k = 0; k < 3; k++) {\n"
     "                double v = (1 - wy) * ((1 - wx) * rgb[(y0 * cw + x0) * 3 + k] + wx * rgb[(y0 * cw + x1) * 3 + k])\n"
     "                         + wy * ((1 - wx) * rgb[(y1 * cw + x0) * 3 + k] + wx * rgb[(y1 * cw + x1) * 3 + k]);\n"
     "                t[k * 224 * 224 + y * 224 + x] = (float)rint(v);  /* uint8 result, as cv2.resize on uint8 */\n"
     "            }\n"
     "        }\n"
     "    }\n"
     "    free(rgb);\n"
     "    return 1;\n"
     "}\n"
     "\n"
     "/* WHENet output [1,3] = yaw, roll, pitch (degrees) */\n"
     "static int whenet_angles(const double *m, double *pitch, double *yaw, double *roll) {\n"
     "    *yaw = m[0]; *roll = m[1]; *pitch = m[2];\n"
     "    return isfinite(*yaw) && isfinite(*roll) && isfinite(*pitch);\n"
     "}\n"
     "\n"
     "static int infer_pose(DirectCtx *c, float *input, double *m) {\n"),
    ("    if (ok) for (int k = 0; k < 9; k++) m[k] = ((float *)data)[k];\n",
     "    if (ok) for (int k = 0; k < 3; k++) m[k] = ((float *)data)[k];  /* WHENet: yaw, roll, pitch */\n"),
    ("                    int prepared = main_pose_tensor(py, sy, puv, suv, c->W, c->H, &b[i], tensor);\n",
     "                    int prepared = whenet_tensor(py, sy, puv, suv, c->W, c->H, &b[i], tensor);\n"),
    ("                    } else if (!rotation_to_euler(m, &pitch, &yaw, &roll)) {\n",
     "                    } else if (!whenet_angles(m, &pitch, &yaw, &roll)) {\n"),
])

# 1. WHENet in her camera-process quality check (same model, same input)
patch(root / "v8code" / "pipeline" / "dlstreamer_bridge.py", [
    ("        self._pose = OpenVINOHeadPoseEstimator(pose_model, telemetry=telemetry)\n",
     "        from face_gate import WhenetPose  # face_patch.py: WHENet FP16 replaces SixDRepNet A0\n"
     "        self._pose = WhenetPose(pose_model, telemetry=telemetry)\n"),
    ("            pose_crop = self._crop(frame, candidate.bbox, self.pose_context)\n",
     "            from face_gate import whenet_crop\n"
     "            pose_crop = whenet_crop(frame, candidate.bbox)  # WHENet input: x +-20 %, y +-10 %\n"),
])

# 2. clear-face gate: the chosen face's box is kept with the candidate; the gate decides at the end of the window
patch(root / "v8code" / "pipeline" / "v8_events.py", [
    ('            "vector": np.asarray(vector, np.float32).copy(), "image": crop.copy(),\n',
     '            "vector": np.asarray(vector, np.float32).copy(), "image": crop.copy(),\n'
     '            "face_box": tuple(int(round(v)) for v in box),  # face_patch.py: clear-face gate looks up its landmarks\n'),
    ('            if now < candidate["deadline"]:\n'
     '                continue\n',
     '            if now < candidate["deadline"]:\n'
     '                continue\n'
     '            gate = getattr(self, "_face_gate", None)  # face_patch.py: clear-face gate on the chosen face\n'
     '            if gate is not None and not gate.keep(candidate):\n'
     '                self.face_candidates.pop(presence_id, None)\n'
     '                if self.telemetry is not None:\n'
     '                    self.telemetry.count("face_gate_dropped")\n'
     '                continue\n'),
])
patch(root / "event_worker_shm.py", [
    # landmarks, detector confidence and person box of each face, by its box (the gate needs them for the chosen face)
    ('            faces = [({"detection": {"confidence": f["conf"]}, "tensors": [{"data": f["emb"]}]}, (f["x"], f["y"], f["w"], f["h"])) for f in cfaces]\n',
     '            faces = [({"detection": {"confidence": f["conf"]}, "tensors": [{"data": f["emb"]}], "lm": f.get("lm")}, (f["x"], f["y"], f["w"], f["h"])) for f in cfaces]\n'),
    ("            owner = min(owners, key=lambda p: p[3] * p[4])\n"
     "            candidates.append(DlsFaceCandidate(",
     "            owner = min(owners, key=lambda p: p[3] * p[4])\n"
     "            face_side[(x, y, x + w, y + h)] = (o.get(\"lm\"), float(o[\"detection\"][\"confidence\"]),\n"
     "                                               (owner[1], owner[2], owner[1] + owner[3], owner[2] + owner[4]))\n"
     "            while len(face_side) > 512:\n"
     "                face_side.popitem(last=False)\n"
     "            candidates.append(DlsFaceCandidate("),
    ("    v8._gait_store = v8.gait  # T2 parity C: gait cleanup also on frames where her schedule sets v8.gait = None\n",
     "    v8._gait_store = v8.gait  # T2 parity C: gait cleanup also on frames where her schedule sets v8.gait = None\n"
     "    face_side = collections.OrderedDict()  # face_patch.py: face box -> (landmarks, detector confidence, person box)\n"
     "    if face_extractor is not None and paths.get(\"model_server\") is not None:  # face_patch.py: clear-face gate\n"
     "        from face_gate import ClearFaceGate\n"
     "        from model_server import Remote\n"
     "        _ada = Remote(\"face\", name, *paths[\"model_server\"])\n"
     "        _ada.rid = 1_000_000_000  # own request ids next to body/scene/gait on the same reply channel\n"
     "        v8._face_gate = ClearFaceGate(_ada, face_side)\n"
     "        print(f\"{time.strftime('%T')} {name}: clear-face gate on (mirror + detector confidence, top 60 % per camera)\", flush=True)\n"),
])
