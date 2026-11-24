#!/usr/bin/env python3
"""Crops from the ORIGINAL NV12 frame, crop area only, for every use case that needs a crop - as the face path does
(user decision 2026-11-24). Applied to COPIES in the build context after face_patch.py; every replacement must match
exactly once, or the build stops.

    orig_pixels_patch.py <context>/home/chandana_optimization/integrate

Before: the camera process asked the engine for a GPU JPEG (quality 88) of a held frame and cut every crop from it.
Now (engine = her C on-demand service, camera process = her lazy-pixel helpers):
  - crop (body ReID, face evidence, subject/plate crops, scene with a zone): the camera process posts a crop request
    (frame + box) in its shared-memory header; the engine copies ONLY those rows and columns of the held NV12 frame
    (Y and UV planes, even-aligned) into a pixel slot; the camera process converts only that crop NV12 -> BGR;
  - whole frame (gait background subtraction, scene over the whole frame): the raw NV12 frame (request flag RAW),
    converted once per frame;
  - whole-frame evidence pictures stay the GPU JPEG.
Header slots (int64): 82 crop request seq, 83 pts, 84..87 x1 y1 x2 y2 (even), 88 last crop request served (16..79 are
her frame requests, 100/101 motion gate / missing timestamps). A crop slot carries -seq in its int64 [5].
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

patch(root / "gatesc" / "cgates.c", [
    # whole frame: raw NV12 copy instead of a JPEG when the request carries RAW_FLAG
    ("            long long want = hdr(r)[17 + 2 * i];\n",
     "            long long want = hdr(r)[17 + 2 * i];\n"
     "            int raw = want >= 0 && (want & (1LL << 61));  /* orig_pixels_patch: raw NV12 copy, not a JPEG */\n"
     "            if (raw) want &= ~(1LL << 61);\n"),
    ("            if (r->appsrc) {  /* GPU JPEG of this frame; jpeg_out_probe puts the bytes in the pix ring */\n",
     "            if (r->appsrc && !raw) {  /* GPU JPEG of this frame; jpeg_out_probe puts the bytes in the pix ring */\n"),
    # crop: only the box's rows and columns of the held NV12 frame
    ("#include <pthread.h>\n",
     "/* orig_pixels_patch: copy only the box (x1,y1)-(x2,y2) (even) of one held NV12 frame into the next pix slot, as a\n"
     "   small NV12 image (Y rows then interleaved UV rows); slot int64 [2],[3] = crop width/height, [5] = -seq. */\n"
     "static int copy_crop(Reader *r, void *buffer, long long pts, long long seq, int x1, int y1, int x2, int y2) {\n"
     "    if (x1 < 0) x1 = 0;\n"
     "    if (y1 < 0) y1 = 0;\n"
     "    if (x2 > r->W) x2 = r->W;\n"
     "    if (y2 > r->H) y2 = r->H;\n"
     "    x1 &= ~1; y1 &= ~1; x2 &= ~1; y2 &= ~1;\n"
     "    int cw = x2 - x1, ch = y2 - y1;\n"
     "    if (cw <= 0 || ch <= 0) return 0;\n"
     "    if ((long)cw * ch * 3 / 2 > r->pb) { __atomic_add_fetch(&hdr(r)[6], 1, __ATOMIC_RELAXED); return 0; }\n"
     "    long n = __atomic_add_fetch(&r->pix_seq, 1, __ATOMIC_RELAXED);\n"
     "    unsigned char *s = pix_slot(r, n % r->ps);\n"
     "    volatile long long *sh = (volatile long long *)s;\n"
     "    __atomic_store_n(&sh[0], 0, __ATOMIC_RELEASE);\n"
     "    unsigned char *dst = s + SLOT_HDR;\n"
     "    void *vm = gst_buffer_get_video_meta(buffer);\n"
     "    unsigned char iy[256], iuv[256];\n"
     "    gpointer py = 0, puv = 0; gint sy = 0, suv = 0;\n"
     "    int ok = 0;\n"
     "    if (vm && gst_video_meta_map(vm, 0, iy, &py, &sy, 1)) {\n"
     "        if (gst_video_meta_map(vm, 1, iuv, &puv, &suv, 1)) {\n"
     "            for (int y = 0; y < ch; y++) memcpy(dst + (long)y * cw, (unsigned char *)py + (long)(y1 + y) * sy + x1, (size_t)cw);\n"
     "            for (int y = 0; y < ch / 2; y++) memcpy(dst + (long)cw * ch + (long)y * cw, (unsigned char *)puv + (long)(y1 / 2 + y) * suv + x1, (size_t)cw);\n"
     "            gst_video_meta_unmap(vm, 1, iuv); ok = 1;\n"
     "        }\n"
     "        gst_video_meta_unmap(vm, 0, iy);\n"
     "    } else {\n"
     "        extern int gst_buffer_map(void *buffer, void *info, int flags);\n"
     "        extern void gst_buffer_unmap(void *buffer, void *info);\n"
     "        struct { void *memory; int flags; unsigned char *data; unsigned long size, maxsize; void *user[4]; void *res[4]; } mi;\n"
     "        if (gst_buffer_map(buffer, &mi, 1)) {\n"
     "            if ((long)mi.size >= r->uvoff + (long)r->uvs * (r->H / 2)) {\n"
     "                for (int y = 0; y < ch; y++) memcpy(dst + (long)y * cw, mi.data + (long)(y1 + y) * r->ys + x1, (size_t)cw);\n"
     "                for (int y = 0; y < ch / 2; y++) memcpy(dst + (long)cw * ch + (long)y * cw, mi.data + r->uvoff + (long)(y1 / 2 + y) * r->uvs + x1, (size_t)cw);\n"
     "                ok = 1;\n"
     "            }\n"
     "            gst_buffer_unmap(buffer, &mi);\n"
     "        }\n"
     "    }\n"
     "    if (!ok) return 0;\n"
     "    sh[1] = pts; sh[2] = cw; sh[3] = ch; sh[4] = now_ns(CLOCK_REALTIME); sh[5] = -seq;\n"
     "    __atomic_store_n(&sh[0], n, __ATOMIC_RELEASE);\n"
     "    __atomic_store_n(&hdr(r)[2], n, __ATOMIC_RELEASE);\n"
     "    return 1;\n"
     "}\n"
     "\n"
     "#include <pthread.h>\n"),
    ("        if (!did) { struct timespec ts = {0, 500000}; nanosleep(&ts, 0); }\n",
     "        {   /* orig_pixels_patch: crop request mailbox (header 82..88) */\n"
     "            long long cseq = __atomic_load_n(&hdr(r)[82], __ATOMIC_ACQUIRE);\n"
     "            if (cseq > hdr(r)[88]) {\n"
     "                long long cpts = hdr(r)[83];\n"
     "                int cx1 = (int)hdr(r)[84], cy1 = (int)hdr(r)[85], cx2 = (int)hdr(r)[86], cy2 = (int)hdr(r)[87];\n"
     "                hdr(r)[88] = cseq; did = 1;\n"
     "                void *cbuf = 0;\n"
     "                while (__atomic_exchange_n(&r->lock, 1, __ATOMIC_ACQUIRE)) ;\n"
     "                for (int k = 0; k < r->nheld; k++)\n"
     "                    if (r->held[k] && r->held_pts[k] == cpts) { cbuf = gst_mini_object_ref(r->held[k]); break; }\n"
     "                __atomic_store_n(&r->lock, 0, __ATOMIC_RELEASE);\n"
     "                if (!cbuf) __atomic_add_fetch(&hdr(r)[11], 1, __ATOMIC_RELAXED);  /* frame no longer held */\n"
     "                else { copy_crop(r, cbuf, cpts, cseq, cx1, cy1, cx2, cy2); gst_mini_object_unref(cbuf); }\n"
     "            }\n"
     "        }\n"
     "        if (!did) { struct timespec ts = {0, 500000}; nanosleep(&ts, 0); }\n"),
])

patch(root / "event_worker_shm.py", [
    ('                        lazy_state["pts"], lazy_state["frame"], lazy_state["placeholder"], lazy_state["jpeg"] = pts, None, None, None\n',
     '                        lazy_state["pts"], lazy_state["frame"], lazy_state["placeholder"], lazy_state["jpeg"] = pts, None, None, None\n'
     '                        lazy_state["raw"] = None  # orig_pixels_patch: raw NV12 whole frame, fetched only if a step needs it\n'),
    # crops: only the box, from the original NV12 frame
    ("                body = current_jpeg()\n"
     "                if body is None:\n"
     "                    add(\"lazy_miss\", 1)\n"
     "                    raise RuntimeError(\"lazy pixels: frame no longer held by the engine\")\n"
     "                crop = tjcrop.decode_box(body, x1, y1, x2, y2)\n"
     "                add(\"region_decoded\", 1)\n"
     "                if stats[s[\"region_checked\"]] < 30:  # built-in check: same pixels as a full decode\n"
     "                    add(\"region_checked\", 1)\n"
     "                    full = cv2.imdecode(np.frombuffer(body, np.uint8), cv2.IMREAD_COLOR)\n"
     "                    if full is None or full.shape[:2] != (height, width) or not np.array_equal(full[y1:y2, x1:x2], crop):\n"
     "                        add(\"region_mismatch\", 1)\n"
     "                return crop, (x1, y1, x2, y2)\n",
     "                ex1, ey1 = x1 & ~1, y1 & ~1  # orig_pixels_patch: the box (even-aligned) from the original NV12 frame\n"
     "                ex2, ey2 = min(width & ~1, (x2 + 1) & ~1), min(height & ~1, (y2 + 1) & ~1)\n"
     "                raw = lazy_state.get(\"raw\")\n"
     "                if raw is not None:  # whole raw frame already here (gait/scene): cut from it\n"
     "                    nv, rw, rh = raw\n"
     "                    sub = np.vstack([nv[ey1:ey2, ex1:ex2], nv[rh + ey1 // 2:rh + ey2 // 2, ex1:ex2]])\n"
     "                else:\n"
     "                    sub = request_crop(int(lazy_state[\"pts\"]), ex1, ey1, ex2, ey2)\n"
     "                    if sub is None:\n"
     "                        add(\"lazy_miss\", 1)\n"
     "                        raise RuntimeError(\"lazy pixels: frame no longer held by the engine\")\n"
     "                crop = cv2.cvtColor(sub, cv2.COLOR_YUV2BGR_NV12)[y1 - ey1:y2 - ey1, x1 - ex1:x2 - ex1].copy()\n"
     "                add(\"region_decoded\", 1)\n"
     "                return crop, (x1, y1, x2, y2)\n"),
    # whole frame: the raw NV12 frame
    ("        body = current_jpeg()\n"
     "        if body is None:\n"
     "            return None\n"
     "        add(\"jpeg_decoded\", 1)\n"
     "        return cv2.imdecode(np.frombuffer(body, np.uint8), cv2.IMREAD_COLOR)\n",
     "        raw = current_raw()  # orig_pixels_patch: the original NV12 frame, not the JPEG\n"
     "        if raw is None:\n"
     "            return None\n"
     "        add(\"jpeg_decoded\", 1)\n"
     "        return cv2.cvtColor(raw[0], cv2.COLOR_YUV2BGR_NV12)\n"
     "\n"
     "    RAW_FLAG = 1 << 61\n"
     "    import threading\n"
     "    crop_lock = threading.Lock()  # one crop request at a time (her gait worker may run in a second thread)\n"
     "    crop_seq = [0]\n"
     "\n"
     "    def wait_slot(pts, tag, timeout=0.08):\n"
     "        \"\"\"Copy of the pix slot holding frame pts with int64 [5] == tag (0 raw whole frame, -seq crop), or None.\"\"\"\n"
     "        deadline = time.monotonic() + timeout\n"
     "        while True:\n"
     "            for i in range(L[\"ps\"]):\n"
     "                h = slot_hdr(pix_off(i))\n"
     "                n = int(h[0])\n"
     "                if n > 0 and int(h[1]) == pts and int(h[5]) == tag:\n"
     "                    w, hh = int(h[2]), int(h[3])\n"
     "                    nv = np.array(np.ndarray((hh * 3 // 2, w), dtype=np.uint8, buffer=buf, offset=pix_off(i) + 64))\n"
     "                    if int(slot_hdr(pix_off(i))[0]) != n:\n"
     "                        add(\"overwritten\", 1)\n"
     "                        return None\n"
     "                    return nv, w, hh\n"
     "            if time.monotonic() >= deadline:\n"
     "                return None\n"
     "            time.sleep(0.001)\n"
     "\n"
     "    def request_crop(pts, x1, y1, x2, y2):\n"
     "        \"\"\"orig_pixels_patch: NV12 of only this box of held frame pts (engine copies just these rows/columns).\"\"\"\n"
     "        with crop_lock:\n"
     "            crop_seq[0] = max(crop_seq[0], int(H[88])) + 1\n"
     "            H[83], H[84], H[85], H[86], H[87] = pts, x1, y1, x2, y2\n"
     "            H[82] = crop_seq[0]\n"
     "            got = wait_slot(pts, -crop_seq[0])\n"
     "        return None if got is None else got[0]\n"
     "\n"
     "    def current_raw():\n"
     "        \"\"\"orig_pixels_patch: (NV12, width, height) of the whole frame being processed (raw copy, once per frame).\"\"\"\n"
     "        if lazy_state.get(\"raw\") is None and lazy_state[\"pts\"] is not None:\n"
     "            pts = int(lazy_state[\"pts\"])\n"
     "            request(pts | RAW_FLAG)\n"
     "            lazy_state[\"raw\"] = wait_slot(pts, 0)\n"
     "        return lazy_state.get(\"raw\")\n"),
])

# ANPR like the face path: OCR's crop preparation becomes a setting (OCR_PREPROC, default her opencv), as the plate
# detector's already is (PLATE_PREPROC); both cut only the box from the original frame. 'va' = GPU crop + resize.
patch(root / "integrate9.py", [
    ("model-proc={P}/ocr_model_proc.json device={OCR_DEV} pre-process-backend=opencv ",
     "model-proc={P}/ocr_model_proc.json device={OCR_DEV} pre-process-backend={os.getenv('OCR_PREPROC') or 'opencv'} "),
])
