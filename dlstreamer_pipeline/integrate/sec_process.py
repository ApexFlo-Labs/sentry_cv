"""The secondary branch (plates + OCR) in its OWN process (test switch SEC_PROCESS=1 in integrate9.py).

Same code as before: sec_shared.py's SecondaryPipeline (Sunil's edge_app/secondary.py) with our plate/body gates
(gates_shared.gate_secondary), body/gait/scene off (those come from the shared model server), 1 frame/s in, 7 of 10
used. Only the hosting changes: the engine's C probes (csec_*) put the 640x360 frame + its boxes into shared memory,
and this process reads them and sends the plates to each camera's V8 rules process, as forward_plates did.
Not done here: the frozen-picture check (freeze_shared) of the standalone Option 6 pipeline.
"""
import glob
import os
import sys
import time

SB_MAX, SB_BYTES, HDR = 64, 48, 64


def run(names, shm_names, inboxes, group_app, slots, slot_bytes, ingest_config):
    if os.getenv("RULES_CPUS"):
        a, b = os.getenv("RULES_CPUS").split("-")
        os.sched_setaffinity(0, set(range(int(a), int(b) + 1)))
    os.environ["INGEST_CONFIG"] = ingest_config
    os.environ["INGEST_RESULTS"] = os.devnull
    sys.path.insert(0, group_app)
    import numpy as np
    from multiprocessing import shared_memory
    import sec_shared
    import gates_shared
    import json
    sec_shared.CONFIG.update(body_reid_enabled=False, gait_enabled=False, scene_enabled=False)
    from edge_app.pipeline import DetectionResult
    from edge_app.secondary import SecondaryPipeline
    cams = json.load(open(ingest_config))["cameras"]
    scale = {f"camera-{i + 1:02d}": 640.0 / c["width"] for i, c in enumerate(cams)}
    pipe = SecondaryPipeline(sec_shared.CONFIG, consume=lambda payload: None)
    sec_shared._pipeline = pipe
    gates_shared.gate_secondary(pipe, lambda camera_id: scale.get(camera_id, 1.0))
    mems = [shared_memory.SharedMemory(name=n) for n in shm_names]
    last = [0] * len(names)
    used = [0] * len(names)
    print(f"{time.strftime('%T')} secondary process ready (pid {os.getpid()}), {len(names)} cameras", flush=True)
    while True:
        did = False
        for k, mem in enumerate(mems):
            buf = mem.buf
            seq = int(np.ndarray((1,), dtype=np.int64, buffer=buf)[0])
            if seq <= last[k]:
                continue
            last[k] = seq
            off = HDR + (seq % slots) * slot_bytes
            h = np.ndarray((6,), dtype=np.int64, buffer=buf, offset=off)
            if int(h[0]) != seq:
                continue
            w, hh, nbox, stride = int(h[2]), int(h[3]), int(h[4]), int(h[5])
            raw = bytes(buf[off + HDR:off + HDR + nbox * SB_BYTES])
            pix = np.ndarray((hh, stride // 4, 4), dtype=np.uint8, buffer=buf, offset=off + HDR + SB_MAX * SB_BYTES)[:, :w]
            bgr = np.ascontiguousarray(pix[:, :, :3])
            if int(np.ndarray((1,), dtype=np.int64, buffer=buf, offset=off)[0]) != seq:
                continue  # overwritten while copying
            did = True
            used[k] += 1
            if used[k] % 10 >= 7:  # 1 frame/s in, 0.7 frame/s used (as sec_shared.Secondary)
                continue
            detections = []
            for i in range(nbox):
                rec = raw[i * SB_BYTES:(i + 1) * SB_BYTES]
                label = rec[:16].split(b"\0", 1)[0].decode("utf-8", "replace")
                conf, x, y, bw, bh = np.frombuffer(rec[16:36], dtype=np.float32)
                detections.append({"label": label, "confidence": float(conf),
                                   "bbox": [int(x * w), int(y * hh), int((x + bw) * w), int((y + bh) * hh)]})
            if not detections:
                sec_shared._count["empty"] += 1
            result = DetectionResult(camera_id=f"camera-{k + 1:02d}", timestamp=time.time(), width=w, height=hh,
                                     detections=detections, frame=bgr, pixel_format="BGR")
            started = time.perf_counter()
            payload = pipe._process(result)
            c = sec_shared._count
            c["ms"] += (time.perf_counter() - started) * 1000
            c["frames"] += 1
            c["cams"].add(str(k))
            c["plates"] += len(payload["plates"])
            c["anpr"] += sum(1 for p in payload["plates"] if p["text"])
            sec_shared._report()
            plates = payload.get("plates") or []
            if plates:
                fw, fh = float(payload["frame"]["width"]), float(payload["frame"]["height"])
                norm = lambda b: (b[0] / fw, b[1] / fh, b[2] / fw, b[3] / fh)
                items = [(norm(p["vehicle_bbox"]), norm(p["bbox"]), p.get("text") or "", float(p.get("ocr_confidence") or 0.0),
                          float(p.get("detection_confidence") or 0.0), f"sec-{k}-{time.monotonic_ns()}-{i}") for i, p in enumerate(plates)]
                try:
                    inboxes[k].put_nowait(("plates", items))
                except Exception:
                    pass
        if not did:
            time.sleep(0.005)
