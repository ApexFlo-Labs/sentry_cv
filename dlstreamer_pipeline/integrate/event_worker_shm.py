"""Event process for integrate5.py: as event_worker_cpu.py (Kiranmai's V8 event code unchanged, 2-thread setting), but the
engine no longer sends frames over a queue: C pad probes (cgates.c creader) write the main JSON, the NV12 frames and the
fire JSON into this camera's shared memory (layout LAYOUT, see creader in cgates.c), and this process reads them and
builds her detections and face candidates itself (the code that was the engine's results_from_json). The inbox queue
now carries only ("status", state, reason) and ("stop",).
"""
import os
import sys
import time
import types
import collections

STATS = ["frames", "fire", "pixels", "geometry_only", "overwritten", "skipped", "face_frames", "errors",
         "ms_wait_pixels", "ms_tracker", "ms_faces", "ms_events", "ms_fire", "busy_ms",
         "ms_parse", "missed_main", "missed_fire", "no_pixels", "steady_at", "plates_in", "plates_used", "plates_unmatched", "identity_frames", "pix_requests", "lazy_frames", "lazy_fetch", "lazy_miss", "jpeg_direct", "jpeg_decoded", "cpu_encoded", "lazy_stale", "region_decoded", "region_checked", "region_mismatch", "lazy_copy", "copy_miss", "plates_exact", "plates_fallback", "plates_bytrack", "plates_inline", "plates_gated", "plates_noparent"]
LAYOUT = {"ps": 6, "pb": 3840 * 2160 * 3 // 2, "js": 16, "jb": 512 * 1024, "fs": 8, "fb": 64 * 1024}
VEHICLE_CLASS_NAMES = {0: "bus", 1: "car", 2: "minivan", 3: "motorcycle", 4: "rickshaw", 5: "truck", 6: "bicycle", 7: "pedestrian"}


def shm_bytes(L=LAYOUT):
    return 1024 + L["ps"] * (64 + L["pb"]) + L["js"] * (64 + L["jb"]) + L["fs"] * (64 + L["fb"])
SLOTS = 6


def frame_bytes(width, height):
    return width * height * 3 // 2


def run(name, config, deployment_id, handoff_conn, inbox, stats, paths, face_interval, shm_name):
    if os.getenv("RULES_CPUS"):  # TEST: keep this process off the P-cores (0-5) so the engine's detector threads keep them
        a, b = os.getenv("RULES_CPUS").split("-")
        os.sched_setaffinity(0, set(range(int(a), int(b) + 1)))
    here = paths["here"]
    sys.path.insert(0, os.path.join(here, "v8code"))
    opts = {o.strip() for o in os.getenv("CPU_OPT", "").split(",") if o.strip()}
    n_threads = int(os.getenv("CPU_OPT_THREADS", "1"))
    if "threads" in opts:  # before openvino is imported in this process
        for key in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "OPENCV_FOR_THREADS_NUM", "TBB_NUM_THREADS"):
            os.environ[key] = str(n_threads)
    try:
        import requests  # noqa: F401
    except ImportError:
        sys.modules["requests"] = types.ModuleType("requests")
    import cv2
    import numpy as np
    from multiprocessing import shared_memory
    from pipeline.types import FramePacket
    from pipeline.dlstreamer_bridge import DlsFaceExtractor, GvaTrackerMetadataStage
    from pipeline.face_samples import FaceSamplePipeline
    from pipeline.person_reid import OpenVINOBodyExtractor, OpenVINOGaitExtractor
    from pipeline.scene_embeddings import OpenVINOSceneExtractor
    from pipeline.sentinel_v8_handoff import V8HandoffClient
    from pipeline.v8_events import V8EventPipeline

    if "threads" in opts:
        cv2.setNumThreads(n_threads)
    if "policy" in opts:
        from pipeline import adaptive_policy as ap
        cls = ap.AdaptivePolicyResolver
        nested = [(m, sec, k) for m, secs in cls._PROFILES.items() for sec, d in (secs or {}).items()
                  for k, v in (d or {}).items() if isinstance(v, (dict, list, set))]
        if nested:
            raise RuntimeError(f"policy patch not safe, nested values: {nested[:3]}")

        def section(self, name, object_type=None):  # same as hers, minus deepcopy of flat values
            mode = self._modes.get(name, "balanced")
            if name in self._OBJECT_SECTIONS and object_type in {"person", "vehicle"}:
                mode = self._object_modes[name][str(object_type)]
            source = self._PROFILES[mode].get(name)
            if source is None and name == "tracking":
                return {"mode": mode, **self._tracker_effective}
            return {"mode": mode, **(source or {})}
        cls.section = section
    if "metrics" in opts:
        from pipeline import face_metrics as fm
        original_save = fm.FaceMetrics._save_locked

        def save_at_most_every_2s(self):
            now = time.monotonic()
            if now - getattr(self, "_last_saved", -1e9) >= 2.0:
                self._last_saved = now
                original_save(self)
        fm.FaceMetrics._save_locked = save_at_most_every_2s
    print(f"{time.strftime('%T')} {name}: CPU_OPT={sorted(opts) or 'none'} threads={n_threads if 'threads' in opts else 'default'}", flush=True)

    gait_scale = float(os.getenv("T_GAIT_SCALE", "1"))
    if gait_scale < 1.0:  # TEST ONLY (measurement for Kiranmai): gait MOG2 background on a downscaled frame, mask scaled back
        from pipeline import person_reid as _pr
        _orig_background = _pr.create_gait_background

        class _ScaledBackground:
            def __init__(self, inner):
                self.inner = inner

            def apply(self, frame, *args, **kwargs):
                h, w = frame.shape[:2]
                small = cv2.resize(frame, (max(1, int(w * gait_scale)), max(1, int(h * gait_scale))), interpolation=cv2.INTER_AREA)
                mask = self.inner.apply(small, *args, **kwargs)
                return cv2.resize(mask, (w, h), interpolation=cv2.INTER_NEAREST)

            def __getattr__(self, attr):
                return getattr(self.inner, attr)
        _pr.create_gait_background = lambda: _ScaledBackground(_orig_background())
        print(f"{time.strftime('%T')} {name}: TEST gait background at {gait_scale:g} scale", flush=True)
    cface_mode = os.getenv("CFACES") == "1"  # Option 7 engine: faces + embeddings come in the JSON "cfaces" field
    body_refresh = os.getenv("POLICY_BODY_REFRESH_S")
    if body_refresh:  # PRODUCTION POLICY (Kiranmai: policy may change, contract not): body vector refresh for existing tracks
        import inspect, textwrap
        from pipeline import v8_events as _ve
        src = textwrap.dedent(inspect.getsource(_ve.V8EventPipeline._body_vectors))
        old_due = '.get("last_body_at", 0) >= 1]'
        if src.count(old_due) != 1:
            raise RuntimeError("body refresh policy: her _body_vectors changed, patch not applied")
        ns = dict(_ve.__dict__, _BODY_REFRESH_S=float(body_refresh))
        exec(src.replace(old_due, '.get("last_body_at", 0) >= _BODY_REFRESH_S]'), ns)
        _ve.V8EventPipeline._body_vectors = ns["_body_vectors"]
        print(f"{time.strftime('%T')} {name}: POLICY body vector refresh every {float(body_refresh):g} s (her default 1 s; new tracks at once)", flush=True)
    ours_policy = os.getenv("POLICY_OURS") == "1"
    if ours_policy:  # PRODUCTION POLICY = our secondary branch's settings (sec_shared.CONFIG + gates_shared); contract unchanged
        from pipeline import adaptive_policy as _ap
        for _mode, _secs in _ap.AdaptivePolicyResolver._PROFILES.items():
            _secs["anpr"].update(confirm_min_reads=1, ocr_emit_min_confidence=0.40, min_plate_width_px=16.0,
                                 min_plate_height_px=5.0, min_sharpness=0.0)
            _secs["identity"].update(body_min_width_px=32.0, body_min_height_px=64.0, body_quality_floor=0.2)
        print(f"{time.strftime('%T')} {name}: POLICY ours: plates 1 read, OCR >= 0.40, plate >= 16x5 px, no sharpness floor; "
              f"body >= 32x64 px quality >= 0.2; identity frames 0.7/s: body every 3rd, gait every one, scene every 7th", flush=True)
    if os.getenv("V8_NO_HANDOFF") == "1":  # TEST ONLY: packages are built but not sent (upper bound without delivery)
        V8HandoffClient.persist = lambda self, *a, **k: True
        print(f"{time.strftime('%T')} {name}: TEST no handoff (packages built, not delivered)", flush=True)
    v8conf = config["analytics"]["v8_events"]["v8"]
    profiles = {p for b in v8conf["bindings"] for p in b.get("embedding_profile_ids") or []}
    fps = int(round(config["processing"]["fps"]))
    if os.getenv("V8_FORMAT_ONLY") == "1":  # TEST: V8 input (desired_state) + event rules + package output only; V8's own body/gait/scene models off
        profiles = profiles - {"transreid-ssl-v18.1", "siglip2-base-v1", "gaitbase-v18.1"}
        print(f"{time.strftime('%T')} {name}: V8_FORMAT_ONLY: body, gait, scene models off; profiles {sorted(profiles)}", flush=True)
    if paths.get("model_server") is not None:  # ONE shared copy of our models answers her classes' model calls (model_server.py)
        from model_server import make_extractors
        requests_q, response_q = paths["model_server"]
        body, scene, gait = make_extractors(name, requests_q, response_q, paths["model_info"])
        body = body if "transreid-ssl-v18.1" in profiles else None
        scene = scene if "siglip2-base-v1" in profiles else None
        gait = gait if "gaitbase-v18.1" in profiles else None
        print(f"{time.strftime('%T')} {name}: body/scene/gait via shared model server (no model copy in this process)", flush=True)
    else:
        body = OpenVINOBodyExtractor(paths["body"], os.getenv("BODY_DEVICE", "NPU")) if "transreid-ssl-v18.1" in profiles else None
        scene = OpenVINOSceneExtractor(paths["scene"], device=os.getenv("SCENE_DEVICE", "NPU")) if "siglip2-base-v1" in profiles else None
        gait = OpenVINOGaitExtractor(paths["gait"], gait_device=os.getenv("GAIT_DEVICE", "NPU")) if "gaitbase-v18.1" in profiles else None
    for model in (body, scene, gait):
        if model is not None and hasattr(model, "warmup") and paths.get("model_server") is None:
            model.warmup()
    face_extractor = DlsFaceExtractor(paths["pose"]) if "adaface-ir101-v18.1" in profiles else None
    face_pipeline = None
    if face_extractor is not None:
        face_extractor.warmup()
        face_pipeline = FaceSamplePipeline(name, deployment_id, face_extractor, config, enable_legacy_delivery=False)
    tracker = GvaTrackerMetadataStage(fps)
    pkg_conn = None
    if os.getenv("PKG_SHM") == "1":  # TEST: package JPEG bytes via shared memory, not pickled through the pipe
        import pkgshm
        pkg_conn = pkgshm.WorkerConn(handoff_conn, name)
    v8 = V8EventPipeline(name, config, scene=scene, body=body, gait=gait, telemetry=None,
                         handoff_client=V8HandoffClient(pkg_conn if pkg_conn is not None else handoff_conn, deployment_id, name))
    s = {k: i for i, k in enumerate(STATS)}
    secondary_policy = os.getenv("POLICY_SECONDARY_RATE") == "1"
    id_models = (v8.body, v8.gait, v8.scene)
    if os.getenv("NO_IDENTITY") == "1":  # DIAGNOSIS ONLY: body/gait/scene models never called
        id_models = (None, None, None)
        v8.body, v8.gait, v8.scene = id_models
    id_count = [0]
    import zlib
    scene_phase = zlib.crc32(name.encode("utf-8")) % 7  # as the secondary code's scene phase per camera
    if secondary_policy:
        print(f"{time.strftime('%T')} {name}: POLICY identity work (body, gait, scene) at our secondary branch's rate: "
              f"1 frame/s, 7 of 10 used (0.7/s); presence/zone/line/count/fire/face rules on every frame", flush=True)
    import json
    import queue as queue_mod
    from pipeline.dlstreamer_bridge import DlsFaceCandidate
    from pipeline.types import Detection
    L = LAYOUT
    mem = shared_memory.SharedMemory(name=shm_name)
    buf = mem.buf
    H = np.ndarray((128,), dtype=np.int64, buffer=buf)
    pix_off = lambda i: 1024 + i * (64 + L["pb"])
    main_off = lambda i: 1024 + L["ps"] * (64 + L["pb"]) + i * (64 + L["jb"])
    fire_off = lambda i: 1024 + L["ps"] * (64 + L["pb"]) + L["js"] * (64 + L["jb"]) + i * (64 + L["fb"])
    slot_hdr = lambda off: np.ndarray((6,), dtype=np.int64, buffer=buf, offset=off)
    label_to_id = {("person" if v == "pedestrian" else v): k for k, v in VEHICLE_CLASS_NAMES.items()}
    print(f"{time.strftime('%T')} {name}: event process ready (pid {os.getpid()}, threads {len(os.listdir('/proc/self/task'))})", flush=True)
    t_ready, cpu_ready = time.monotonic(), sum(os.times()[:2])
    profiler = None
    if os.getenv("PROFILE_EVENTS") == name:
        import cProfile
        profiler = cProfile.Profile()
        profiler.enable()

    def add(key, value):
        stats[s[key]] += value

    def read_json(off, n, cap):
        """(pts, t_real_ns, latency_ns, text) of ring slot at off if it still holds message n, else None."""
        h = slot_hdr(off)
        if int(h[0]) != n:
            return None
        pts, t_real, lat, length = int(h[1]), int(h[2]), int(h[3]), int(h[4])
        text = bytes(buf[off + 64:off + 64 + min(length, cap)])
        if int(h[0]) != n:
            return None
        return pts, t_real, lat, text

    ondemand = os.getenv("PIX_ON_DEMAND") == "1"
    req = [0]
    lazy = os.getenv("PIX_LAZY") == "1"
    lazy_state = {"pts": None, "frame": None, "placeholder": None, "jpeg": None, "recent": {}}
    pix_jpeg = os.getenv("PIX_JPEG") == "1"
    no_pix = os.getenv("NO_PIX") == "1"
    lazy_zero = np.zeros((1, 1, 3), dtype=np.uint8)
    if lazy:  # TEST patch of her crop/JPEG helpers (her file unchanged): a placeholder frame becomes the real frame on first use
        from pipeline import v8_events as _ve
        import uuid as _uuid, hashlib as _hashlib
        tiny_jpeg = cv2.imencode(".jpg", np.zeros((16, 16, 3), np.uint8))[1].tobytes()
        _orig_crop, _orig_evidence = _ve.V8EventPipeline._crop, _ve.V8EventPipeline._evidence

        def _materialize(arr):
            if no_pix:  # DIAGNOSIS ONLY: no image work at all; crops are cut from the empty placeholder
                return arr
            if arr is not None and arr is lazy_state["placeholder"]:
                if lazy_state["frame"] is None:
                    add("lazy_fetch", 1)
                    lazy_state["frame"] = current_frame()
                    if lazy_state["frame"] is None:
                        add("lazy_miss", 1)
                        raise RuntimeError("lazy pixels: frame no longer held by the engine")
                return lazy_state["frame"]
            old = lazy_state["recent"].get(id(arr)) if arr is not None else None
            if old is not None and old[0] is arr:  # placeholder of an EARLIER frame kept by her code
                add("lazy_stale", 1)
                frame = find_pixels(old[1], 0)
                if frame is None:
                    add("lazy_miss", 1)
                    raise RuntimeError("lazy pixels: earlier frame no longer held by the engine")
                return frame
            return arr

        class _LazyFrame(np.ndarray):
            """Placeholder frame (size only, no pixels). copy() returns the REAL pixels: her ANPR code keeps
            packet.frame.copy() as the plate evidence frame and encodes it seconds later, when the placeholder
            can no longer be recognised (a plain copy of the placeholder would be an all-black image)."""
            def copy(self, order="C"):
                real = _materialize(self)
                if real is self:  # a slice/view of a placeholder, not the frame itself: no real pixels known
                    add("copy_miss", 1)
                    return np.array(self, order=order, subok=False)
                add("lazy_copy", 1)
                return np.array(real, order=order, subok=False)

        def _lazy_evidence(self, event, frame_id, image, kind, bounds, frame_shape, now, *, content_key=None):
            if (pix_jpeg or no_pix) and kind == "frame" and image is not None and (image is lazy_state["placeholder"] or image is lazy_state["frame"]):
                body = tiny_jpeg if no_pix else current_jpeg()
                if body is not None:  # the GPU's JPEG of this same frame (quality 88, like hers): no CPU encode
                    add("jpeg_direct", 1)
                    if self.telemetry is not None:
                        self.telemetry.count("evidence_jpeg_encoded")
                    height, width = frame_shape[:2]
                    metadata = {"schema_version": "7.0", "evidence_id": str(_uuid.uuid4()),
                                "observation_id": event["observation_id"], "evidence_type": kind,
                                "captured_at": _ve.utc(now), "source_frame_id": frame_id,
                                "source_bbox_normalized": _ve.bbox_normalized(bounds, width, height) if bounds else None,
                                "content_type": "image/jpeg", "width": int(image.shape[1]),
                                "height": int(image.shape[0]), "size_bytes": len(body),
                                "sha256": "sha256:" + _hashlib.sha256(body).hexdigest()}
                    return metadata, body
            add("cpu_encoded", 1)
            return _orig_evidence(self, event, frame_id, _materialize(image), kind, bounds, frame_shape, now, content_key=content_key)
        region = os.getenv("REGION_DECODE") == "1" and pix_jpeg and not no_pix
        if region:  # TEST: crops of a not-yet-decoded frame are decoded from the GPU JPEG box only (libjpeg-turbo crop decode)
            import tjcrop

        def _region_crop(frame, box):
            if region and frame is not None and frame is lazy_state["placeholder"] and lazy_state["frame"] is None:
                height, width = frame.shape[:2]
                x1, y1, x2, y2 = [int(value) for value in box]  # same clipping as her _crop
                x1, x2 = max(0, x1), min(width, x2)
                y1, y2 = max(0, y1), min(height, y2)
                if x2 <= x1 or y2 <= y1:
                    return None, None
                body = current_jpeg()
                if body is None:
                    add("lazy_miss", 1)
                    raise RuntimeError("lazy pixels: frame no longer held by the engine")
                crop = tjcrop.decode_box(body, x1, y1, x2, y2)
                add("region_decoded", 1)
                if stats[s["region_checked"]] < 30:  # built-in check: same pixels as a full decode
                    add("region_checked", 1)
                    full = cv2.imdecode(np.frombuffer(body, np.uint8), cv2.IMREAD_COLOR)
                    if full is None or full.shape[:2] != (height, width) or not np.array_equal(full[y1:y2, x1:x2], crop):
                        add("region_mismatch", 1)
                return crop, (x1, y1, x2, y2)
            return _orig_crop(_materialize(frame), box)
        _ve.V8EventPipeline._crop = staticmethod(_region_crop)
        _ve.V8EventPipeline._evidence = _lazy_evidence
        print(f"{time.strftime('%T')} {name}: TEST lazy pixels: frame fetched only when her code crops or encodes it", flush=True)

    def current_jpeg():
        """GPU JPEG bytes of the frame being processed, fetched once per frame."""
        if lazy_state["jpeg"] is None:
            lazy_state["jpeg"] = find_pixels(lazy_state["pts"], want_jpeg=True)
        return lazy_state["jpeg"]

    def current_frame():
        """BGR of the frame being processed; with PIX_JPEG decoded from the cached JPEG."""
        if not pix_jpeg:
            return find_pixels(lazy_state["pts"])
        body = current_jpeg()
        if body is None:
            return None
        add("jpeg_decoded", 1)
        return cv2.imdecode(np.frombuffer(body, np.uint8), cv2.IMREAD_COLOR)

    def request(pts):
        """PIX_ON_DEMAND: ask the engine to copy this held frame (pts) or the newest one (-1) into the pix ring."""
        req[0] += 1
        i = req[0] % 32
        H[17 + 2 * i] = pts
        H[16 + 2 * i] = req[0]
        add("pix_requests", 1)

    def find_pixels(pts, wait_s=0.05, want_jpeg=False):
        """BGR frame of the pix slot with this pts (waits briefly for the pix branch), or None.
        PIX_JPEG slots hold the GPU's JPEG: want_jpeg returns its bytes, otherwise it is decoded."""
        if no_pix:  # DIAGNOSIS ONLY: engine sends no images; frames that need pixels are skipped at once
            return None
        if ondemand:
            request(pts)
            wait_s = max(wait_s, 0.08)
        deadline = time.monotonic() + wait_s
        while True:
            for i in range(L["ps"]):
                h = slot_hdr(pix_off(i))
                n = int(h[0])
                if n > 0 and int(h[1]) == pts and int(h[5]) > 0:
                    data = bytes(buf[pix_off(i) + 64:pix_off(i) + 64 + int(h[5])])
                    if int(slot_hdr(pix_off(i))[0]) != n:
                        add("overwritten", 1)
                        return None
                    if want_jpeg:
                        return data
                    add("jpeg_decoded", 1)
                    return cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
                if n > 0 and int(h[1]) == pts and not want_jpeg:
                    w, hh = int(h[2]), int(h[3])
                    nv12 = np.ndarray((hh * 3 // 2, w), dtype=np.uint8, buffer=buf, offset=pix_off(i) + 64)
                    bgr = cv2.cvtColor(nv12, cv2.COLOR_YUV2BGR_NV12)
                    if int(slot_hdr(pix_off(i))[0]) != n:
                        add("overwritten", 1)
                        return None
                    return bgr
            if time.monotonic() >= deadline:
                return None
            time.sleep(0.002)

    def latest_pixels():
        if ondemand:  # newest held frame, copied on request
            before = max(int(slot_hdr(pix_off(i))[0]) for i in range(L["ps"]))
            request(-1)
            deadline = time.monotonic() + 0.08
            while time.monotonic() < deadline:
                for i in range(L["ps"]):
                    h = slot_hdr(pix_off(i))
                    if int(h[0]) > before:
                        return find_pixels(int(h[1]), 0)
                time.sleep(0.002)
            return None
        best, best_i = 0, -1
        for i in range(L["ps"]):
            n = int(slot_hdr(pix_off(i))[0])
            if n > best:
                best, best_i = n, i
        return find_pixels(int(slot_hdr(pix_off(best_i))[1]), 0) if best_i >= 0 else None

    def results_from_json(objects, cfaces=None):
        """The engine's former results_from_json (integrate4.py), unchanged logic. cfaces (Option 7 face path): the faces
        with their C AdaFace embedding from the frame JSON, used instead of face objects' tensors."""
        detections, people, faces = [], [], []
        plates_seen.clear()
        for o in objects:
            label = (o.get("detection") or {}).get("label", "")
            x, y, w, h = int(o["x"]), int(o["y"]), int(o["w"]), int(o["h"])
            if label == "license_plate":  # PLATE_INLINE: plate gvadetect box on THIS frame + its OCR tensor (gvaclassify)
                ocr = next((t.get("data") for t in (o.get("tensors") or []) if t.get("data") is not None and len(t["data"]) == 15 * 37), None)
                plates_seen.append((x, y, w, h, float((o.get("detection") or {}).get("confidence", 0.0)), ocr))
                continue
            if label == "face":
                faces.append((o, (x, y, w, h)))
                continue
            class_id = label_to_id.get(label)
            if class_id is None:
                continue
            track_id = o.get("id")
            if track_id is None:
                raise RuntimeError("gvatrack did not attach a track ID")
            bbox = [x, y, x + w, y + h]
            detections.append(Detection(bbox=bbox, class_id=int(class_id), class_name=VEHICLE_CLASS_NAMES[class_id],
                                        confidence=float(o["detection"]["confidence"]), model_name="vehicle",
                                        metadata={"track_id": int(track_id), "raw_bbox": list(bbox), "predicted": False,
                                                  "tracker_backend": "gvatrack"}))
            if label == "person":
                people.append((int(track_id), x, y, w, h))
        candidates = []
        if cfaces is not None:
            faces = [({"detection": {"confidence": f["conf"]}, "tensors": [{"data": f["emb"]}]}, (f["x"], f["y"], f["w"], f["h"])) for f in cfaces]
        for o, (x, y, w, h) in faces:
            feature = None
            for tensor in o.get("tensors") or []:
                data = tensor.get("data")
                if data is not None and len(data) == 512:
                    feature = np.asarray(data, dtype=np.float32)
                    break
            if feature is None or not np.isfinite(feature).all() or float(np.linalg.norm(feature)) <= 1e-12:
                continue
            feature /= float(np.linalg.norm(feature))
            cx, cy = x + w / 2.0, y + h / 2.0
            owners = [p for p in people if p[1] <= cx <= p[1] + p[3] and p[2] <= cy <= p[2] + p[4]]
            if not owners:
                continue
            owner = min(owners, key=lambda p: p[3] * p[4])
            candidates.append(DlsFaceCandidate(bbox=(x, y, x + w, y + h), embedding=feature,
                                               detector_confidence=float(o["detection"]["confidence"]), track_id=owner[0]))
        return detections, candidates

    def hazards_from_json(objects, width, height, side=320):
        """Her hazards_from_sample (labels fire/smoke) + the engine's unletterbox from 320x320."""
        sc = min(side / width, side / height)
        pad_x, pad_y = (side - width * sc) / 2.0, (side - height * sc) / 2.0
        out = []
        for o in objects:
            label = str((o.get("detection") or {}).get("label", ""))
            if label not in {"fire", "smoke"}:
                continue
            x1, y1, x2, y2 = o["x"], o["y"], o["x"] + o["w"], o["y"] + o["h"]
            bbox = [int(max(0, min(width, (x1 - pad_x) / sc))), int(max(0, min(height, (y1 - pad_y) / sc))),
                    int(max(0, min(width, (x2 - pad_x) / sc))), int(max(0, min(height, (y2 - pad_y) / sc)))]
            out.append(Detection(bbox=bbox, class_id=0 if label == "fire" else 1, class_name=label,
                                 confidence=float(o["detection"]["confidence"]), model_name="smoke_fire",
                                 metadata={"inference_backend": "dlstreamer-gvadetect"}))
        return out

    pending_plates = []
    plates_seen = []  # PLATE_INLINE: plates of the frame being processed (from its own JSON)
    import re
    OCR_ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ_"
    OCR_PATTERNS = (re.compile(r"^[A-Z]{2}[0-9]{1,2}[A-Z]{1,3}[0-9]{4}$"), re.compile(r"^[0-9]{2}BH[0-9]{4}[A-Z]{1,2}$"))
    ROAD = {"bus", "car", "minivan", "motorcycle", "rickshaw", "truck"}

    def read_ocr(data):
        """edge_app SecondaryModels.read_plate on the gvaclassify tensor: positional decode, Indian formats only,
        confidence = geometric mean of the kept positions' probabilities."""
        p = np.asarray(data, dtype=np.float64).reshape(15, 37)
        idx = p.argmax(axis=1)
        keep = idx != OCR_ALPHABET.index("_")
        text = "".join(OCR_ALPHABET[int(i)] for i in idx if int(i) != OCR_ALPHABET.index("_"))
        text = text if any(r.fullmatch(text) for r in OCR_PATTERNS) else ""
        if not keep.any():
            return text, 0.0
        sel = p[np.arange(len(idx))[keep], idx[keep]]
        return text, float(np.exp(np.mean(np.log(np.clip(sel, 1e-12, 1.0)))))

    def inline_plates(dets, frame_no):
        """Each plate goes to the road vehicle whose box holds the plate centre ON THE SAME FRAME (smallest such box).
        Gates as gates_shared.plate_usable at native size (>= 16x5 px, aspect 0.8-12); OCR text kept at >= 0.40."""
        out = []
        vehicles = [d for d in dets if d.model_name == "vehicle" and d.class_name in ROAD and d.metadata.get("track_id") is not None]
        for i, (x, y, w, h, conf, ocr) in enumerate(plates_seen):
            add("plates_inline", 1)
            if w < 16 or h < 5 or not 0.8 <= w / max(h, 1) <= 12.0 or ocr is None:
                add("plates_gated", 1)
                continue
            cx, cy = x + w / 2.0, y + h / 2.0
            owners = [d for d in vehicles if d.bbox[0] <= cx <= d.bbox[2] and d.bbox[1] <= cy <= d.bbox[3]]
            if not owners:
                add("plates_noparent", 1)
                continue
            owner = min(owners, key=lambda d: (d.bbox[2] - d.bbox[0]) * (d.bbox[3] - d.bbox[1]))
            text, ocr_conf = read_ocr(ocr)
            if ocr_conf < 0.4:
                text = ""
            out.append(Detection(bbox=[x, y, x + w, y + h], class_id=0, class_name="license_plate", confidence=conf, model_name="license_plate",
                                 parent_id=int(owner.metadata["track_id"]),
                                 metadata={"ocr_text": text, "ocr_confidence": float(ocr_conf), "ocr_observation_id": f"inl-{name}-{frame_no}-{i}"}))
            add("plates_used", 1)
        return out

    vehicle_history = collections.OrderedDict()  # pts -> [(normalized detector box, track id)] of recent frames

    def iou_n(a, b):
        ix = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
        iy = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
        inter = ix * iy
        union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
        return inter / union if union > 0 else 0.0

    def plates_for(dets, width, height):
        """Her plate input: license_plate detections with parent_id = vehicle track id and OCR text in metadata.
        Each secondary plate is matched to the current vehicle box with the best IoU (normalized); the plate box keeps its
        position relative to the vehicle box. Unmatched plates are kept 1.5 s, then dropped (counted)."""
        out, keep, now_m = [], [], time.monotonic()
        vehicles = [d for d in dets if d.model_name == "vehicle" and d.class_name != "pedestrian" and d.metadata.get("track_id") is not None]
        for t_in, item in pending_plates:
            vb, pb, text, ocr_conf, det_conf, oid = item[:6]
            spts = item[6] if len(item) > 6 else None
            best, best_iou = None, 0.0
            track = item[7] if len(item) > 7 else None  # the plate step's own gvatrack id of the vehicle (works for frames we never got)
            by_track = track is not None
            if by_track:
                pass
            elif spts is not None and spts in vehicle_history:  # the plate's own frame: its vehicle box is one of that frame's boxes
                seen = [(iou_n(box, vb), tid) for box, tid in vehicle_history[spts]]
                if seen and max(seen)[0] >= 0.7:
                    track = max(seen)[1]
            if track is not None:
                best = next((d for d in vehicles if d.metadata["track_id"] == track), None)
                if best is None:  # its vehicle is not in this frame (occluded or missed): wait for it
                    if now_m - t_in < 1.5:
                        keep.append((t_in, item))
                    else:
                        add("plates_unmatched", 1)
                    continue
                best_iou = 1.0
                add("plates_bytrack" if by_track else "plates_exact", 1)
            for d in (vehicles if track is None else []):
                b = (d.bbox[0] / width, d.bbox[1] / height, d.bbox[2] / width, d.bbox[3] / height)
                ix = max(0.0, min(b[2], vb[2]) - max(b[0], vb[0]))
                iy = max(0.0, min(b[3], vb[3]) - max(b[1], vb[1]))
                inter = ix * iy
                union = (b[2] - b[0]) * (b[3] - b[1]) + (vb[2] - vb[0]) * (vb[3] - vb[1]) - inter
                score = inter / union if union > 0 else 0.0
                if score > best_iou:
                    best, best_iou = d, score
            if track is None and best is not None and best_iou >= 0.2:
                add("plates_fallback", 1)  # plate's frame not known (older sender or frame not processed): overlap match
            if best is None or best_iou < 0.2:
                if now_m - t_in < 1.5:
                    keep.append((t_in, item))
                else:
                    add("plates_unmatched", 1)
                continue
            x1, y1, x2, y2 = best.bbox
            vw, vh = max(1e-6, vb[2] - vb[0]), max(1e-6, vb[3] - vb[1])
            rel = ((pb[0] - vb[0]) / vw, (pb[1] - vb[1]) / vh, (pb[2] - vb[0]) / vw, (pb[3] - vb[1]) / vh)
            box = [int(x1 + rel[0] * (x2 - x1)), int(y1 + rel[1] * (y2 - y1)), int(x1 + rel[2] * (x2 - x1)), int(y1 + rel[3] * (y2 - y1))]
            box = [max(0, min(width - 1, box[0])), max(0, min(height - 1, box[1])), max(1, min(width, box[2])), max(1, min(height, box[3]))]
            if box[2] - box[0] < 2 or box[3] - box[1] < 2:
                add("plates_unmatched", 1)
                continue
            out.append(Detection(bbox=box, class_id=0, class_name="license_plate", confidence=float(det_conf), model_name="license_plate",
                                 parent_id=int(best.metadata["track_id"]),
                                 metadata={"ocr_text": text, "ocr_confidence": float(ocr_conf), "ocr_observation_id": oid}))
            add("plates_used", 1)
        pending_plates[:] = keep
        return out

    last_main = last_fire = frame_count = 0
    size = None
    latencies = []
    running = True
    idle = os.getenv("EVENTS_IDLE") == "1"  # measurement only: skip her code, just follow the rings
    while running:
        did = False
        if idle:
            try:
                if inbox.get(timeout=0.5)[0] == "stop":
                    break
            except queue_mod.Empty:
                pass
            last_main, last_fire = int(H[0]), int(H[1])
            stats[s["frames"]] = last_main
            continue
        try:
            message = inbox.get_nowait()
        except queue_mod.Empty:
            message = None
        if message is not None:
            did = True
            if message[0] == "stop":
                break
            if message[0] == "reconfigure":  # new desired-state revision, same cameras: her V8 reconfigure (as her worker's hot config)
                try:
                    v8.reconfigure(message[1])
                    config = message[1]
                    print(f"{time.strftime('%T')} {name}: hot config applied, revision {v8.config['config_revision']}", flush=True)
                except Exception as exc:  # noqa: BLE001
                    print(f"{time.strftime('%T')} {name}: hot config FAILED: {type(exc).__name__}: {exc}", flush=True)
            if message[0] == "plates":  # our secondary branch's plate + OCR results (normalized boxes), used on the next main frame
                pending_plates.extend((time.monotonic(), p) for p in message[1])
                add("plates_in", len(message[1]))
            if message[0] == "status":
                try:
                    session = v8.camera_status(message[1], message[2])
                    print(f"{time.strftime('%T')} {name}: camera_status {message[1]} ({message[2]}), stream session {session[:8]}", flush=True)
                except Exception as exc:
                    add("errors", 1)
                    print(f"{time.strftime('%T')} {name}: event code error: {type(exc).__name__}: {exc}", flush=True)
        newest = int(H[0])
        if newest > last_main:
            did = True
            if newest - last_main > L["js"] - 2:  # fell behind: skip to the newest messages still in the ring
                add("missed_main", newest - last_main - (L["js"] - 2))
                last_main = newest - (L["js"] - 2)
            n = last_main + 1
            last_main = n
            t_start = time.perf_counter()
            got = read_json(main_off(n % L["js"]), n, L["jb"])
            if got is None:
                add("missed_main", 1)
            else:
                pts, t_real, lat, text = got
                try:
                    t = time.perf_counter()
                    msg = json.loads(text)
                    res = msg.get("resolution") or {}
                    width, height = int(res.get("width", 0)), int(res.get("height", 0))
                    size = (width, height) if width else size
                    detections, candidates = results_from_json(msg.get("objects", []), msg.get("cfaces", []) if cface_mode else None)
                    add("ms_parse", (time.perf_counter() - t) * 1000)
                    frame_count += 1
                    evaluated = (frame_count - 1) % face_interval == 0
                    observed = t_real / 1e9
                    if stats[s["steady_at"]] and observed >= stats[s["steady_at"]] and lat >= 0:
                        latencies.append((lat / 1e6, time.time() * 1000 - t_real / 1e6))  # (arrival -> engine out, engine out -> her code starts)
                    add("frames", 1)
                    t = time.perf_counter()
                    if lazy:  # TEST: pixels upfront only where they are always used; otherwise fetched when her code crops/encodes
                        lazy_state["pts"], lazy_state["frame"], lazy_state["placeholder"], lazy_state["jpeg"] = pts, None, None, None
                        needed = not no_pix and (bool(candidates) or (bool(pending_plates) and os.getenv("REGION_DECODE") != "1")
                                  or (frame_count % fps == 0 and (frame_count // fps) % 10 < 7))  # identity frame (0.7/s)
                    else:
                        needed = v8.primary_pixels_required(observed, detections)
                    frame = (current_frame() if lazy else find_pixels(pts)) if needed else None
                    t1 = time.perf_counter()
                    if needed and frame is None:
                        add("skipped", 1)
                        add("no_pixels", 1)
                    else:
                        if frame is None and lazy:
                            frame = lazy_state["placeholder"] = np.broadcast_to(lazy_zero, (height, width, 3)).view(_LazyFrame)  # size only, no pixels
                            rec = lazy_state["recent"]
                            rec[id(frame)] = (frame, pts)
                            if len(rec) > 8:
                                rec.pop(next(iter(rec)))
                            add("lazy_frames", 1)
                        elif frame is None:
                            add("geometry_only", 1)
                            frame = np.empty((height, width, 0), dtype=np.uint8)
                        else:
                            add("pixels", 1)
                            if lazy:
                                lazy_state["frame"] = frame  # real pixels of this frame: full-frame evidence can still use the GPU JPEG
                        add("ms_wait_pixels", (t1 - t) * 1000)
                        packet = FramePacket(index=n, name=name, frame=frame, frame_observed_at=observed, frame_received_at=observed)
                        packet.detections = detections
                        packet.analytics_state["fire_smoke_evaluated"] = False
                        raw_boxes = [(d, (d.bbox[0] / width, d.bbox[1] / height, d.bbox[2] / width, d.bbox[3] / height))
                                     for d in detections if d.model_name == "vehicle" and d.class_name != "pedestrian"]
                        tracker.process([packet])
                        vehicle_history[pts] = [(box, d.metadata["track_id"]) for d, box in raw_boxes if d.metadata.get("track_id") is not None]
                        while len(vehicle_history) > 64:  # ~6 s at 10 fps; plates arrive within ~0.1-1.5 s
                            vehicle_history.popitem(last=False)
                        if pending_plates:
                            packet.detections.extend(plates_for(packet.detections, width, height))
                        if plates_seen:  # PLATE_INLINE: plates found on this very frame
                            packet.detections.extend(inline_plates(packet.detections, frame_count))
                        t2 = time.perf_counter()
                        add("ms_tracker", (t2 - t1) * 1000)
                        faces = []
                        if face_pipeline is not None:
                            add("face_frames", bool(candidates))
                            face_extractor.prepare(evaluated, candidates)
                            people = [d for d in packet.detections if d.model_name == "vehicle" and d.class_name == "pedestrian"
                                      and d.metadata.get("track_id") is not None]
                            faces = face_pipeline.process(packet, people)
                        t3 = time.perf_counter()
                        add("ms_faces", (t3 - t2) * 1000)
                        if secondary_policy or ours_policy:  # PRODUCTION POLICY = our secondary branch's rate: identity work on 0.7 frames/s per camera
                            second = frame_count // fps
                            identity_frame = (frame_count % fps == 0) and (second % 10 < 7)
                            if not identity_frame:
                                v8.body, v8.gait, v8.scene = None, None, None
                            elif ours_policy:  # our secondary schedule: person (body) every 3rd, gait every, scene every 7th (with phase)
                                id_count[0] += 1
                                k = id_count[0]
                                v8.body = id_models[0] if k % 3 == 0 else None
                                v8.gait = id_models[1]
                                v8.scene = id_models[2] if (k + scene_phase) % 7 == 0 else None
                            else:
                                v8.body, v8.gait, v8.scene = id_models
                            add("identity_frames", int(identity_frame))
                        v8.process(packet, faces)
                        add("ms_events", (time.perf_counter() - t3) * 1000)
                except Exception as exc:
                    add("errors", 1)
                    if stats[s["errors"]] <= 5:
                        print(f"{time.strftime('%T')} {name}: event code error: {type(exc).__name__}: {exc}", flush=True)
            add("busy_ms", (time.perf_counter() - t_start) * 1000)
        newest_fire = int(H[1])
        if newest_fire > last_fire and size is not None:
            did = True
            if newest_fire - last_fire > L["fs"] - 2:
                add("missed_fire", newest_fire - last_fire - (L["fs"] - 2))
                last_fire = newest_fire - (L["fs"] - 2)
            n = last_fire + 1
            last_fire = n
            t = time.perf_counter()
            got = read_json(fire_off(n % L["fs"]), n, L["fb"])
            if got is None:
                add("missed_fire", 1)
            else:
                try:
                    pts, t_real, _lat, text = got
                    detections = hazards_from_json(json.loads(text).get("objects", []), size[0], size[1])
                    add("fire", 1)
                    observed = t_real / 1e9
                    needed = v8.hazard_pixels_required(detections, observed)
                    frame = latest_pixels() if needed else None
                    if needed and frame is None:
                        add("skipped", 1)
                    else:
                        if frame is None:
                            frame = np.empty((size[1], size[0], 0), dtype=np.uint8)
                        packet = FramePacket(index=last_main, name=name, frame=frame, frame_observed_at=observed, frame_received_at=observed)
                        packet.detections = detections
                        v8.process_hazards(packet)
                except Exception as exc:
                    add("errors", 1)
                    if stats[s["errors"]] <= 5:
                        print(f"{time.strftime('%T')} {name}: event code error (fire): {type(exc).__name__}: {exc}", flush=True)
            add("ms_fire", (time.perf_counter() - t) * 1000)
        if not did:
            time.sleep(0.002)
    try:
        np.save(os.path.join(os.getenv("APEXFABRIC_STATE_ROOT", "/tmp"), f"lat_{name}.npy"), np.asarray(latencies, dtype=np.float32).reshape(-1, 2))
    except OSError:
        pass
    wall, cpu = time.monotonic() - t_ready, sum(os.times()[:2]) - cpu_ready
    if lazy or ondemand:
        st = {k: int(stats[s[k]]) for k in ("frames", "pixels", "lazy_frames", "lazy_fetch", "lazy_miss", "lazy_stale", "jpeg_direct", "jpeg_decoded", "cpu_encoded", "region_decoded", "region_checked", "region_mismatch", "pix_requests", "skipped", "errors")}
        if pkg_conn is not None:
            st.update(pkg_conn.stats)
        print(f"{time.strftime('%T')} {name}: PIXSTATS {st}", flush=True)
    print(f"{time.strftime('%T')} {name}: PLATESTATS " + str({k: int(stats[s[k]]) for k in ("plates_in", "plates_used", "plates_bytrack", "plates_exact", "plates_fallback", "plates_unmatched", "plates_inline", "plates_gated", "plates_noparent")}), flush=True)
    print(f"{time.strftime('%T')} {name}: CPU_USAGE cores={cpu / max(wall, 1e-6):.3f} cpu_s={cpu:.1f} wall_s={wall:.1f} "
          f"threads={len(os.listdir('/proc/self/task'))} frames={int(stats[s['frames']])}", flush=True)
    if profiler is not None:
        import pstats
        profiler.disable()
        out = os.path.join(os.getenv("APEXFABRIC_STATE_ROOT", "/tmp"), f"profile_{name}.txt")
        with open(out, "w") as f:
            st = pstats.Stats(profiler, stream=f)
            st.sort_stats("tottime").print_stats(30)
            st.sort_stats("cumulative").print_stats(45)
        print(f"{time.strftime('%T')} {name}: profile written to {out}", flush=True)
    del H
    try:
        mem.close()
    except BufferError:
        pass
    print(f"{time.strftime('%T')} {name}: event process stopped", flush=True)
