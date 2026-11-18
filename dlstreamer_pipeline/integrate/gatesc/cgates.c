/* The two main-branch quality gates of ingest_gates.py (VehicleDedup, FaceSize) in C, as pad probes, so they do not
   take the Python lock or read boxes through the gstgva wrapper. Same rules as gates_shared.py:
     vehicle_dedup: among road vehicles (any class), highest confidence first; a box whose IoU with a kept one is
                    >= 0.55, or that is >= 85 % inside it (share of the smaller box), is removed. Persons never removed.
     face_size:     faces narrower than FACE_MIN_PX (28) px are removed.
   Boxes are read as gstgva does (analytics OD mtd: location, confidence, type) and removed as gstgva remove_region
   does (the GstVideoRegionOfInterestMeta with the same id). The box has no GStreamer headers, so the few stable
   declarations needed are written here (as strip_meta.c). Build: see build.sh. */
#include <string.h>

typedef unsigned int guint;
typedef int gint;
typedef unsigned int GQuark;
typedef unsigned long GType;
typedef void *gpointer;

typedef struct { guint id; void *meta; } Mtd;  /* GstAnalyticsMtd / GstAnalyticsODMtd */
typedef struct { int type; unsigned long id; void *data; } ProbeInfo;
typedef int (*ProbeFunc)(void *pad, ProbeInfo *info, void *data);

extern void *gst_mini_object_make_writable(void *object);
extern unsigned long gst_pad_add_probe(void *pad, int mask, ProbeFunc func, void *data, void *destroy);
extern void *gst_buffer_get_analytics_relation_meta(void *buffer);
extern int gst_analytics_relation_meta_iterate(void *meta, gpointer *state, GType type, Mtd *rlt_mtd);
extern GType gst_analytics_od_mtd_get_mtd_type(void);
extern int gst_analytics_od_mtd_get_location(const Mtd *handle, gint *x, gint *y, gint *w, gint *h, float *loc_conf_lvl);
extern int gst_analytics_od_mtd_get_confidence_lvl(const Mtd *handle, float *conf);
extern GQuark gst_analytics_od_mtd_get_obj_type(const Mtd *handle);
extern const char *g_quark_to_string(GQuark quark);
extern void *gst_buffer_get_video_region_of_interest_meta_id(void *buffer, gint id);
extern int gst_buffer_remove_meta(void *buffer, void *meta);

#define PROBE_TYPE_BUFFER (1 << 4)
#define PROBE_OK 1
#define MAXB 512
#define DEDUP_IOU 0.55
#define DEDUP_CONTAINED 0.85

static volatile long n_vehicles, n_dedup, n_faces, n_small, n_frames_dedup, n_frames_face;
static int face_min_px = 28;

typedef struct { guint id; double conf, x1, y1, x2, y2; int vehicle, face, w; } Box;

static int is_road_vehicle(const char *label) {
    static const char *v[] = {"bicycle", "bus", "car", "minivan", "motorcycle", "rickshaw", "truck", 0};
    for (int i = 0; label && v[i]; i++)
        if (strcmp(label, v[i]) == 0) return 1;
    return 0;
}

static int read_boxes(void *buffer, Box *b) {  /* all OD boxes, in relation-meta order (as gstgva regions()) */
    void *rm = gst_buffer_get_analytics_relation_meta(buffer);
    if (!rm) return 0;
    gpointer state = 0;
    Mtd mtd;
    int n = 0;
    GType od = gst_analytics_od_mtd_get_mtd_type();
    while (n < MAXB && gst_analytics_relation_meta_iterate(rm, &state, od, &mtd)) {
        gint x = 0, y = 0, w = 0, h = 0;
        float lc = 0, conf = 0;
        gst_analytics_od_mtd_get_location(&mtd, &x, &y, &w, &h, &lc);
        gst_analytics_od_mtd_get_confidence_lvl(&mtd, &conf);
        const char *label = g_quark_to_string(gst_analytics_od_mtd_get_obj_type(&mtd));
        b[n].id = mtd.id;
        b[n].conf = conf;
        b[n].x1 = x; b[n].y1 = y; b[n].x2 = (double)x + w; b[n].y2 = (double)y + h;
        b[n].vehicle = is_road_vehicle(label);
        b[n].face = label && strcmp(label, "face") == 0;
        b[n].w = w;
        n++;
    }
    return n;
}

static void remove_box(void *buffer, guint id) {  /* as gstgva VideoFrame.remove_region */
    void *roi = gst_buffer_get_video_region_of_interest_meta_id(buffer, (gint)id);
    if (roi) gst_buffer_remove_meta(buffer, roi);
}

static int duplicate(const Box *a, const Box *b) {  /* gates_shared._overlap + threshold */
    double w = (a->x2 < b->x2 ? a->x2 : b->x2) - (a->x1 > b->x1 ? a->x1 : b->x1);
    double h = (a->y2 < b->y2 ? a->y2 : b->y2) - (a->y1 > b->y1 ? a->y1 : b->y1);
    if (w < 0) w = 0;
    if (h < 0) h = 0;
    double inter = w * h;
    double aa = (a->x2 - a->x1 > 0 ? a->x2 - a->x1 : 0) * (a->y2 - a->y1 > 0 ? a->y2 - a->y1 : 0);
    double ab = (b->x2 - b->x1 > 0 ? b->x2 - b->x1 : 0) * (b->y2 - b->y1 > 0 ? b->y2 - b->y1 : 0);
    double uni = aa + ab - inter, smaller = aa < ab ? aa : ab;
    double iou = uni > 0 ? inter / uni : 0, inside = smaller > 0 ? inter / smaller : 0;
    return iou >= DEDUP_IOU || inside >= DEDUP_CONTAINED;
}

/* The gate decisions, shared by the live probes and the record-only test probes (cgates_record.c).
   drop[i] = 1 for each box the gate removes. Returns the number of road vehicles (dedup) or faces (face size). */
static int decide_dedup(const Box *b, int n, char *drop) {
    int order[MAXB], kept[MAXB], nv = 0, nk = 0;
    memset(drop, 0, n);
    for (int i = 0; i < n; i++)
        if (b[i].vehicle) order[nv++] = i;
    if (nv > 1) {
        for (int i = 1; i < nv; i++) {  /* stable insertion sort: confidence high to low, then index (as main) */
            int v = order[i], j = i - 1;
            while (j >= 0 && b[order[j]].conf < b[v].conf) { order[j + 1] = order[j]; j--; }
            order[j + 1] = v;
        }
        for (int i = 0; i < nv; i++) {
            int dup = 0;
            for (int k = 0; k < nk && !dup; k++) dup = duplicate(&b[order[i]], &b[kept[k]]);
            if (dup) drop[order[i]] = 1;
            else kept[nk++] = order[i];
        }
    }
    return nv;
}

static int decide_face(const Box *b, int n, char *drop) {
    int faces = 0;
    for (int i = 0; i < n; i++) {
        drop[i] = b[i].face && b[i].w < face_min_px;
        faces += b[i].face;
    }
    return faces;
}

static int dedup_probe(void *pad, ProbeInfo *info, void *data) {
    Box b[MAXB];
    char drop[MAXB];
    int removed = 0;
    void *buffer = gst_mini_object_make_writable(info->data);
    info->data = buffer;
    int n = read_boxes(buffer, b);
    int nv = decide_dedup(b, n, drop);
    for (int i = 0; i < n; i++)
        if (drop[i]) { remove_box(buffer, b[i].id); removed++; }
    __atomic_add_fetch(&n_vehicles, nv, __ATOMIC_RELAXED);
    __atomic_add_fetch(&n_dedup, removed, __ATOMIC_RELAXED);
    __atomic_add_fetch(&n_frames_dedup, 1, __ATOMIC_RELAXED);
    return PROBE_OK;
}

static int face_probe(void *pad, ProbeInfo *info, void *data) {
    Box b[MAXB];
    char drop[MAXB];
    int small = 0;
    void *buffer = gst_mini_object_make_writable(info->data);
    info->data = buffer;
    int n = read_boxes(buffer, b);
    int faces = decide_face(b, n, drop);
    for (int i = 0; i < n; i++)
        if (drop[i]) { remove_box(buffer, b[i].id); small++; }
    __atomic_add_fetch(&n_faces, faces, __ATOMIC_RELAXED);
    __atomic_add_fetch(&n_small, small, __ATOMIC_RELAXED);
    __atomic_add_fetch(&n_frames_face, 1, __ATOMIC_RELAXED);
    return PROBE_OK;
}

void cgates_attach_dedup(void *pad) { gst_pad_add_probe(pad, PROBE_TYPE_BUFFER, dedup_probe, 0, 0); }
void cgates_attach_face(void *pad, int min_px) { face_min_px = min_px; gst_pad_add_probe(pad, PROBE_TYPE_BUFFER, face_probe, 0, 0); }
void cgates_counts(long *out) {  /* vehicles, vehicle_dedup, faces, face_size, frames through dedup, frames through face */
    out[0] = n_vehicles; out[1] = n_dedup; out[2] = n_faces; out[3] = n_small; out[4] = n_frames_dedup; out[5] = n_frames_face;
}

/* ---------------- head-pose gate, as sentry_cv main (runtime_v6 openvino_face.py) ----------------
   Main: face box + 20 % border (clamped to the frame) -> resize so the short side is 256 -> centre 224x224 ->
   SixDRepNet -> rotation matrix -> pitch/yaw/roll; non-finite matrix = head_pose_invalid, |pitch| > 25, |yaw| > 35
   or |roll| > 25 degrees = head_pose_out_of_range; both mean the face gets no embedding.
   Here: pose_pre adds a helper box "face_pose" with exactly that 224x224 window (in frame pixels) for each face still
   present (parent_id = the face's id); gvaclassify object-class=face_pose runs the model on it (ImageNet
   normalisation in the model-proc); pose_post reads the 3x3 matrix, applies main's rules, removes the face if
   rejected and always removes the helper box. */
#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <time.h>

typedef struct { void *data; void *next; void *prev; } GListNode;
typedef struct { unsigned int flags; const void *info; GQuark roi_type; gint id; gint parent_id; guint x, y, w, h; GListNode *params; } RoiMeta;

extern int gst_analytics_relation_meta_add_od_mtd(void *meta, GQuark type, gint x, gint y, gint w, gint h, float conf, Mtd *out);
extern void *gst_buffer_add_video_region_of_interest_meta_id(void *buffer, GQuark roi_type, guint x, guint y, guint w, guint h);
extern GQuark g_quark_from_string(const char *s);
extern const void *gst_structure_get_value(const void *structure, const char *field);
extern void *g_value_get_variant(const void *value);
extern const void *g_variant_get_fixed_array(void *variant, unsigned long *n_elements, unsigned long element_size);
extern unsigned long g_variant_get_size(void *variant);
extern void *gst_pad_get_current_caps(void *pad);
extern void *gst_caps_get_structure(const void *caps, guint index);
extern int gst_structure_get_int(const void *structure, const char *field, gint *value);
extern void gst_mini_object_unref(void *object);
extern const char *g_type_name(GType type);
extern void *gst_structure_new_empty(const char *name);
extern void gst_structure_set(void *structure, const char *field, ...);
extern void gst_video_region_of_interest_meta_add_param(void *meta, void *structure);
#define G_TYPE_DOUBLE_ ((GType)(15 << 2))  /* G_TYPE_DOUBLE */

static double max_pitch = 25, max_yaw = 35, max_roll = 25, pose_context = 0.20;
static volatile long p_faces, p_posed, p_missing, p_invalid, p_range, p_kept, a_faces_roi, a_faces_orphan, a_embedded;

typedef struct { gint W, H; long frame; FILE *rec; int record; } PoseCtx;  /* DirectCtx starts with the same fields */

static void frame_size(void *pad, PoseCtx *c) {
    if (c->W > 0) return;
    void *caps = gst_pad_get_current_caps(pad);
    if (!caps) return;
    void *s = gst_caps_get_structure(caps, 0);
    gst_structure_get_int(s, "width", &c->W);
    gst_structure_get_int(s, "height", &c->H);
    gst_mini_object_unref(caps);
}

/* main's crop: _pose_crop (20 % border, clamped) then estimate() (short side to 256, centre 224), in frame pixels */
static int pose_window(gint W, gint H, const Box *f, gint *ox, gint *oy, gint *ow, gint *oh) {
    gint x1 = (gint)lround(f->x1), y1 = (gint)lround(f->y1), x2 = (gint)lround(f->x2), y2 = (gint)lround(f->y2);
    gint bw = x2 - x1 > 1 ? x2 - x1 : 1, bh = y2 - y1 > 1 ? y2 - y1 : 1;
    gint px = (gint)lround(pose_context * bw), py = (gint)lround(pose_context * bh);
    gint left = x1 - px > 0 ? x1 - px : 0, top = y1 - py > 0 ? y1 - py : 0;
    gint right = x2 + px < W ? x2 + px : W, bottom = y2 + py < H ? y2 + py : H;
    gint pw = right - left, ph = bottom - top;
    if (pw <= 0 || ph <= 0) return 0;
    double rw, rh;  /* PIL resize target, as estimate() */
    if (pw <= ph) { rw = 256; rh = lround(256.0 * ph / pw); if (rh < 256) rh = 256; }
    else { rh = 256; rw = lround(256.0 * pw / ph); if (rw < 256) rw = 256; }
    double cl = lround((rw - 224) / 2), ct = lround((rh - 224) / 2);
    double sx = rw / pw, sy = rh / ph;
    *ox = left + (gint)lround(cl / sx); *oy = top + (gint)lround(ct / sy);
    *ow = (gint)lround(224 / sx); *oh = (gint)lround(224 / sy);
    if (*ow < 1) *ow = 1;
    if (*oh < 1) *oh = 1;
    if (*ox + *ow > right) *ow = right - *ox;
    if (*oy + *oh > bottom) *oh = bottom - *oy;
    return *ow > 0 && *oh > 0;
}

static int pose_pre_probe(void *pad, ProbeInfo *info, void *data) {
    PoseCtx *c = data;
    frame_size(pad, c);
    void *buffer = gst_mini_object_make_writable(info->data);
    info->data = buffer;
    Box b[MAXB];
    int n = read_boxes(buffer, b);
    void *rm = gst_buffer_get_analytics_relation_meta(buffer);
    GQuark q = g_quark_from_string("face_pose");
    for (int i = 0; i < n && rm && c->W > 0; i++) {
        if (!b[i].face || !gst_buffer_get_video_region_of_interest_meta_id(buffer, (gint)b[i].id)) continue;  /* removed faces skipped */
        __atomic_add_fetch(&p_faces, 1, __ATOMIC_RELAXED);
        gint x, y, w, h;
        if (!pose_window(c->W, c->H, &b[i], &x, &y, &w, &h)) continue;
        Mtd od;
        if (!gst_analytics_relation_meta_add_od_mtd(rm, q, x, y, w, h, 1.0f, &od)) continue;
        RoiMeta *roi = gst_buffer_add_video_region_of_interest_meta_id(buffer, q, (guint)x, (guint)y, (guint)w, (guint)h);
        if (!roi) continue;
        roi->id = (gint)od.id;
        roi->parent_id = (gint)b[i].id;
        void *det = gst_structure_new_empty("detection");  /* as gstgva add_region: the normalised box DL Streamer crops by */
        gst_structure_set(det, "confidence", G_TYPE_DOUBLE_, 1.0, "x_min", G_TYPE_DOUBLE_, (double)x / c->W,
                          "x_max", G_TYPE_DOUBLE_, (double)(x + w) / c->W, "y_min", G_TYPE_DOUBLE_, (double)y / c->H,
                          "y_max", G_TYPE_DOUBLE_, (double)(y + h) / c->H, (char *)0);
        gst_video_region_of_interest_meta_add_param(roi, det);
    }
    return PROBE_OK;
}

static int read_matrix(RoiMeta *roi, double *m) {  /* the 3x3 FP32 raw tensor gvaclassify attached to the helper box */
    for (GListNode *p = roi->params; p; p = p->next) {
        const GType *value = gst_structure_get_value(p->data, "data_buffer");
        if (!value || strcmp(g_type_name(*value), "GVariant") != 0) continue;
        void *v = g_value_get_variant(value);  /* DL Streamer stores the raw tensor as a byte array ("ay") */
        if (!v || g_variant_get_size(v) != 9 * sizeof(float)) continue;
        unsigned long count = 0;
        const unsigned char *bytes = g_variant_get_fixed_array(v, &count, 1);
        if (!bytes || count != 9 * sizeof(float)) continue;
        float f[9];
        memcpy(f, bytes, sizeof f);
        for (int k = 0; k < 9; k++) m[k] = f[k];
        return 1;
    }
    return 0;
}

/* main's _rotation_to_euler; returns 0 if the matrix is not finite (head_pose_invalid) */
static int rotation_to_euler(const double *m, double *pitch, double *yaw, double *roll) {
    for (int k = 0; k < 9; k++)
        if (!isfinite(m[k])) return 0;
    double sy = hypot(m[0], m[3]);
    if (sy >= 1e-6) { *pitch = atan2(m[7], m[8]); *yaw = atan2(-m[6], sy); *roll = atan2(m[3], m[0]); }
    else { *pitch = atan2(-m[5], m[4]); *yaw = atan2(-m[6], sy); *roll = 0.0; }
    *pitch *= 180.0 / M_PI; *yaw *= 180.0 / M_PI; *roll *= 180.0 / M_PI;
    return 1;
}

static int pose_post_probe(void *pad, ProbeInfo *info, void *data) {
    PoseCtx *c = data;
    void *buffer = c->record ? info->data : gst_mini_object_make_writable(info->data);
    info->data = buffer;
    Box b[MAXB];
    int n = read_boxes(buffer, b);
    GQuark q = g_quark_from_string("face_pose");
    for (int i = 0; i < n; i++) {
        RoiMeta *roi = gst_buffer_get_video_region_of_interest_meta_id(buffer, (gint)b[i].id);
        if (!roi || roi->roi_type != q) continue;
        double m[9], pitch = 0, yaw = 0, roll = 0;
        const char *decision;
        int reject = 0;
        if (!read_matrix(roi, m)) { decision = "missing"; __atomic_add_fetch(&p_missing, 1, __ATOMIC_RELAXED); }
        else if (!rotation_to_euler(m, &pitch, &yaw, &roll)) { decision = "head_pose_invalid"; reject = 1; __atomic_add_fetch(&p_invalid, 1, __ATOMIC_RELAXED); }
        else if (fabs(pitch) > max_pitch || fabs(yaw) > max_yaw || fabs(roll) > max_roll) {
            decision = "head_pose_out_of_range"; reject = 1; __atomic_add_fetch(&p_range, 1, __ATOMIC_RELAXED);
        } else { decision = "kept"; __atomic_add_fetch(&p_kept, 1, __ATOMIC_RELAXED); }
        __atomic_add_fetch(&p_posed, 1, __ATOMIC_RELAXED);
        gint face = roi->parent_id;
        if (c->rec && getenv("POSE_DEBUG")) {  /* temporary: what is attached to the helper box */
            extern const char *gst_structure_get_name(const void *structure);
            fprintf(c->rec, "#DBG frame %ld helper od %u roi id %d parent %d box %u,%u %ux%u params:", c->frame, b[i].id, roi->id,
                    roi->parent_id, roi->x, roi->y, roi->w, roi->h);
            for (GListNode *p = roi->params; p; p = p->next) {
                const GType *value = gst_structure_get_value(p->data, "data_buffer");
                fprintf(c->rec, " [%s %s %lu]", gst_structure_get_name(p->data), value ? g_type_name(*value) : "-",
                        value && strcmp(g_type_name(*value), "GVariant") == 0 ? g_variant_get_size(g_value_get_variant(value)) : 0UL);
            }
            fprintf(c->rec, " m=%.3f,%.3f,%.3f,%.3f,%.3f,%.3f,%.3f,%.3f,%.3f\n", m[0], m[1], m[2], m[3], m[4], m[5], m[6], m[7], m[8]);
        }
        if (c->rec) {
            RoiMeta *fr = gst_buffer_get_video_region_of_interest_meta_id(buffer, face);
            fprintf(c->rec, "%ld %d %u %u %u %u %.4f %.4f %.4f %s\n", c->frame, face, fr ? fr->x : 0, fr ? fr->y : 0,
                    fr ? fr->w : 0, fr ? fr->h : 0, pitch, yaw, roll, decision);
        }
        if (!c->record) {
            gst_buffer_remove_meta(buffer, roi);  /* helper box */
            if (reject) remove_box(buffer, (guint)face);  /* main: rejected faces get no embedding */
        }
    }
    c->frame++;
    return PROBE_OK;
}

static int align_check_probe(void *pad, ProbeInfo *info, void *data) {  /* after face_align: do removed faces get an embedding? */
    Box b[MAXB];
    int n = read_boxes(info->data, b);
    for (int i = 0; i < n; i++) {
        if (!b[i].face) continue;
        RoiMeta *roi = gst_buffer_get_video_region_of_interest_meta_id(info->data, (gint)b[i].id);
        if (!roi) { __atomic_add_fetch(&a_faces_orphan, 1, __ATOMIC_RELAXED); continue; }
        __atomic_add_fetch(&a_faces_roi, 1, __ATOMIC_RELAXED);
        for (GListNode *p = roi->params; p; p = p->next) {
            const GType *value = gst_structure_get_value(p->data, "data_buffer");
            if (value && strcmp(g_type_name(*value), "GVariant") == 0) {
                void *v = g_value_get_variant(value);
                if (v && g_variant_get_size(v) >= 512 * sizeof(float)) { __atomic_add_fetch(&a_embedded, 1, __ATOMIC_RELAXED); break; }
            }
        }
    }
    return PROBE_OK;
}

static void pose_limits(void) {
    const char *e;
    if ((e = getenv("HEAD_POSE_MAX_PITCH_DEGREES"))) max_pitch = atof(e);
    if ((e = getenv("HEAD_POSE_MAX_YAW_DEGREES"))) max_yaw = atof(e);
    if ((e = getenv("HEAD_POSE_MAX_ROLL_DEGREES"))) max_roll = atof(e);
    if ((e = getenv("HEAD_POSE_CONTEXT_RATIO"))) pose_context = atof(e);
}

/* record_path NULL: live gate. record_path set: record only (nothing removed), for the accuracy check. */
void cgates_attach_pose(void *pre_pad, void *post_pad, const char *record_path) {
    pose_limits();
    PoseCtx *c = calloc(1, sizeof(PoseCtx));
    if (record_path) { c->rec = fopen(record_path, "w"); c->record = 1; }
    gst_pad_add_probe(pre_pad, PROBE_TYPE_BUFFER, pose_pre_probe, c, 0);
    gst_pad_add_probe(post_pad, PROBE_TYPE_BUFFER, pose_post_probe, c, 0);
}
void cgates_attach_align_check(void *pad) { gst_pad_add_probe(pad, PROBE_TYPE_BUFFER, align_check_probe, 0, 0); }
void cgates_pose_counts(long *out) {  /* faces in, posed, missing, invalid, out_of_range, kept, after-align: faces, removed (no box), embedded */
    out[0] = p_faces; out[1] = p_posed; out[2] = p_missing; out[3] = p_invalid; out[4] = p_range; out[5] = p_kept;
    out[6] = a_faces_roi; out[7] = a_faces_orphan; out[8] = a_embedded;
}
void cgates_pose_flush(void) { fflush(0); }

/* ---------------- head pose done directly, exactly as sentry_cv main (no gvaclassify) ----------------
   DL Streamer's own crop/resize gave answers ~6 deg apart from main's code and this model is very sensitive to the
   resize method, so here the face preparation is main's, step by step, on the frame's own NV12 pixels:
     NV12 -> RGB with OpenCV's integer formula (cv2.COLOR_YUV2BGR_NV12, as main's frames) -> _pose_crop (face box + 20 %,
     clamped) -> PIL Image.resize BILINEAR to short side 256 (Pillow's two-pass filter and 22-bit integer weights) ->
     centre 224x224 (Python round: half to even) -> /255, ImageNet mean/std -> SixDRepNet best_a0_fp16 via OpenVINO
     (GPU, or NPU with f16 as main) -> _rotation_to_euler -> main's limits. Pixels are read only on frames with a face. */
#include <openvino/c/openvino.h>

static ov_core_t *pd_core;
static ov_compiled_model_t *pd_model;
static volatile long pd_frames_mapped, pd_us, pd_map_us, pd_prep_us, pd_infer_us;
static inline long us_since(struct timespec *a) { struct timespec b; clock_gettime(CLOCK_MONOTONIC, &b); long d = (b.tv_sec - a->tv_sec) * 1000000L + (b.tv_nsec - a->tv_nsec) / 1000; *a = b; return d; }

typedef struct { gint W, H; long frame; FILE *rec; int record; ov_infer_request_t *req; } DirectCtx;

extern void *gst_buffer_get_video_meta(void *buffer);
extern int gst_video_meta_map(void *meta, guint plane, void *info, gpointer *data, gint *stride, int flags);
extern int gst_video_meta_unmap(void *meta, guint plane, void *info);

static inline unsigned char sat8(int v) { return v < 0 ? 0 : v > 255 ? 255 : (unsigned char)v; }

/* cv2 YUV420sp (NV12) -> RGB, OpenCV's fixed-point BT.601 coefficients; region [x0,x1) x [y0,y1) into rgb (w*h*3) */
static void nv12_to_rgb(const unsigned char *Y, int ys, const unsigned char *UV, int uvs, int x0, int y0, int x1, int y1, unsigned char *rgb) {
    const int CY = 1220542, CUB = 2116026, CUG = -409993, CVG = -852492, CVR = 1673527, SH = 20, HALF = 1 << (SH - 1);
    int w = x1 - x0;
    for (int y = y0; y < y1; y++)
        for (int x = x0; x < x1; x++) {
            int yy = Y[y * ys + x] - 16; if (yy < 0) yy = 0;
            const unsigned char *c = UV + (y / 2) * uvs + (x / 2) * 2;
            int u = c[0] - 128, v = c[1] - 128, y00 = yy * CY;
            unsigned char *o = rgb + ((y - y0) * w + (x - x0)) * 3;
            o[0] = sat8((y00 + HALF + CVR * v) >> SH);
            o[1] = sat8((y00 + HALF + CVG * v + CUG * u) >> SH);
            o[2] = sat8((y00 + HALF + CUB * u) >> SH);
        }
}

/* Pillow ImagingResample, BILINEAR, 8 bits per channel: precompute_coeffs + normalize_coeffs_8bpc (PRECISION_BITS 22) */
#define PB 22
typedef struct { int n, ksize; int *bounds; int *k; } Coeffs;
static void coeffs(int in_size, int out_size, Coeffs *c) {
    double scale = (double)in_size / out_size, fs = scale < 1.0 ? 1.0 : scale, support = 1.0 * fs;
    int ksize = (int)ceil(support) * 2 + 1;
    c->n = out_size; c->ksize = ksize;
    c->bounds = malloc(sizeof(int) * 2 * out_size); c->k = calloc((size_t)out_size * ksize, sizeof(int));
    double *kk = malloc(sizeof(double) * ksize);
    for (int xx = 0; xx < out_size; xx++) {
        double center = (xx + 0.5) * scale, ww = 0.0, ss = 1.0 / fs;
        int xmin = (int)(center - support + 0.5); if (xmin < 0) xmin = 0;
        int xmax = (int)(center + support + 0.5); if (xmax > in_size) xmax = in_size; xmax -= xmin;
        for (int x = 0; x < xmax; x++) { double t = fabs((x + xmin - center + 0.5) * ss); double w = t < 1.0 ? 1.0 - t : 0.0; kk[x] = w; ww += w; }
        for (int x = 0; x < xmax; x++) { double w = ww != 0.0 ? kk[x] / ww : kk[x];
            c->k[xx * ksize + x] = w < 0 ? (int)(-0.5 + w * (1 << PB)) : (int)(0.5 + w * (1 << PB)); }
        c->bounds[xx * 2] = xmin; c->bounds[xx * 2 + 1] = xmax;
    }
    free(kk);
}
static void coeffs_free(Coeffs *c) { free(c->bounds); free(c->k); }
static inline unsigned char clip8(int v) { v >>= PB; return v < 0 ? 0 : v > 255 ? 255 : (unsigned char)v; }

/* PIL resize (w,h)->(ow,oh): horizontal pass then vertical, each rounded to 8 bits (only passes that change the size) */
static unsigned char *pil_resize(const unsigned char *in, int w, int h, int ow, int oh) {
    const unsigned char *src = in;
    unsigned char *tmp = 0, *out;
    if (ow != w) {
        Coeffs c; coeffs(w, ow, &c);
        tmp = malloc((size_t)ow * h * 3);
        for (int y = 0; y < h; y++)
            for (int xx = 0; xx < ow; xx++) {
                int xmin = c.bounds[xx * 2], xmax = c.bounds[xx * 2 + 1], s0 = 1 << (PB - 1), s1 = s0, s2 = s0;
                const int *k = c.k + xx * c.ksize;
                for (int x = 0; x < xmax; x++) { const unsigned char *p = src + (y * w + x + xmin) * 3; s0 += p[0] * k[x]; s1 += p[1] * k[x]; s2 += p[2] * k[x]; }
                unsigned char *o = tmp + (y * ow + xx) * 3; o[0] = clip8(s0); o[1] = clip8(s1); o[2] = clip8(s2);
            }
        coeffs_free(&c); src = tmp; w = ow;
    }
    if (oh != h) {
        Coeffs c; coeffs(h, oh, &c);
        out = malloc((size_t)w * oh * 3);
        for (int yy = 0; yy < oh; yy++) {
            int ymin = c.bounds[yy * 2], ymax = c.bounds[yy * 2 + 1];
            const int *k = c.k + yy * c.ksize;
            for (int x = 0; x < w; x++) {
                int s0 = 1 << (PB - 1), s1 = s0, s2 = s0;
                for (int y = 0; y < ymax; y++) { const unsigned char *p = src + ((y + ymin) * w + x) * 3; s0 += p[0] * k[y]; s1 += p[1] * k[y]; s2 += p[2] * k[y]; }
                unsigned char *o = out + (yy * w + x) * 3; o[0] = clip8(s0); o[1] = clip8(s1); o[2] = clip8(s2);
            }
        }
        coeffs_free(&c);
    } else {
        out = malloc((size_t)w * h * 3); memcpy(out, src, (size_t)w * h * 3);
    }
    free(tmp);
    return out;
}

/* main's _pose_crop + estimate() preprocessing; returns 0 if the crop is empty */
static int main_pose_tensor(const unsigned char *Y, int ys, const unsigned char *UV, int uvs, gint W, gint H, const Box *f, float *t) {
    int x1 = (int)rint(f->x1), y1 = (int)rint(f->y1), x2 = (int)rint(f->x2), y2 = (int)rint(f->y2);
    int bw = x2 - x1 > 1 ? x2 - x1 : 1, bh = y2 - y1 > 1 ? y2 - y1 : 1;
    int px = (int)rint(pose_context * bw), py = (int)rint(pose_context * bh);
    int left = x1 - px > 0 ? x1 - px : 0, top = y1 - py > 0 ? y1 - py : 0;
    int right = x2 + px < W ? x2 + px : W, bottom = y2 + py < H ? y2 + py : H;
    int cw = right - left, ch = bottom - top;
    if (cw <= 0 || ch <= 0) return 0;
    unsigned char *rgb = malloc((size_t)cw * ch * 3);
    nv12_to_rgb(Y, ys, UV, uvs, left, top, right, bottom, rgb);
    int rw, rh;
    if (cw <= ch) { rw = 256; rh = (int)rint(256.0 * ch / cw); if (rh < 256) rh = 256; }
    else { rh = 256; rw = (int)rint(256.0 * cw / ch); if (rw < 256) rw = 256; }
    unsigned char *res = pil_resize(rgb, cw, ch, rw, rh);
    int cl = (int)rint((rw - 224) / 2.0), ct = (int)rint((rh - 224) / 2.0);
    const float mean[3] = {0.485f, 0.456f, 0.406f}, stdv[3] = {0.229f, 0.224f, 0.225f};
    for (int y = 0; y < 224; y++)
        for (int x = 0; x < 224; x++) {
            const unsigned char *p = res + ((y + ct) * rw + (x + cl)) * 3;
            for (int ch3 = 0; ch3 < 3; ch3++) t[ch3 * 224 * 224 + y * 224 + x] = ((float)p[ch3] / 255.0f - mean[ch3]) / stdv[ch3];
        }
    free(rgb); free(res);
    return 1;
}

static int infer_pose(DirectCtx *c, float *input, double *m) {
    if (!c->req && ov_compiled_model_create_infer_request(pd_model, &c->req) != OK) return 0;
    int64_t dims[4] = {1, 3, 224, 224};
    ov_shape_t shape; ov_tensor_t *in = 0, *out = 0; void *data = 0;
    if (ov_shape_create(4, dims, &shape) != OK) return 0;
    int ok = ov_tensor_create_from_host_ptr(F32, shape, input, &in) == OK
             && ov_infer_request_set_input_tensor_by_index(c->req, 0, in) == OK
             && ov_infer_request_infer(c->req) == OK
             && ov_infer_request_get_output_tensor_by_index(c->req, 0, &out) == OK
             && ov_tensor_data(out, &data) == OK;
    if (ok) for (int k = 0; k < 9; k++) m[k] = ((float *)data)[k];
    if (out) ov_tensor_free(out);
    if (in) ov_tensor_free(in);
    ov_shape_free(&shape);
    return ok;
}

static int direct_pose_probe(void *pad, ProbeInfo *info, void *data) {
    DirectCtx *c = data;
    frame_size(pad, (PoseCtx *)c);  /* same leading fields W, H */
    void *buffer = c->record ? info->data : gst_mini_object_make_writable(info->data);
    info->data = buffer;
    Box b[MAXB];
    int n = read_boxes(buffer, b), any = 0;
    for (int i = 0; i < n; i++)
        if (b[i].face && gst_buffer_get_video_region_of_interest_meta_id(buffer, (gint)b[i].id)) { any = 1; break; }
    if (any && c->W > 0) {
        struct timespec t0, t1; clock_gettime(CLOCK_MONOTONIC, &t0);
        void *vm = gst_buffer_get_video_meta(buffer);
        unsigned char info_y[256], info_uv[256];
        gpointer py = 0, puv = 0; gint sy = 0, suv = 0;
        if (vm && gst_video_meta_map(vm, 0, info_y, &py, &sy, 1)) {
            if (gst_video_meta_map(vm, 1, info_uv, &puv, &suv, 1)) {
                struct timespec tt = t0;
                __atomic_add_fetch(&pd_map_us, us_since(&tt), __ATOMIC_RELAXED);
                __atomic_add_fetch(&pd_frames_mapped, 1, __ATOMIC_RELAXED);
                float *tensor = malloc(sizeof(float) * 3 * 224 * 224);
                for (int i = 0; i < n; i++) {
                    if (!b[i].face || !gst_buffer_get_video_region_of_interest_meta_id(buffer, (gint)b[i].id)) continue;
                    __atomic_add_fetch(&p_faces, 1, __ATOMIC_RELAXED);
                    double m[9], pitch = 0, yaw = 0, roll = 0;
                    const char *decision; int reject = 0;
                    int prepared = main_pose_tensor(py, sy, puv, suv, c->W, c->H, &b[i], tensor);
                    __atomic_add_fetch(&pd_prep_us, us_since(&tt), __ATOMIC_RELAXED);
                    int inferred = prepared && infer_pose(c, tensor, m);
                    __atomic_add_fetch(&pd_infer_us, us_since(&tt), __ATOMIC_RELAXED);
                    if (!inferred) {
                        decision = "missing"; __atomic_add_fetch(&p_missing, 1, __ATOMIC_RELAXED);
                    } else if (!rotation_to_euler(m, &pitch, &yaw, &roll)) {
                        decision = "head_pose_invalid"; reject = 1; __atomic_add_fetch(&p_invalid, 1, __ATOMIC_RELAXED);
                    } else if (fabs(pitch) > max_pitch || fabs(yaw) > max_yaw || fabs(roll) > max_roll) {
                        decision = "head_pose_out_of_range"; reject = 1; __atomic_add_fetch(&p_range, 1, __ATOMIC_RELAXED);
                    } else { decision = "kept"; __atomic_add_fetch(&p_kept, 1, __ATOMIC_RELAXED); }
                    __atomic_add_fetch(&p_posed, 1, __ATOMIC_RELAXED);
                    if (c->rec) fprintf(c->rec, "%ld %u %d %d %d %d %.4f %.4f %.4f %s\n", c->frame, b[i].id, (int)b[i].x1, (int)b[i].y1,
                                        (int)(b[i].x2 - b[i].x1), (int)(b[i].y2 - b[i].y1), pitch, yaw, roll, decision);
                    if (reject && !c->record) remove_box(buffer, b[i].id);  /* main: no embedding for this face */
                }
                free(tensor);
                gst_video_meta_unmap(vm, 1, info_uv);
            }
            gst_video_meta_unmap(vm, 0, info_y);
        }
        clock_gettime(CLOCK_MONOTONIC, &t1);
        __atomic_add_fetch(&pd_us, (long)((t1.tv_sec - t0.tv_sec) * 1000000L + (t1.tv_nsec - t0.tv_nsec) / 1000), __ATOMIC_RELAXED);
    }
    c->frame++;
    return PROBE_OK;
}

/* model: SixDRepNet IR (main's best_a0_fp16.xml); device "GPU" or "NPU" (NPU with f16, as main). record_path: test only. */
int cgates_attach_pose_direct(void *pad, const char *model, const char *device, const char *record_path) {
    pose_limits();
    if (!pd_model) {
        ov_model_t *mdl = 0;
        if (ov_core_create(&pd_core) != OK || ov_core_read_model(pd_core, model, 0, &mdl) != OK) return 0;
        int st = strncmp(device, "NPU", 3) == 0
            ? ov_core_compile_model(pd_core, mdl, device, 4, &pd_model, "PERFORMANCE_HINT", "LATENCY", "INFERENCE_PRECISION_HINT", "f16")
            : ov_core_compile_model(pd_core, mdl, device, 2, &pd_model, "PERFORMANCE_HINT", "LATENCY");
        ov_model_free(mdl);
        if (st != OK) { pd_model = 0; return 0; }
    }
    DirectCtx *c = calloc(1, sizeof(DirectCtx));
    if (record_path) { c->rec = fopen(record_path, "w"); c->record = 1; }
    gst_pad_add_probe(pad, PROBE_TYPE_BUFFER, direct_pose_probe, c, 0);
    return 1;
}
void cgates_pose_direct_counts(long *out) { out[0] = pd_frames_mapped; out[1] = pd_us; }
void cgates_pose_direct_times(long *out) { out[0] = pd_map_us; out[1] = pd_prep_us; out[2] = pd_infer_us; out[3] = pd_frames_mapped; out[4] = p_posed; }

/* ---------------- real removal for vehicle dedup ----------------
   DL Streamer rebuilds a box's region meta from the analytics relation meta, so removing only the region meta (as
   gstgva remove_region does) leaves the box for gvatrack and gvametaconvert. Right after vehicle detection the relation
   meta holds class mtds and one OD mtd per box (related to the class mtd), so when dedup removes boxes the relation
   meta is rebuilt with only the kept ones: same mtds, same relations, region metas renumbered to the new ids.
   Anything else in the meta: left unchanged and counted (dedup_rebuild_skipped). */
typedef struct { guint id; void *meta; } AMtd;
extern GType gst_analytics_mtd_get_mtd_type(const AMtd *handle);
extern GType gst_analytics_cls_mtd_get_mtd_type(void);
extern unsigned long gst_analytics_cls_mtd_get_length(const AMtd *handle);
extern float gst_analytics_cls_mtd_get_level(const AMtd *handle, unsigned long index);
extern GQuark gst_analytics_cls_mtd_get_quark(const AMtd *handle, unsigned long index);
extern int gst_analytics_relation_meta_add_cls_mtd(void *meta, unsigned long length, float *levels, GQuark *quarks, AMtd *out);
extern int gst_analytics_od_mtd_get_oriented_location(const AMtd *handle, gint *x, gint *y, gint *w, gint *h, float *r, float *conf);
extern int gst_analytics_relation_meta_add_oriented_od_mtd(void *meta, GQuark type, gint x, gint y, gint w, gint h, float r, float conf, AMtd *out);
extern int gst_analytics_relation_meta_get_relation(void *meta, guint a, guint b);
extern int gst_analytics_relation_meta_set_relation(void *meta, int type, guint a, guint b);
extern void *gst_buffer_add_analytics_relation_meta(void *buffer);
static volatile long d_rebuilt, d_rebuild_skipped;

typedef struct { guint id; int is_cls; GQuark q; gint x, y, w, h; float r, conf; unsigned long n; float lv[64]; GQuark qs[64]; RoiMeta *roi; guint nid;
                 int is_trk; unsigned long long tid, first, last; int lost; } Keep;
extern GType gst_analytics_tracking_mtd_get_mtd_type(void);
extern int gst_analytics_tracking_mtd_get_info(const AMtd *handle, unsigned long long *id, unsigned long long *first, unsigned long long *last, int *lost);
extern int gst_analytics_relation_meta_add_tracking_mtd(void *meta, unsigned long long id, unsigned long long first, AMtd *out);
extern int gst_analytics_tracking_mtd_update_last_seen(AMtd *handle, unsigned long long last);
extern int gst_analytics_tracking_mtd_set_lost(AMtd *handle);

static int rebuild_without(void *buffer, const guint *drop_ids, int ndrop) {
    void *rm = gst_buffer_get_analytics_relation_meta(buffer);
    if (!rm) return 0;
    static __thread Keep k[MAXB + 8];
    int n = 0;
    gpointer state = 0; AMtd m;
    GType od = gst_analytics_od_mtd_get_mtd_type(), cls = gst_analytics_cls_mtd_get_mtd_type(), trk = gst_analytics_tracking_mtd_get_mtd_type();
    while (gst_analytics_relation_meta_iterate(rm, &state, 0 /* any */, (Mtd *)&m)) {
        if (n >= MAXB + 8) return 0;
        GType t = gst_analytics_mtd_get_mtd_type(&m);
        int dropped = 0;
        for (int d = 0; d < ndrop; d++) dropped |= drop_ids[d] == m.id;
        if (dropped) continue;
        Keep *e = &k[n];
        memset(e, 0, sizeof *e);
        e->id = m.id;
        if (t == cls) {
            e->is_cls = 1; e->n = gst_analytics_cls_mtd_get_length(&m);
            if (e->n > 64) return 0;
            for (unsigned long j = 0; j < e->n; j++) { e->lv[j] = gst_analytics_cls_mtd_get_level(&m, j); e->qs[j] = gst_analytics_cls_mtd_get_quark(&m, j); }
        } else if (t == od) {
            e->q = gst_analytics_od_mtd_get_obj_type((Mtd *)&m);
            gst_analytics_od_mtd_get_oriented_location(&m, &e->x, &e->y, &e->w, &e->h, &e->r, &e->conf);
            e->roi = gst_buffer_get_video_region_of_interest_meta_id(buffer, (gint)m.id);
        } else if (t == trk) {  /* gvatrack's track id entries (after the tracker) */
            e->is_trk = 1;
            if (!gst_analytics_tracking_mtd_get_info(&m, &e->tid, &e->first, &e->last, &e->lost)) return 0;
        } else {
            return 0;  /* unexpected mtd type here: do not rebuild */
        }
        n++;
    }
    int rel[MAXB + 8][8]; int nrel[MAXB + 8]; guint relto[MAXB + 8][8];  /* relations among kept mtds (few per mtd) */
    for (int a = 0; a < n; a++) {
        nrel[a] = 0;
        for (int b = 0; b < n; b++) {
            if (a == b) continue;
            int r = gst_analytics_relation_meta_get_relation(rm, k[a].id, k[b].id);
            if (r) { if (nrel[a] >= 8) return 0; rel[a][nrel[a]] = r; relto[a][nrel[a]] = (guint)b; nrel[a]++; }
        }
    }
    gst_buffer_remove_meta(buffer, rm);
    void *nm = gst_buffer_add_analytics_relation_meta(buffer);
    if (!nm) return -1;
    for (int a = 0; a < n; a++) {
        AMtd out;
        int ok;
        if (k[a].is_trk) {
            ok = gst_analytics_relation_meta_add_tracking_mtd(nm, k[a].tid, k[a].first, &out);
            if (ok) { gst_analytics_tracking_mtd_update_last_seen(&out, k[a].last); if (k[a].lost) gst_analytics_tracking_mtd_set_lost(&out); }
        } else ok = k[a].is_cls ? gst_analytics_relation_meta_add_cls_mtd(nm, k[a].n, k[a].lv, k[a].qs, &out)
                                : gst_analytics_relation_meta_add_oriented_od_mtd(nm, k[a].q, k[a].x, k[a].y, k[a].w, k[a].h, k[a].r, k[a].conf, &out);
        if (!ok) return -1;
        k[a].nid = out.id;
    }
    for (int a = 0; a < n; a++)
        for (int j = 0; j < nrel[a]; j++) gst_analytics_relation_meta_set_relation(nm, rel[a][j], k[a].nid, k[relto[a][j]].nid);
    for (int a = 0; a < n; a++)
        if (k[a].roi) k[a].roi->id = (gint)k[a].nid;
    return 1;
}

static int dedup_real_probe(void *pad, ProbeInfo *info, void *data) {
    Box b[MAXB];
    char drop[MAXB];
    guint ids[MAXB];
    int removed = 0;
    void *buffer = gst_mini_object_make_writable(info->data);
    info->data = buffer;
    int n = read_boxes(buffer, b);
    int nv = decide_dedup(b, n, drop);
    for (int i = 0; i < n; i++)
        if (drop[i]) { remove_box(buffer, b[i].id); ids[removed++] = b[i].id; }
    if (removed) {
        int r = rebuild_without(buffer, ids, removed);
        __atomic_add_fetch(r > 0 ? &d_rebuilt : &d_rebuild_skipped, 1, __ATOMIC_RELAXED);
    }
    __atomic_add_fetch(&n_vehicles, nv, __ATOMIC_RELAXED);
    __atomic_add_fetch(&n_dedup, removed, __ATOMIC_RELAXED);
    __atomic_add_fetch(&n_frames_dedup, 1, __ATOMIC_RELAXED);
    return PROBE_OK;
}
/* vehicle dedup that really removes the boxes (use instead of cgates_attach_dedup) */
void cgates_attach_dedup_real(void *pad) { gst_pad_add_probe(pad, PROBE_TYPE_BUFFER, dedup_real_probe, 0, 0); }
void cgates_dedup_rebuild_counts(long *out) { out[0] = d_rebuilt; out[1] = d_rebuild_skipped; }

/* ---------------- C reader for the integration (integrate5.py) ----------------
   Replaces the engine's Python reader threads (one GIL for all cameras). Per camera, pad probes write into one shared
   memory block that the camera's event process reads:
     header (1024 bytes, int64): [0] main_seq [1] fire_seq [2] pix_seq [3] frames_in [4] last_in_mono_ns
                                 [5] json_too_big [6] pix_too_big [7] base_time_ns (set by Python after PLAYING)
                                 [8] fire_too_big [9] no_json
     pix ring:  PS slots x (64-byte slot header {seq, pts, w, h, t_real_ns} + PB bytes of packed NV12)
     main ring: JS slots x (64-byte slot header {seq, pts, t_real_ns, latency_ns, len} + JB bytes of JSON text)
     fire ring: FS slots x (64-byte slot header {seq, pts, t_real_ns, 0, len} + FB bytes of JSON text)
   Writer: slot seq = 0, data, slot fields, slot seq = n (release), then header seq = n. Reader checks slot seq before
   and after copying. Layout sizes are passed in by Python (event_worker_shm.LAYOUT), so both sides agree. */
#define SLOT_HDR 64
typedef struct {
    unsigned char *base; long ps, pb, js, jb, fs, fb;
    long main_seq, fire_seq, pix_seq;
    gint W, H, ys, uvs; long uvoff;  /* NV12 layout of the pix branch (system memory) */
    int ondemand, nheld; void *held[16]; long long held_pts[16]; long held_w; int lock; long req_done[64]; int thread_started;
    void *appsrc;  /* PIX_JPEG_OD: requested frames go to appsrc -> vajpegenc (GPU) instead of being copied */
    int jpeg;  /* pix branch carries GPU-encoded JPEG (vajpegenc): copy the bytes, slot int64 [5] = length */
} Reader;

static inline volatile long long *hdr(Reader *r) { return (volatile long long *)r->base; }
static inline unsigned char *pix_slot(Reader *r, long i) { return r->base + 1024 + i * (SLOT_HDR + r->pb); }
static inline unsigned char *main_slot(Reader *r, long i) { return r->base + 1024 + r->ps * (SLOT_HDR + r->pb) + i * (SLOT_HDR + r->jb); }
static inline unsigned char *fire_slot(Reader *r, long i) {
    return r->base + 1024 + r->ps * (SLOT_HDR + r->pb) + r->js * (SLOT_HDR + r->jb) + i * (SLOT_HDR + r->fb);
}
static inline long long buf_pts(void *buffer) { return *(long long *)((unsigned char *)buffer + 72); }  /* GstBuffer.pts */
static inline long long now_ns(clockid_t c) { struct timespec t; clock_gettime(c, &t); return (long long)t.tv_sec * 1000000000LL + t.tv_nsec; }

typedef struct { unsigned int flags; const struct { GType api; } *info; } MetaHdr;
typedef int (*MetaFunc2)(void *buffer, void **meta, void *data);
extern int gst_buffer_foreach_meta(void *buffer, MetaFunc2 func, void *data);
static int find_json(void *buffer, void **meta, void *data) {
    MetaHdr *m = *meta;
    const char *name = g_type_name(m->info->api);
    if (name && strcmp(name, "GstGVAJSONMetaAPI") == 0) *(const char **)data = *(const char **)((unsigned char *)m + 16);  /* last one wins */
    return 1;
}

static void write_json(Reader *r, unsigned char *(*slot_of)(Reader *, long), long nslots, long cap, long *seqp, int hdr_seq,
                       int hdr_big, void *buffer, int with_latency) {
    const char *msg = 0;
    gst_buffer_foreach_meta(buffer, find_json, &msg);
    if (!msg) { __atomic_add_fetch(&hdr(r)[9], 1, __ATOMIC_RELAXED); return; }
    long len = (long)strlen(msg);
    if (len > cap) { __atomic_add_fetch(&hdr(r)[hdr_big], 1, __ATOMIC_RELAXED); return; }
    long n = ++*seqp;
    unsigned char *s = slot_of(r, n % nslots);
    volatile long long *sh = (volatile long long *)s;
    __atomic_store_n(&sh[0], 0, __ATOMIC_RELEASE);
    memcpy(s + SLOT_HDR, msg, (size_t)len);
    long long pts = buf_pts(buffer);
    sh[1] = pts; sh[2] = now_ns(CLOCK_REALTIME);
    long long base = hdr(r)[7];
    sh[3] = with_latency && base > 0 && pts >= 0 ? now_ns(CLOCK_MONOTONIC) - base - pts : -1;
    sh[4] = len;
    __atomic_store_n(&sh[0], n, __ATOMIC_RELEASE);
    __atomic_store_n(&hdr(r)[hdr_seq], n, __ATOMIC_RELEASE);
}

static int main_probe(void *pad, ProbeInfo *info, void *data) {
    Reader *r = data;
    write_json(r, main_slot, r->js, r->jb, &r->main_seq, 0, 5, info->data, 1);
    return PROBE_OK;
}
static int fire_probe(void *pad, ProbeInfo *info, void *data) {
    Reader *r = data;
    write_json(r, fire_slot, r->fs, r->fb, &r->fire_seq, 1, 8, info->data, 0);
    return PROBE_OK;
}

static int copy_frame(Reader *r, void *buffer, long long pts);
static void copy_jpeg(Reader *r, void *buffer) {  /* TEST (PIX_JPEG): JPEG bytes from the GPU media engine into the next pix slot */
    extern int gst_buffer_map(void *buffer, void *info, int flags);
    extern void gst_buffer_unmap(void *buffer, void *info);
    struct { void *memory; int flags; unsigned char *data; unsigned long size, maxsize; void *user[4]; void *res[4]; } mi;
    if (!gst_buffer_map(buffer, &mi, 1)) return;
    if ((long)mi.size > r->pb) { __atomic_add_fetch(&hdr(r)[6], 1, __ATOMIC_RELAXED); gst_buffer_unmap(buffer, &mi); return; }
    long n = __atomic_add_fetch(&r->pix_seq, 1, __ATOMIC_RELAXED);
    unsigned char *s = pix_slot(r, n % r->ps);
    volatile long long *sh = (volatile long long *)s;
    __atomic_store_n(&sh[0], 0, __ATOMIC_RELEASE);
    memcpy(s + SLOT_HDR, mi.data, mi.size);
    {   /* the VA encoder writes Se = 0 in the SOS header; baseline JPEG requires 63 (libjpeg warns "Invalid SOS parameters") */
        unsigned char *d = s + SLOT_HDR; long lim = (long)mi.size < 8192 ? (long)mi.size : 8192;
        for (long i = 2; i + 8 < lim; i++)
            if (d[i] == 0xFF && d[i + 1] == 0xDA) { long k = i + 5 + 2L * d[i + 4]; if (k + 1 < lim && d[k + 1] == 0) d[k + 1] = 63; break; }
    }
    sh[1] = buf_pts(buffer); sh[2] = r->W; sh[3] = r->H; sh[4] = now_ns(CLOCK_REALTIME); sh[5] = (long long)mi.size;
    gst_buffer_unmap(buffer, &mi);
    __atomic_store_n(&sh[0], n, __ATOMIC_RELEASE);
    __atomic_store_n(&hdr(r)[2], n, __ATOMIC_RELEASE);
    __atomic_add_fetch(&hdr(r)[10], 1, __ATOMIC_RELAXED);
}
void creader_set_jpeg(void *rv) { ((Reader *)rv)->jpeg = 1; }
static int jpeg_out_probe(void *pad, ProbeInfo *info, void *data) { copy_jpeg(data, info->data); return PROBE_OK; }
/* TEST (PIX_JPEG_OD): only the frames the V8 process asks for are JPEG-encoded on the GPU (appsrc -> vajpegenc -> jpeg_pad) */
void creader_set_jpeg_ondemand(void *rv, void *appsrc, void *jpeg_pad) {
    Reader *r = rv;
    r->appsrc = appsrc;
    gst_pad_add_probe(jpeg_pad, PROBE_TYPE_BUFFER, jpeg_out_probe, r, 0);
}
static int pix_probe(void *pad, ProbeInfo *info, void *data) {
    Reader *r = data;
    void *buffer = info->data;
    if (r->W <= 0) {
        void *caps = gst_pad_get_current_caps(pad);
        if (!caps) return PROBE_OK;
        void *st = gst_caps_get_structure(caps, 0);
        gst_structure_get_int(st, "width", &r->W); gst_structure_get_int(st, "height", &r->H);
        r->ys = r->uvs = (r->W + 3) & ~3; r->uvoff = (long)r->ys * r->H;  /* GstVideoInfo default NV12 layout */
        if (r->appsrc) { extern void g_object_set(void *o, const char *first, ...); g_object_set(r->appsrc, "caps", caps, (char *)0); }
        gst_mini_object_unref(caps);
    }
    if (r->jpeg) { copy_jpeg(r, buffer); return PROBE_OK; }
    if (r->ondemand) {  /* keep a reference to the GPU frame only; copied when the camera's V8 process asks for it */
        extern void *gst_mini_object_ref(void *o);
        void *old_buf = 0;
        gst_mini_object_ref(buffer);
        while (__atomic_exchange_n(&r->lock, 1, __ATOMIC_ACQUIRE)) ;
        long i = r->held_w++ % r->nheld;
        old_buf = r->held[i]; r->held[i] = buffer; r->held_pts[i] = buf_pts(buffer);
        __atomic_store_n(&r->lock, 0, __ATOMIC_RELEASE);
        if (old_buf) gst_mini_object_unref(old_buf);
        return PROBE_OK;
    }
    copy_frame(r, buffer, buf_pts(buffer));
    return PROBE_OK;
}

/* copy one NV12 frame (GPU-mapped or system memory) into the next pix slot; 1 if copied */
static int copy_frame(Reader *r, void *buffer, long long pts) {
    long need = (long)r->W * r->H * 3 / 2;
    if (need > r->pb) { __atomic_add_fetch(&hdr(r)[6], 1, __ATOMIC_RELAXED); return 0; }
    long n = __atomic_add_fetch(&r->pix_seq, 1, __ATOMIC_RELAXED);
    unsigned char *s = pix_slot(r, n % r->ps);
    volatile long long *sh = (volatile long long *)s;
    __atomic_store_n(&sh[0], 0, __ATOMIC_RELEASE);
    unsigned char *dst = s + SLOT_HDR;
    void *vm = gst_buffer_get_video_meta(buffer);
    unsigned char iy[256], iuv[256];
    gpointer py = 0, puv = 0; gint sy = 0, suv = 0;
    int ok = 0;
    if (vm && gst_video_meta_map(vm, 0, iy, &py, &sy, 1)) {
        if (gst_video_meta_map(vm, 1, iuv, &puv, &suv, 1)) {
            for (int y = 0; y < r->H; y++) memcpy(dst + (long)y * r->W, (unsigned char *)py + (long)y * sy, (size_t)r->W);
            for (int y = 0; y < r->H / 2; y++) memcpy(dst + (long)r->W * r->H + (long)y * r->W, (unsigned char *)puv + (long)y * suv, (size_t)r->W);
            gst_video_meta_unmap(vm, 1, iuv); ok = 1;
        }
        gst_video_meta_unmap(vm, 0, iy);
    } else {
        extern int gst_buffer_map(void *buffer, void *info, int flags);
        extern void gst_buffer_unmap(void *buffer, void *info);
        struct { void *memory; int flags; unsigned char *data; unsigned long size, maxsize; void *user[4]; void *res[4]; } mi;
        if (gst_buffer_map(buffer, &mi, 1)) {
            if ((long)mi.size >= r->uvoff + (long)r->uvs * (r->H / 2)) {
                for (int y = 0; y < r->H; y++) memcpy(dst + (long)y * r->W, mi.data + (long)y * r->ys, (size_t)r->W);
                for (int y = 0; y < r->H / 2; y++) memcpy(dst + (long)r->W * r->H + (long)y * r->W, mi.data + r->uvoff + (long)y * r->uvs, (size_t)r->W);
                ok = 1;
            }
            gst_buffer_unmap(buffer, &mi);
        }
    }
    if (!ok) return 0;
    sh[1] = pts; sh[2] = r->W; sh[3] = r->H; sh[4] = now_ns(CLOCK_REALTIME); sh[5] = 0;
    __atomic_store_n(&sh[0], n, __ATOMIC_RELEASE);
    __atomic_store_n(&hdr(r)[2], n, __ATOMIC_RELEASE);
    __atomic_add_fetch(&hdr(r)[10], 1, __ATOMIC_RELAXED);  /* frames copied */
    return 1;
}

/* on-demand service: requests at header int64 [16 + 2*i] = request seq, [17 + 2*i] = pts (-1 = newest held frame),
   i = seq % 32. Served by copying that held frame into the pix ring (same slots the V8 process already reads). */
#include <pthread.h>
#define NREQ 32
static void *ondemand_thread(void *arg) {
    Reader *r = arg;
    extern void *gst_mini_object_ref(void *o);
    while (1) {
        int did = 0;
        for (int i = 0; i < NREQ; i++) {
            long long seq = __atomic_load_n(&hdr(r)[16 + 2 * i], __ATOMIC_ACQUIRE);
            if (seq <= r->req_done[i]) continue;
            long long want = hdr(r)[17 + 2 * i];
            r->req_done[i] = seq; did = 1;
            void *buf = 0; long long pts = -1;
            while (__atomic_exchange_n(&r->lock, 1, __ATOMIC_ACQUIRE)) ;
            long best = -1;
            for (int k = 0; k < r->nheld; k++) {
                if (!r->held[k]) continue;
                if (want >= 0 ? r->held_pts[k] == want : (best < 0 || r->held_pts[k] > r->held_pts[best])) { best = k; if (want >= 0) break; }
            }
            if (best >= 0) { buf = gst_mini_object_ref(r->held[best]); pts = r->held_pts[best]; }
            __atomic_store_n(&r->lock, 0, __ATOMIC_RELEASE);
            if (!buf) { __atomic_add_fetch(&hdr(r)[11], 1, __ATOMIC_RELAXED); continue; }  /* requested frame no longer held */
            if (r->appsrc) {  /* GPU JPEG of this frame; jpeg_out_probe puts the bytes in the pix ring */
                extern void g_signal_emit_by_name(void *instance, const char *signal, ...);
                int flow = 0;
                g_signal_emit_by_name(r->appsrc, "push-buffer", buf, &flow);
            } else copy_frame(r, buf, pts);
            gst_mini_object_unref(buf);
        }
        if (!did) { struct timespec ts = {0, 500000}; nanosleep(&ts, 0); }
    }
    return 0;
}
void creader_set_ondemand(void *rv, int nheld) {
    Reader *r = rv;
    r->ondemand = 1; r->nheld = nheld > 16 ? 16 : nheld < 2 ? 2 : nheld;
    if (!r->thread_started) { pthread_t t; pthread_create(&t, 0, ondemand_thread, r); pthread_detach(t); r->thread_started = 1; }
}

static int in_probe(void *pad, ProbeInfo *info, void *data) {  /* frames entering (replaces the Python count_in probe) */
    Reader *r = data;
    __atomic_add_fetch(&hdr(r)[3], 1, __ATOMIC_RELAXED);
    __atomic_store_n(&hdr(r)[4], now_ns(CLOCK_MONOTONIC), __ATOMIC_RELAXED);
    return PROBE_OK;
}

void *creader_new(void *base, long ps, long pb, long js, long jb, long fs, long fb) {
    Reader *r = calloc(1, sizeof(Reader));
    r->base = base; r->ps = ps; r->pb = pb; r->js = js; r->jb = jb; r->fs = fs; r->fb = fb;
    return r;
}
void creader_attach(void *r, void *main_pad, void *pix_pad, void *fire_pad) {
    gst_pad_add_probe(main_pad, PROBE_TYPE_BUFFER, main_probe, r, 0);
    if (pix_pad) gst_pad_add_probe(pix_pad, PROBE_TYPE_BUFFER, pix_probe, r, 0);
    gst_pad_add_probe(fire_pad, PROBE_TYPE_BUFFER, fire_probe, r, 0);
}
void creader_attach_in(void *r, void *pad) { gst_pad_add_probe(pad, PROBE_TYPE_BUFFER, in_probe, r, 0); }

/* ---------------- face path with ONE SCRFD: landmarks from the detector, alignment + AdaFace in C ----------------
   The detector scrfd_top1lm_u8 (facecmp/prepare_top1lm.py) gives per person the TIGHT face box ("face") and its 5
   landmarks as small boxes "lm0".."lm4" (gvadetect maps them to frame pixels). Per camera (FaceCtx):
   1. cface_attach_landmarks (pad right after that gvadetect): for each face, the 5 points = centres of the nearest
      lm boxes of each label near the face; stored with the face box and its person box (by buffer pts); the lm boxes
      are removed (relation meta rebuilt when possible, else region meta only). The size + head-pose gates then see the
      tight box, like main.
   2. cface_attach_embed (pad after the head-pose gate; replaces gvaclassify face_align_u8): for each face still
      present: similarity transform landmarks -> main's 112x112 template (least squares), warp from the frame (NV12 ->
      BGR, bilinear, zero outside the person box as main warps the person crop), main's AdaFace input
      ((v/255-0.5)/0.5, BGR, CHW), AdaFace on the NPU (OpenVINO C API), L2 -> 512 values; stored by pts.
   3. cface_attach_json (gvametaconvert src pad): adds "cfaces":[{x,y,w,h,conf,lm:[10],emb:[512]}] to the frame's JSON. */
extern char *g_strdup(const char *s);
extern void g_free(void *p);
extern void *g_malloc(unsigned long n);
#define FRING 32
#define FMAX 16
typedef struct { gint x, y, w, h; float conf; float lm[10]; gint px1, py1, px2, py2; int has_lm; float emb[512]; int has_emb; } FaceItem;
typedef struct { long long pts; int n; FaceItem f[FMAX]; } FaceFrame;
typedef struct { FaceFrame ring[FRING]; long w; int lock; gint W, H; ov_infer_request_t *req; } FaceCtx;
static ov_compiled_model_t *ae_model;
static volatile long cf_faces, cf_lm_ok, cf_lm_removed, cf_rebuild_skipped, cf_embedded, cf_no_lm, cf_json, cf_us_embed;
static const float TPL[10] = {38.2946f, 51.6963f, 73.5318f, 51.5014f, 56.0252f, 71.7366f, 41.5493f, 92.3655f, 70.7299f, 92.2041f};

static inline void flock_(FaceCtx *c) { while (__atomic_exchange_n(&c->lock, 1, __ATOMIC_ACQUIRE)) ; }
static inline void funlock_(FaceCtx *c) { __atomic_store_n(&c->lock, 0, __ATOMIC_RELEASE); }
static FaceFrame *ff_get(FaceCtx *c, long long pts) {
    for (int i = 0; i < FRING; i++) if (c->ring[i].pts == pts && c->ring[i].n >= 0) return &c->ring[i];
    return 0;
}

typedef struct { guint id; double x1, y1, x2, y2; float conf; int kind; } LBox;  /* kind: 0 face, 1..5 lm0..lm4, 6 person, -1 other */
static int read_lboxes(void *buffer, LBox *b) {
    void *rm = gst_buffer_get_analytics_relation_meta(buffer);
    if (!rm) return 0;
    gpointer state = 0; Mtd mtd; int n = 0;
    GType od = gst_analytics_od_mtd_get_mtd_type();
    while (n < MAXB && gst_analytics_relation_meta_iterate(rm, &state, od, &mtd)) {
        gint x = 0, y = 0, w = 0, h = 0; float lc = 0, conf = 0;
        gst_analytics_od_mtd_get_location(&mtd, &x, &y, &w, &h, &lc);
        gst_analytics_od_mtd_get_confidence_lvl(&mtd, &conf);
        const char *l = g_quark_to_string(gst_analytics_od_mtd_get_obj_type(&mtd));
        b[n].id = mtd.id; b[n].x1 = x; b[n].y1 = y; b[n].x2 = (double)x + w; b[n].y2 = (double)y + h; b[n].conf = conf;
        b[n].kind = !l ? -1 : strcmp(l, "face") == 0 ? 0 : (strncmp(l, "lm", 2) == 0 && l[2] >= '0' && l[2] <= '4' && !l[3]) ? 1 + (l[2] - '0')
                  : strcmp(l, "person") == 0 ? 6 : -1;
        n++;
    }
    return n;
}

static int landmarks_probe(void *pad, ProbeInfo *info, void *data) {
    FaceCtx *c = data;
    void *buffer = gst_mini_object_make_writable(info->data);
    info->data = buffer;
    LBox b[MAXB]; guint drop[MAXB]; int nd = 0;
    int n = read_lboxes(buffer, b);
    long long pts = buf_pts(buffer);
    flock_(c);
    FaceFrame *fr = &c->ring[c->w++ % FRING];
    fr->pts = pts; fr->n = 0;
    for (int i = 0; i < n; i++) {
        if (b[i].kind != 0 || fr->n >= FMAX) continue;
        FaceItem *f = &fr->f[fr->n++];
        memset(f, 0, sizeof *f);
        f->x = (gint)b[i].x1; f->y = (gint)b[i].y1; f->w = (gint)(b[i].x2 - b[i].x1); f->h = (gint)(b[i].y2 - b[i].y1); f->conf = b[i].conf;
        double cx = (b[i].x1 + b[i].x2) / 2, cy = (b[i].y1 + b[i].y2) / 2, fw = b[i].x2 - b[i].x1, fh = b[i].y2 - b[i].y1;
        /* person box: smallest person containing the face centre (main warps the person crop) */
        double best_area = 1e18; f->px1 = 0; f->py1 = 0; f->px2 = -1; f->py2 = -1;
        for (int j = 0; j < n; j++) {
            if (b[j].kind != 6 || cx < b[j].x1 || cx > b[j].x2 || cy < b[j].y1 || cy > b[j].y2) continue;
            double a = (b[j].x2 - b[j].x1) * (b[j].y2 - b[j].y1);
            if (a < best_area) { best_area = a; f->px1 = (gint)b[j].x1; f->py1 = (gint)b[j].y1; f->px2 = (gint)b[j].x2; f->py2 = (gint)b[j].y2; }
        }
        int got = 0;
        for (int k = 1; k <= 5; k++) {
            double bd = 1e18; int bj = -1;
            for (int j = 0; j < n; j++) {
                if (b[j].kind != k) continue;
                double lx = (b[j].x1 + b[j].x2) / 2, ly = (b[j].y1 + b[j].y2) / 2;
                if (fabs(lx - cx) > fw * 1.5 || fabs(ly - cy) > fh * 1.5) continue;
                double d = (lx - cx) * (lx - cx) + (ly - cy) * (ly - cy);
                if (d < bd) { bd = d; bj = j; }
            }
            if (bj < 0) break;
            f->lm[2 * (k - 1)] = (float)((b[bj].x1 + b[bj].x2) / 2); f->lm[2 * (k - 1) + 1] = (float)((b[bj].y1 + b[bj].y2) / 2);
            got++;
        }
        f->has_lm = got == 5 && f->px2 > f->px1;
        __atomic_add_fetch(&cf_faces, 1, __ATOMIC_RELAXED);
        if (f->has_lm) __atomic_add_fetch(&cf_lm_ok, 1, __ATOMIC_RELAXED);
    }
    funlock_(c);
    for (int i = 0; i < n; i++)
        if (b[i].kind >= 1 && b[i].kind <= 5) { remove_box(buffer, b[i].id); drop[nd++] = b[i].id; }
    if (nd) {
        __atomic_add_fetch(&cf_lm_removed, nd, __ATOMIC_RELAXED);
        if (rebuild_without(buffer, drop, nd) <= 0) __atomic_add_fetch(&cf_rebuild_skipped, 1, __ATOMIC_RELAXED);
    }
    return PROBE_OK;
}

/* least-squares similarity src(5 pts) -> TPL: [x';y'] = [a -b; b a][x;y] + t ; returns M (2x3) */
static void similarity(const float *p, double *M) {
    double mx = 0, my = 0, tx = 0, ty = 0;
    for (int i = 0; i < 5; i++) { mx += p[2 * i]; my += p[2 * i + 1]; tx += TPL[2 * i]; ty += TPL[2 * i + 1]; }
    mx /= 5; my /= 5; tx /= 5; ty /= 5;
    double num_a = 0, num_b = 0, den = 0;
    for (int i = 0; i < 5; i++) {
        double ax = p[2 * i] - mx, ay = p[2 * i + 1] - my, bx = TPL[2 * i] - tx, by = TPL[2 * i + 1] - ty;
        num_a += ax * bx + ay * by; num_b += ax * by - ay * bx; den += ax * ax + ay * ay;
    }
    double a = den > 0 ? num_a / den : 1, bb = den > 0 ? num_b / den : 0;
    M[0] = a; M[1] = -bb; M[2] = tx - (a * mx - bb * my);
    M[3] = bb; M[4] = a; M[5] = ty - (bb * mx + a * my);
}

static inline void bgr_at(const unsigned char *Y, int ys, const unsigned char *UV, int uvs, int x, int y, int *o) {
    const int CY = 1220542, CUB = 2116026, CUG = -409993, CVG = -852492, CVR = 1673527, SH = 20, HALF = 1 << (SH - 1);
    int yy = Y[y * ys + x] - 16; if (yy < 0) yy = 0;
    const unsigned char *cc = UV + (y / 2) * uvs + (x / 2) * 2;
    int u = cc[0] - 128, v = cc[1] - 128, y00 = yy * CY;
    o[2] = sat8((y00 + HALF + CVR * v) >> SH); o[1] = sat8((y00 + HALF + CVG * v + CUG * u) >> SH); o[0] = sat8((y00 + HALF + CUB * u) >> SH);
}

/* warp to 112x112 (cv2.warpAffine INTER_LINEAR semantics, constant 0 outside the person box) and AdaFace tensor */
static void aligned_tensor(const unsigned char *Y, int ys, const unsigned char *UV, int uvs, const FaceItem *f, gint W, gint H, float *t) {
    double M[6]; similarity(f->lm, M);
    double det = M[0] * M[4] - M[1] * M[3];
    double i0 = M[4] / det, i1 = -M[1] / det, i3 = -M[3] / det, i4 = M[0] / det;
    double i2 = -(i0 * M[2] + i1 * M[5]), i5 = -(i3 * M[2] + i4 * M[5]);
    int bx1 = f->px1 < 0 ? 0 : f->px1, by1 = f->py1 < 0 ? 0 : f->py1, bx2 = f->px2 > W ? W : f->px2, by2 = f->py2 > H ? H : f->py2;
    for (int y = 0; y < 112; y++)
        for (int x = 0; x < 112; x++) {
            double sx = i0 * x + i1 * y + i2, sy = i3 * x + i4 * y + i5;
            int x0 = (int)floor(sx), y0 = (int)floor(sy);
            double fx = sx - x0, fy = sy - y0, acc[3] = {0, 0, 0};
            for (int dy = 0; dy < 2; dy++)
                for (int dx = 0; dx < 2; dx++) {
                    int xx = x0 + dx, yy = y0 + dy;
                    double wgt = (dx ? fx : 1 - fx) * (dy ? fy : 1 - fy);
                    if (xx < bx1 || xx >= bx2 || yy < by1 || yy >= by2 || wgt == 0) continue;
                    int px[3]; bgr_at(Y, ys, UV, uvs, xx, yy, px);
                    acc[0] += wgt * px[0]; acc[1] += wgt * px[1]; acc[2] += wgt * px[2];
                }
            for (int ch = 0; ch < 3; ch++) {
                double v = rint(acc[ch]); if (v < 0) v = 0; if (v > 255) v = 255;
                t[ch * 112 * 112 + y * 112 + x] = (float)((v / 255.0 - 0.5) / 0.5);
            }
        }
}

static int infer_face(FaceCtx *c, float *input, float *emb) {
    if (!c->req && ov_compiled_model_create_infer_request(ae_model, &c->req) != OK) return 0;
    int64_t dims[4] = {1, 3, 112, 112};
    ov_shape_t shape; ov_tensor_t *in = 0, *out = 0; void *data = 0;
    if (ov_shape_create(4, dims, &shape) != OK) return 0;
    int ok = ov_tensor_create_from_host_ptr(F32, shape, input, &in) == OK && ov_infer_request_set_input_tensor_by_index(c->req, 0, in) == OK
             && ov_infer_request_infer(c->req) == OK && ov_infer_request_get_output_tensor_by_index(c->req, 0, &out) == OK
             && ov_tensor_data(out, &data) == OK;
    if (ok) {
        double s = 0;
        for (int k = 0; k < 512; k++) { emb[k] = ((float *)data)[k]; s += (double)emb[k] * emb[k]; }
        s = sqrt(s); if (s < 1e-12) ok = 0; else for (int k = 0; k < 512; k++) emb[k] = (float)(emb[k] / s);
    }
    if (out) ov_tensor_free(out);
    if (in) ov_tensor_free(in);
    ov_shape_free(&shape);
    return ok;
}

static int embed_probe(void *pad, ProbeInfo *info, void *data) {
    FaceCtx *c = data;
    if (c->W <= 0) {
        void *caps = gst_pad_get_current_caps(pad);
        if (caps) { void *st = gst_caps_get_structure(caps, 0); gst_structure_get_int(st, "width", &c->W); gst_structure_get_int(st, "height", &c->H); gst_mini_object_unref(caps); }
    }
    void *buffer = info->data;
    LBox b[MAXB];
    int n = read_lboxes(buffer, b);
    long long pts = buf_pts(buffer);
    int todo = 0;
    for (int i = 0; i < n; i++) if (b[i].kind == 0 && gst_buffer_get_video_region_of_interest_meta_id(buffer, (gint)b[i].id)) todo++;
    if (!todo || c->W <= 0) return PROBE_OK;
    flock_(c);
    FaceFrame *fr = ff_get(c, pts);
    FaceFrame copy; if (fr) copy = *fr;
    funlock_(c);
    if (!fr) return PROBE_OK;
    struct timespec t0; clock_gettime(CLOCK_MONOTONIC, &t0);
    void *vm = gst_buffer_get_video_meta(buffer);
    unsigned char iy[256], iuv[256]; gpointer py = 0, puv = 0; gint sy = 0, suv = 0;
    if (!vm || !gst_video_meta_map(vm, 0, iy, &py, &sy, 1)) return PROBE_OK;
    if (!gst_video_meta_map(vm, 1, iuv, &puv, &suv, 1)) { gst_video_meta_unmap(vm, 0, iy); return PROBE_OK; }
    float *tensor = malloc(sizeof(float) * 3 * 112 * 112);
    for (int i = 0; i < n; i++) {
        if (b[i].kind != 0 || !gst_buffer_get_video_region_of_interest_meta_id(buffer, (gint)b[i].id)) continue;  /* removed by a gate */
        for (int j = 0; j < copy.n; j++) {
            FaceItem *f = &copy.f[j];
            if (f->x != (gint)b[i].x1 || f->y != (gint)b[i].y1 || f->w != (gint)(b[i].x2 - b[i].x1)) continue;
            if (!f->has_lm) { __atomic_add_fetch(&cf_no_lm, 1, __ATOMIC_RELAXED); break; }
            aligned_tensor(py, sy, puv, suv, f, c->W, c->H, tensor);
            if (infer_face(c, tensor, f->emb)) { f->has_emb = 1; __atomic_add_fetch(&cf_embedded, 1, __ATOMIC_RELAXED); }
            break;
        }
    }
    free(tensor);
    gst_video_meta_unmap(vm, 1, iuv); gst_video_meta_unmap(vm, 0, iy);
    flock_(c);
    fr = ff_get(c, pts);
    if (fr) for (int j = 0; j < copy.n && j < fr->n; j++) if (copy.f[j].has_emb) { memcpy(fr->f[j].emb, copy.f[j].emb, sizeof copy.f[j].emb); fr->f[j].has_emb = 1; }
    funlock_(c);
    __atomic_add_fetch(&cf_us_embed, us_since(&t0), __ATOMIC_RELAXED);
    return PROBE_OK;
}

static int find_json_meta(void *buffer, void **meta, void *data) {
    MetaHdr *m = *meta;
    const char *name = g_type_name(m->info->api);
    if (name && strcmp(name, "GstGVAJSONMetaAPI") == 0) *(void **)data = m;
    return 1;
}

static int json_probe(void *pad, ProbeInfo *info, void *data) {
    FaceCtx *c = data;
    void *buffer = info->data;
    long long pts = buf_pts(buffer);
    flock_(c);
    FaceFrame *fr = ff_get(c, pts);
    int any = 0;
    if (fr) for (int j = 0; j < fr->n; j++) any |= fr->f[j].has_emb;
    if (!any) { funlock_(c); return PROBE_OK; }
    FaceFrame copy = *fr;
    fr->n = -1;  /* used */
    funlock_(c);
    void *meta = 0;
    gst_buffer_foreach_meta(buffer, find_json_meta, &meta);  /* the JSON meta; message pointer at offset 16 (as find_json) */
    if (!meta) return PROBE_OK;
    char **msgp = (char **)((unsigned char *)meta + 16);
    const char *old = *msgp;
    if (!old) return PROBE_OK;
    long len = (long)strlen(old), cap = len + 64 + (long)copy.n * (512 * 12 + 256);
    char *out = g_malloc((unsigned long)cap), *p = out;
    long k = len - 1; while (k > 0 && old[k] != '}') k--;
    memcpy(p, old, (size_t)k); p += k;
    p += sprintf(p, ",\"cfaces\":[");
    int first = 1;
    for (int j = 0; j < copy.n; j++) {
        FaceItem *f = &copy.f[j];
        if (!f->has_emb) continue;
        p += sprintf(p, "%s{\"x\":%d,\"y\":%d,\"w\":%d,\"h\":%d,\"conf\":%.5f,\"lm\":[", first ? "" : ",", f->x, f->y, f->w, f->h, f->conf);
        for (int q = 0; q < 10; q++) p += sprintf(p, "%s%.2f", q ? "," : "", f->lm[q]);
        p += sprintf(p, "],\"emb\":[");
        for (int q = 0; q < 512; q++) p += sprintf(p, "%s%.6f", q ? "," : "", f->emb[q]);
        p += sprintf(p, "]}");
        first = 0;
    }
    p += sprintf(p, "]}");
    *msgp = out;
    g_free((void *)old);
    __atomic_add_fetch(&cf_json, 1, __ATOMIC_RELAXED);
    return PROBE_OK;
}

void *cface_new(void) { FaceCtx *c = calloc(1, sizeof(FaceCtx)); for (int i = 0; i < FRING; i++) c->ring[i].pts = -1; return c; }
void cface_attach_landmarks(void *ctx, void *pad) { gst_pad_add_probe(pad, PROBE_TYPE_BUFFER, landmarks_probe, ctx, 0); }
int cface_attach_embed(void *ctx, void *pad, const char *model, const char *device) {
    if (!ae_model) {
        ov_model_t *mdl = 0;
        if (!pd_core && ov_core_create(&pd_core) != OK) return 0;
        if (ov_core_read_model(pd_core, model, 0, &mdl) != OK) return 0;
        int st = ov_core_compile_model(pd_core, mdl, device, 2, &ae_model, "PERFORMANCE_HINT", "LATENCY");
        ov_model_free(mdl);
        if (st != OK) { ae_model = 0; return 0; }
    }
    gst_pad_add_probe(pad, PROBE_TYPE_BUFFER, embed_probe, ctx, 0);
    return 1;
}
void cface_attach_json(void *ctx, void *pad) { gst_pad_add_probe(pad, PROBE_TYPE_BUFFER, json_probe, ctx, 0); }
void cface_counts(long *out) {  /* faces, with 5 landmarks, lm boxes removed, rebuild skipped, embedded, gate-kept without landmarks, json frames, embed us */
    out[0] = cf_faces; out[1] = cf_lm_ok; out[2] = cf_lm_removed; out[3] = cf_rebuild_skipped; out[4] = cf_embedded; out[5] = cf_no_lm; out[6] = cf_json; out[7] = cf_us_embed;
}

/* ---------------- secondary branch out of the engine (test switch SEC_PROCESS=1) ----------------
   Replaces the two gvapython elements of the secondary branch. csec_attach(ctx, pre_pad, post_pad):
   pre (before vapostproc, full-size VA frame): the frame's boxes (label, confidence, rect normalized) by buffer pts;
   post (after vapostproc, 640x360 BGRx system memory): frame + its boxes into this camera's shared-memory ring.
   Layout: [0] int64 seq, then SS slots: 64-byte header {seq, pts, w, h, nbox, stride_bytes} + 64 boxes x 48 bytes
   {char label[16]; float conf, x, y, w, h} + w*h*4 pixel bytes. The plate process (sec_process.py) reads the newest. */
#define SB_MAX 64
#define SB_RING 16
typedef struct { char label[16]; float conf, x, y, w, h; char pad[12]; } SecBox;
typedef struct { long long pts; int n; SecBox b[SB_MAX]; } SecFrame;
typedef struct { unsigned char *base; long slots, slot_bytes, seq; gint W, H; SecFrame ring[SB_RING]; long w; int lock; } SecCtx;
static volatile long cs_frames, cs_no_boxes, cs_too_big;

static int sec_pre_probe(void *pad, ProbeInfo *info, void *data) {
    SecCtx *c = data;
    if (c->W <= 0) {
        void *caps = gst_pad_get_current_caps(pad);
        if (caps) { void *st = gst_caps_get_structure(caps, 0); gst_structure_get_int(st, "width", &c->W); gst_structure_get_int(st, "height", &c->H); gst_mini_object_unref(caps); }
        if (c->W <= 0) return PROBE_OK;
    }
    void *buffer = info->data;
    void *rm = gst_buffer_get_analytics_relation_meta(buffer);
    while (__atomic_exchange_n(&c->lock, 1, __ATOMIC_ACQUIRE)) ;
    SecFrame *f = &c->ring[c->w++ % SB_RING];
    f->pts = buf_pts(buffer); f->n = 0;
    if (rm) {
        gpointer state = 0; Mtd mtd; GType od = gst_analytics_od_mtd_get_mtd_type();
        while (f->n < SB_MAX && gst_analytics_relation_meta_iterate(rm, &state, od, &mtd)) {
            gint x = 0, y = 0, w = 0, h = 0; float lc = 0, conf = 0;
            gst_analytics_od_mtd_get_location(&mtd, &x, &y, &w, &h, &lc);
            gst_analytics_od_mtd_get_confidence_lvl(&mtd, &conf);
            if (!gst_buffer_get_video_region_of_interest_meta_id(buffer, (gint)mtd.id)) continue;  /* removed by a gate */
            const char *l = g_quark_to_string(gst_analytics_od_mtd_get_obj_type(&mtd));
            SecBox *b = &f->b[f->n++];
            memset(b, 0, sizeof *b);
            if (l) strncpy(b->label, l, 15);
            b->conf = conf; b->x = (float)x / c->W; b->y = (float)y / c->H; b->w = (float)w / c->W; b->h = (float)h / c->H;
        }
    }
    __atomic_store_n(&c->lock, 0, __ATOMIC_RELEASE);
    return PROBE_OK;
}

static int sec_post_probe(void *pad, ProbeInfo *info, void *data) {
    SecCtx *c = data;
    void *buffer = info->data;
    long long pts = buf_pts(buffer);
    SecFrame fr; int found = 0;
    while (__atomic_exchange_n(&c->lock, 1, __ATOMIC_ACQUIRE)) ;
    for (int i = 0; i < SB_RING; i++) if (c->ring[i].pts == pts) { fr = c->ring[i]; found = 1; break; }
    __atomic_store_n(&c->lock, 0, __ATOMIC_RELEASE);
    if (!found) { fr.n = 0; __atomic_add_fetch(&cs_no_boxes, 1, __ATOMIC_RELAXED); }
    void *caps = gst_pad_get_current_caps(pad);
    gint w = 0, h = 0;
    if (caps) { void *st = gst_caps_get_structure(caps, 0); gst_structure_get_int(st, "width", &w); gst_structure_get_int(st, "height", &h); gst_mini_object_unref(caps); }
    long need = 64 + SB_MAX * (long)sizeof(SecBox) + (long)w * h * 4;
    if (w <= 0 || need > c->slot_bytes) { __atomic_add_fetch(&cs_too_big, 1, __ATOMIC_RELAXED); return PROBE_OK; }
    extern int gst_buffer_map(void *buffer, void *info, int flags);
    extern void gst_buffer_unmap(void *buffer, void *info);
    struct { void *memory; int flags; unsigned char *data; unsigned long size, maxsize; void *user[4]; void *res[4]; } mi;
    if (!gst_buffer_map(buffer, &mi, 1)) return PROBE_OK;
    long stride = (long)w * 4;
    void *vm = gst_buffer_get_video_meta(buffer);
    if (vm) { gint *strides = (gint *)((unsigned char *)vm + 64); (void)strides; }  /* default stride w*4 for BGRx */
    if ((long)mi.size < stride * h) { gst_buffer_unmap(buffer, &mi); return PROBE_OK; }
    long n = ++c->seq;
    unsigned char *s = c->base + 64 + (n % c->slots) * c->slot_bytes;
    volatile long long *sh = (volatile long long *)s;
    __atomic_store_n(&sh[0], 0, __ATOMIC_RELEASE);
    memcpy(s + 64, fr.b, (size_t)fr.n * sizeof(SecBox));
    memcpy(s + 64 + SB_MAX * sizeof(SecBox), mi.data, (size_t)(stride * h));
    gst_buffer_unmap(buffer, &mi);
    sh[1] = pts; sh[2] = w; sh[3] = h; sh[4] = fr.n; sh[5] = stride;
    __atomic_store_n(&sh[0], n, __ATOMIC_RELEASE);
    __atomic_store_n((volatile long long *)c->base, n, __ATOMIC_RELEASE);
    __atomic_add_fetch(&cs_frames, 1, __ATOMIC_RELAXED);
    return PROBE_OK;
}

void *csec_new(void *base, long slots, long slot_bytes) {
    SecCtx *c = calloc(1, sizeof(SecCtx));
    c->base = base; c->slots = slots; c->slot_bytes = slot_bytes;
    for (int i = 0; i < SB_RING; i++) c->ring[i].pts = -1;
    return c;
}
void csec_attach(void *ctx, void *pre_pad, void *post_pad) {
    gst_pad_add_probe(pre_pad, PROBE_TYPE_BUFFER, sec_pre_probe, ctx, 0);
    gst_pad_add_probe(post_pad, PROBE_TYPE_BUFFER, sec_post_probe, ctx, 0);
}
void csec_counts(long *out) { out[0] = cs_frames; out[1] = cs_no_boxes; out[2] = cs_too_big; }
