#!/usr/bin/env python3
"""ANPR like the face path (user decision 2026-11-25): plate detector + OCR as one C step on crops of the ORIGINAL NV12
frame (cplate.c, appended to the COPY of cgates.c), instead of DL Streamer's gvadetect + gvaclassify with opencv
preparation. Her plate selection (pv: vehicles >= 80 px, every 5th frame, change gate, confirmed skip) and her
confirmation rule are kept; the C step reads her helper boxes and confirms on the same numbers. Plates reach the
camera process as "cplates" in the frame JSON (same fields her inline_plates reads). PLATE_C=1 turns it on (image ENV).
Applied after orig_pixels_patch.py; every replacement must match exactly once, or the build stops.

    anpr_patch.py <context>/home/chandana_optimization/integrate
"""
import sys
from pathlib import Path


def patch(path, edits):
    text = path.read_text(encoding="utf-8")
    for old, new in edits:
        n = text.count(old)
        if n != 1:
            sys.exit(f"PATCH FAILED {path.name}: expected 1 match, found {n}:\n{old}")
        text = text.replace(old, new)
    path.write_text(text, encoding="utf-8")
    print(f"patched {path} ({len(edits)} edits)")


root = Path(sys.argv[1])
cg = root / "gatesc" / "cgates.c"
cg.write_text(cg.read_text(encoding="utf-8") + (Path(__file__).resolve().parent / "cplate.c").read_text(encoding="utf-8"),
              encoding="utf-8")
print(f"appended cplate.c to {cg}")

patch(root / "integrate9.py", [
    ('PLATE_INLINE = os.getenv("PLATE_INLINE") == "1"\n',
     'PLATE_INLINE = os.getenv("PLATE_INLINE") == "1"\n'
     'PLATE_C = PLATE_INLINE and os.getenv("PLATE_C") == "1"  # anpr_patch.py: plate + OCR as a C step on NV12 crops (cplate.c)\n'),
    # graph: the plate gvadetect + OCR gvaclassify become one marker element where the C step runs (pi<n>)
    ('               + (("identity name=pr@N@ silent=true ! " + Q + " ! ") if os.getenv("PLATE_STATE") == "1" and PLATE_SELECT else "")) if PLATE_INLINE else ""\n',
     '               + (("identity name=pr@N@ silent=true ! " + Q + " ! ") if os.getenv("PLATE_STATE") == "1" and PLATE_SELECT else "")) if PLATE_INLINE else ""\n'
     'if PLATE_C:  # anpr_patch.py: selection (pv) -> C plate + OCR step (pi) -> confirmation probe (pr, now a no-op: the C step confirms)\n'
     '    PLATE_ELEMS = (("identity name=pv@N@ silent=true ! " + Q + " ! ") if PLATE_SELECT else "") + "identity name=pi@N@ silent=true ! " + Q + " ! " \\\n'
     '                  + (("identity name=pr@N@ silent=true ! " + Q + " ! ") if os.getenv("PLATE_STATE") == "1" and PLATE_SELECT else "")\n'),
    # engine start: attach the C step per camera (after her selection/confirmation steps)
    ("                cg.cplate_attach_report(ctypes.c_void_p(sel), ctypes.c_void_p(hash(pipeline.get_by_name(f\"pr{c['n']}\").get_static_pad(\"src\"))), stable_s, confirm)\n",
     "                cg.cplate_attach_report(ctypes.c_void_p(sel), ctypes.c_void_p(hash(pipeline.get_by_name(f\"pr{c['n']}\").get_static_pad(\"src\"))), stable_s, confirm)\n"
     "            if PLATE_C:  # anpr_patch.py\n"
     "                cg.cplate_infer_new.restype = ctypes.c_void_p\n"
     "                cg.cplate_infer_new.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_char_p, ctypes.c_char_p]\n"
     "                cg.cplate_infer_attach.argtypes = [ctypes.c_void_p] * 3\n"
     "                pc = cg.cplate_infer_new(ctypes.c_void_p(sel), f\"{SEC}/traffic-v11/openvino/license_plate.xml\".encode(),\n"
     "                                         f\"{SEC}/traffic-v11/openvino/ocr.xml\".encode(), PLATE_DEV.encode())\n"
     "                if not pc:\n"
     "                    log(f\"FATAL plate/OCR models failed to load on {PLATE_DEV}\")\n"
     "                    sys.exit(4)\n"
     "                c[\"cplate\"] = pc\n"
     "                cg.cplate_infer_attach(ctypes.c_void_p(pc), ctypes.c_void_p(hash(pipeline.get_by_name(f\"pi{c['n']}\").get_static_pad(\"src\"))),\n"
     "                                       ctypes.c_void_p(hash(pipeline.get_by_name(f\"mc{c['n']}\").get_static_pad(\"src\"))))\n"),
    # live add: the same C step on a branch built while running
    ("                cg.cplate_attach_report(ctypes.c_void_p(sel), src(\"pr\"), stable_s, confirm)\n",
     "                cg.cplate_attach_report(ctypes.c_void_p(sel), src(\"pr\"), stable_s, confirm)\n"
     "            if PLATE_C:  # anpr_patch.py\n"
     "                pc = cg.cplate_infer_new(ctypes.c_void_p(sel), f\"{SEC}/traffic-v11/openvino/license_plate.xml\".encode(),\n"
     "                                         f\"{SEC}/traffic-v11/openvino/ocr.xml\".encode(), PLATE_DEV.encode())\n"
     "                if not pc:\n"
     "                    raise RuntimeError(\"plate/OCR C step not attached\")\n"
     "                c[\"cplate\"] = pc\n"
     "                cg.cplate_infer_attach(ctypes.c_void_p(pc), src(\"pi\"), src(\"mc\"))\n"),
])

patch(root / "event_worker_shm.py", [
    ('                    detections, candidates = results_from_json(msg.get("objects", []), msg.get("cfaces", []) if cface_mode else None)\n',
     '                    detections, candidates = results_from_json(msg.get("objects", []), msg.get("cfaces", []) if cface_mode else None)\n'
     '                    for _p in msg.get("cplates") or []:  # anpr_patch.py: plates of the C step (same fields as license_plate objects)\n'
     '                        plates_seen.append((int(_p["x"]), int(_p["y"]), int(_p["w"]), int(_p["h"]), float(_p["conf"]), _p["ocr"]))\n'),
])
