
/* ---------------- ANPR like the face path (anpr_patch.py appends this to a COPY of cgates.c) ----------------
   Replaces the plate gvadetect + OCR gvaclassify (opencv preparation) with one C step, like cface:
   for each helper box "plate_vehicle" her selection step added (road vehicle >= 80 px, every 5th frame, changed,
   not confirmed): only the vehicle box is cut from the ORIGINAL NV12 frame (mapped, as the face step), converted to
   RGB, letterboxed to 224x224 (aspect kept, centred, sizes rounded down, bilinear, padding 114, /255 - measured to
   match DL Streamer's preparation: same OCR text on 12/12 plates, boxes within half a model pixel) -> plate model
   (NPU) -> boxes >= 0.35 mapped back to the frame -> only each plate box is cut from the NV12 frame, stretched to
   128x64 (bilinear), RGB 0..255 -> OCR (NPU) -> 15x37 scores. Her confirmation (OCR >= confirm, valid format ->
   the vehicle's track is skipped for stable_s) runs here, on the same numbers. Plates go into the frame JSON as
   "cplates": [{x,y,w,h,conf,ocr:[555]}], read by the camera process in place of the license_plate objects. */
#define PL_MAX 32
typedef struct { gint x, y, w, h; float conf; float ocr[15 * 37]; } PlateItem;
typedef struct { long long pts; int n; PlateItem p[PL_MAX]; } PlateFrame;
typedef struct { PlateFrame ring[FRING]; long w; int lock; gint W, H; ov_infer_request_t *preq, *oreq; PlateCtx *state; } PlInfCtx;
static ov_compiled_model_t *pl_model, *ocr_model;
static volatile long pl_vehicles, pl_plates, pl_ocr, pl_us;

/* bilinear resize (cv2.resize INTER_LINEAR semantics, uint8 result) of an RGB image into dst (dw x dh) */
static void pl_resize(const unsigned char *src, int sw, int sh, unsigned char *dst, int dw, int dh) {
    double sx = (double)sw / dw, sy = (double)sh / dh;
    for (int y = 0; y < dh; y++) {
        double fy = (y + 0.5) * sy - 0.5; int y0 = (int)floor(fy); double wy = fy - y0;
        if (y0 < 0) { y0 = 0; wy = 0; }
        int y1 = y0 + 1 < sh ? y0 + 1 : sh - 1;
        if (y0 > sh - 1) y0 = sh - 1;
        for (int x = 0; x < dw; x++) {
            double fx = (x + 0.5) * sx - 0.5; int x0 = (int)floor(fx); double wx = fx - x0;
            if (x0 < 0) { x0 = 0; wx = 0; }
            int x1 = x0 + 1 < sw ? x0 + 1 : sw - 1;
            if (x0 > sw - 1) x0 = sw - 1;
            for (int k = 0; k < 3; k++) {
                double v = (1 - wy) * ((1 - wx) * src[(y0 * sw + x0) * 3 + k] + wx * src[(y0 * sw + x1) * 3 + k])
                         + wy * ((1 - wx) * src[(y1 * sw + x0) * 3 + k] + wx * src[(y1 * sw + x1) * 3 + k]);
                dst[(y * dw + x) * 3 + k] = (unsigned char)(v < 0 ? 0 : v > 255 ? 255 : rint(v));
            }
        }
    }
}

static int pl_infer(ov_infer_request_t **req, ov_compiled_model_t *model, float *input, int64_t c, int64_t h, int64_t w,
                    float *out, unsigned long nout) {
    if (!*req && ov_compiled_model_create_infer_request(model, req) != OK) return 0;
    int64_t dims[4] = {1, c, h, w};
    ov_shape_t shape; ov_tensor_t *in = 0, *o = 0; void *data = 0;
    if (ov_shape_create(4, dims, &shape) != OK) return 0;
    int ok = ov_tensor_create_from_host_ptr(F32, shape, input, &in) == OK
             && ov_infer_request_set_input_tensor_by_index(*req, 0, in) == OK
             && ov_infer_request_infer(*req) == OK
             && ov_infer_request_get_output_tensor_by_index(*req, 0, &o) == OK
             && ov_tensor_data(o, &data) == OK;
    if (ok) memcpy(out, data, nout * sizeof(float));
    if (o) ov_tensor_free(o);
    if (in) ov_tensor_free(in);
    ov_shape_free(&shape);
    return ok;
}

/* OCR decode as edge_app read_plate / her plate_report_probe: argmax per position, '_' dropped, geometric mean */
static double pl_text(const float *p, char *text) {
    static const char *A = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ_";
    int m = 0, kept = 0; double lsum = 0;
    for (int t = 0; t < 15; t++) {
        int a = 0;
        for (int j = 1; j < 37; j++) if (p[t * 37 + j] > p[t * 37 + a]) a = j;
        if (a == 36) continue;
        text[m++] = A[a];
        double v = p[t * 37 + a] < 1e-12 ? 1e-12 : p[t * 37 + a];
        lsum += log(v); kept++;
    }
    text[m] = 0;
    return kept ? exp(lsum / kept) : 0;
}

static int pl_infer_probe(void *pad, ProbeInfo *info, void *data) {
    PlInfCtx *c = data;
    void *buffer = info->data;
    if (c->W <= 0) {
        void *caps = gst_pad_get_current_caps(pad);
        if (!caps) return PROBE_OK;
        void *st = gst_caps_get_structure(caps, 0);
        gst_structure_get_int(st, "width", &c->W); gst_structure_get_int(st, "height", &c->H);
        gst_mini_object_unref(caps);
        if (c->W <= 0) return PROBE_OK;
    }
    void *rm = gst_buffer_get_analytics_relation_meta(buffer);
    if (!rm) return PROBE_OK;
    /* the helper boxes her selection step added on this frame */
    typedef struct { guint id; gint x, y, w, h; gint parent; } HB;
    HB hb[64]; int nh = 0;
    gpointer state = 0; Mtd mtd; GType od = gst_analytics_od_mtd_get_mtd_type();
    while (nh < 64 && gst_analytics_relation_meta_iterate(rm, &state, od, &mtd)) {
        const char *l = g_quark_to_string(gst_analytics_od_mtd_get_obj_type(&mtd));
        if (!l || strcmp(l, "plate_vehicle") != 0) continue;
        RoiMeta *roi = gst_buffer_get_video_region_of_interest_meta_id(buffer, (gint)mtd.id);
        if (!roi) continue;
        gint x = 0, y = 0, w = 0, h = 0; float lc = 0;
        gst_analytics_od_mtd_get_location(&mtd, &x, &y, &w, &h, &lc);
        hb[nh].id = mtd.id; hb[nh].x = x; hb[nh].y = y; hb[nh].w = w; hb[nh].h = h; hb[nh].parent = roi->parent_id; nh++;
    }
    if (!nh) return PROBE_OK;
    struct timespec t0, t1; clock_gettime(CLOCK_MONOTONIC, &t0);
    void *vm = gst_buffer_get_video_meta(buffer);
    unsigned char info_y[256], info_uv[256];
    gpointer py = 0, puv = 0; gint sy = 0, suv = 0;
    if (!vm || !gst_video_meta_map(vm, 0, info_y, &py, &sy, 1)) return PROBE_OK;
    if (!gst_video_meta_map(vm, 1, info_uv, &puv, &suv, 1)) { gst_video_meta_unmap(vm, 0, info_y); return PROBE_OK; }
    PlateFrame fr; fr.pts = buf_pts(buffer); fr.n = 0;
    float *ptensor = malloc(sizeof(float) * 3 * 224 * 224), *otensor = malloc(sizeof(float) * 3 * 64 * 128);
    float *rows = malloc(sizeof(float) * 300 * 6);
    unsigned char *lb = malloc(224 * 224 * 3), *oc = malloc(128 * 64 * 3);
    for (int v = 0; v < nh; v++) {
        int x1 = hb[v].x, y1 = hb[v].y, x2 = hb[v].x + hb[v].w, y2 = hb[v].y + hb[v].h;
        if (x1 < 0) x1 = 0;
        if (y1 < 0) y1 = 0;
        if (x2 > c->W) x2 = c->W;
        if (y2 > c->H) y2 = c->H;
        int vw = x2 - x1, vh = y2 - y1;
        if (vw < 2 || vh < 2) continue;
        __atomic_add_fetch(&pl_vehicles, 1, __ATOMIC_RELAXED);
        unsigned char *rgb = malloc((size_t)vw * vh * 3);
        nv12_to_rgb(py, sy, puv, suv, x1, y1, x2, y2, rgb);  /* only the vehicle box */
        double s = fmin(224.0 / vw, 224.0 / vh);
        int nw = (int)(vw * s), nhh = (int)(vh * s);
        if (nw < 1) nw = 1;
        if (nhh < 1) nhh = 1;
        int px = (224 - nw) / 2, pyy = (224 - nhh) / 2;
        memset(lb, 114, 224 * 224 * 3);
        unsigned char *rs = malloc((size_t)nw * nhh * 3);
        pl_resize(rgb, vw, vh, rs, nw, nhh);
        for (int yy = 0; yy < nhh; yy++) memcpy(lb + ((yy + pyy) * 224 + px) * 3, rs + (size_t)yy * nw * 3, (size_t)nw * 3);
        free(rs); free(rgb);
        for (int yy = 0; yy < 224; yy++)
            for (int xx = 0; xx < 224; xx++)
                for (int k = 0; k < 3; k++) ptensor[k * 224 * 224 + yy * 224 + xx] = lb[(yy * 224 + xx) * 3 + k] / 255.0f;
        if (!pl_infer(&c->preq, pl_model, ptensor, 3, 224, 224, rows, 300 * 6)) continue;
        double sxm = (double)nw / vw, sym = (double)nhh / vh;
        for (int r = 0; r < 300 && fr.n < PL_MAX; r++) {
            const float *q = rows + r * 6;
            if (q[4] < 0.35f) continue;
            double bx1 = (q[0] - px) / sxm + x1, by1 = (q[1] - pyy) / sym + y1;
            double bx2 = (q[2] - px) / sxm + x1, by2 = (q[3] - pyy) / sym + y1;
            int ix1 = (int)fmax(0, floor(bx1)), iy1 = (int)fmax(0, floor(by1));
            int ix2 = (int)fmin(c->W, ceil(bx2)), iy2 = (int)fmin(c->H, ceil(by2));
            if (ix2 - ix1 < 2 || iy2 - iy1 < 2) continue;
            PlateItem *it = &fr.p[fr.n];
            it->x = ix1; it->y = iy1; it->w = ix2 - ix1; it->h = iy2 - iy1; it->conf = q[4];
            __atomic_add_fetch(&pl_plates, 1, __ATOMIC_RELAXED);
            unsigned char *prgb = malloc((size_t)it->w * it->h * 3);
            nv12_to_rgb(py, sy, puv, suv, ix1, iy1, ix2, iy2, prgb);  /* only the plate box */
            pl_resize(prgb, it->w, it->h, oc, 128, 64);
            free(prgb);
            for (int yy = 0; yy < 64; yy++)
                for (int xx = 0; xx < 128; xx++)
                    for (int k = 0; k < 3; k++) otensor[k * 64 * 128 + yy * 128 + xx] = (float)oc[(yy * 128 + xx) * 3 + k];
            if (!pl_infer(&c->oreq, ocr_model, otensor, 3, 64, 128, it->ocr, 15 * 37)) continue;
            __atomic_add_fetch(&pl_ocr, 1, __ATOMIC_RELAXED);
            fr.n++;
            if (c->state && c->state->state_on) {  /* her confirmation, on the same numbers */
                char text[16];
                double conf = pl_text(it->ocr, text);
                __atomic_add_fetch(&ps_reads, 1, __ATOMIC_RELAXED);
                unsigned long long tid;
                if (conf >= plate_confirm_conf && plate_format_ok(text) && track_of(rm, (guint)hb[v].parent, &tid)) {
                    double now = now_s();
                    while (__atomic_test_and_set(&c->state->lock, __ATOMIC_ACQUIRE)) ;
                    PTrack *e = pt_get(c->state, tid, now);
                    e->last_seen = now; e->confirmed_at = now; e->conf = (float)conf;
                    __atomic_clear(&c->state->lock, __ATOMIC_RELEASE);
                    __atomic_add_fetch(&ps_confirmed, 1, __ATOMIC_RELAXED);
                }
            }
        }
    }
    free(ptensor); free(otensor); free(rows); free(lb); free(oc);
    gst_video_meta_unmap(vm, 1, info_uv);
    gst_video_meta_unmap(vm, 0, info_y);
    if (fr.n) {
        while (__atomic_exchange_n(&c->lock, 1, __ATOMIC_ACQUIRE)) ;
        c->ring[c->w++ % FRING] = fr;
        __atomic_store_n(&c->lock, 0, __ATOMIC_RELEASE);
    }
    clock_gettime(CLOCK_MONOTONIC, &t1);
    __atomic_add_fetch(&pl_us, (long)((t1.tv_sec - t0.tv_sec) * 1000000L + (t1.tv_nsec - t0.tv_nsec) / 1000), __ATOMIC_RELAXED);
    return PROBE_OK;
}

static int pl_json_probe(void *pad, ProbeInfo *info, void *data) {  /* "cplates" into the frame JSON, as cfaces */
    PlInfCtx *c = data;
    void *buffer = info->data;
    long long pts = buf_pts(buffer);
    PlateFrame copy; copy.n = 0;
    while (__atomic_exchange_n(&c->lock, 1, __ATOMIC_ACQUIRE)) ;
    for (int i = 0; i < FRING; i++)
        if (c->ring[i].pts == pts && c->ring[i].n > 0) { copy = c->ring[i]; c->ring[i].n = 0; break; }
    __atomic_store_n(&c->lock, 0, __ATOMIC_RELEASE);
    if (!copy.n) return PROBE_OK;
    void *meta = 0;
    gst_buffer_foreach_meta(buffer, find_json_meta, &meta);
    if (!meta) return PROBE_OK;
    char **msgp = (char **)((unsigned char *)meta + 16);
    const char *old = *msgp;
    if (!old) return PROBE_OK;
    long len = (long)strlen(old), cap = len + 64 + (long)copy.n * (555 * 12 + 160);
    char *out = g_malloc((unsigned long)cap), *p = out;
    long k = len - 1; while (k > 0 && old[k] != '}') k--;
    memcpy(p, old, (size_t)k); p += k;
    p += sprintf(p, ",\"cplates\":[");
    for (int j = 0; j < copy.n; j++) {
        PlateItem *it = &copy.p[j];
        p += sprintf(p, "%s{\"x\":%d,\"y\":%d,\"w\":%d,\"h\":%d,\"conf\":%.5f,\"ocr\":[", j ? "," : "", it->x, it->y, it->w, it->h, it->conf);
        for (int q = 0; q < 15 * 37; q++) p += sprintf(p, "%s%.6g", q ? "," : "", it->ocr[q]);
        p += sprintf(p, "]}");
    }
    p += sprintf(p, "]}");
    *msgp = out;
    g_free((void *)old);
    return PROBE_OK;
}

/* models are compiled once (shared by every camera, as AdaFace); each camera has its own infer requests */
void *cplate_infer_new(void *state, const char *plate_model, const char *ocr_model_path, const char *device) {
    if (!pl_model || !ocr_model) {
        ov_model_t *m = 0;
        if (!pd_core && ov_core_create(&pd_core) != OK) return 0;
        if (!pl_model) {
            if (ov_core_read_model(pd_core, plate_model, 0, &m) != OK) return 0;
            int st = ov_core_compile_model(pd_core, m, device, 2, &pl_model, "PERFORMANCE_HINT", "LATENCY");
            ov_model_free(m);
            if (st != OK) { pl_model = 0; return 0; }
        }
        if (!ocr_model) {
            if (ov_core_read_model(pd_core, ocr_model_path, 0, &m) != OK) return 0;
            int st = ov_core_compile_model(pd_core, m, device, 2, &ocr_model, "PERFORMANCE_HINT", "LATENCY");
            ov_model_free(m);
            if (st != OK) { ocr_model = 0; return 0; }
        }
    }
    PlInfCtx *c = calloc(1, sizeof(PlInfCtx));
    c->state = state;
    for (int i = 0; i < FRING; i++) c->ring[i].pts = -2;
    return c;
}
void cplate_infer_attach(void *ctx, void *infer_pad, void *json_pad) {
    gst_pad_add_probe(infer_pad, PROBE_TYPE_BUFFER, pl_infer_probe, ctx, 0);
    gst_pad_add_probe(json_pad, PROBE_TYPE_BUFFER, pl_json_probe, ctx, 0);
}
void cplate_infer_counts(long *out) { out[0] = pl_vehicles; out[1] = pl_plates; out[2] = pl_ocr; out[3] = pl_us; }
