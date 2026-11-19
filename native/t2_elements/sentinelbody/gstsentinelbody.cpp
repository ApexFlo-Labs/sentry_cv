// sentinelbody: exact v8 body ReID (transreid-ssl-v18.1) inside the DL Streamer graph.
//
// Placed after the in-graph BGR conversion (T2-B2). For every tracked person (OD label "person") whose last
// embedding is at least `interval` seconds old (by source PTS) and whose box is at least min-width x min-height,
// it crops the box from the BGR frame exactly like v8 `_crop`, resizes with a bit-exact port of Pillow BILINEAR,
// normalizes like body_reid_model_proc.json and runs the model (measured identical to v8's NPU path: 153/153 crops
// cos 1.0, tests/parity/body_npu_only_result.txt). Results are attached to the SAME buffer as a GstCustomMeta
// "SentinelBodyMeta" { track_ids: bytes(uint64[n]), boxes: bytes(int32[n*4] x1,y1,x2,y2), vectors: bytes(f32[n*dim]) }.
// Python keeps v8's own size / quality gates and evidence crops; it never waits on a model.
#include <gst/gst.h>
#include <gst/base/gstbasetransform.h>
#include <gst/video/video.h>
#include <gst/analytics/analytics.h>
#include <mutex>
#include <string>
#include <unordered_map>
#include <vector>

extern "C" {
void* body_embedder_create(const char* model_path, const char* device, int norm_mode);
void body_embedder_destroy(void* h);
int body_embedder_embed(void* h, const uint8_t* bgr, int W, int H, int stride, int x1, int y1, int x2, int y2,
                        float* out, int out_len);
int body_embedder_embed_many(void* h, const uint8_t* bgr, int W, int H, int stride, const int* boxes, int n,
                             float* out, int out_len, int* status);
}

GST_DEBUG_CATEGORY_STATIC(sentinelbody_debug);
#define GST_CAT_DEFAULT sentinelbody_debug

struct BodySettings {
    std::string model = "/models/surveillance/transreid_ssl_int8.xml";
    std::string device = "NPU";
    double interval = 1.0;
    int min_width = 28, min_height = 56;
    int dim = 384;
    double expiry = 30.0;
    GQuark person = 0;
};
struct BodyStats { guint64 frames = 0, embeds = 0, errors = 0, skipped_small = 0; double infer_seconds = 0; };

typedef struct _GstSentinelBody {
    GstBaseTransform parent;
    BodySettings* s;
    BodyStats* st;
    std::unordered_map<guint64, std::pair<double, double>>* tracks;  // track -> (last embed pts, last seen pts)
    std::mutex* lock;
    void* embedder;
    GstVideoInfo info;
    gboolean have_info;
} GstSentinelBody;
typedef struct _GstSentinelBodyClass { GstBaseTransformClass parent_class; } GstSentinelBodyClass;

#define GST_TYPE_SENTINEL_BODY (gst_sentinel_body_get_type())
#define GST_SENTINEL_BODY(obj) (G_TYPE_CHECK_INSTANCE_CAST((obj), GST_TYPE_SENTINEL_BODY, GstSentinelBody))
G_DEFINE_TYPE(GstSentinelBody, gst_sentinel_body, GST_TYPE_BASE_TRANSFORM)

enum { PROP_0, PROP_MODEL, PROP_DEVICE, PROP_INTERVAL, PROP_MIN_W, PROP_MIN_H, PROP_STATS };

static GstStaticPadTemplate sink_t = GST_STATIC_PAD_TEMPLATE("sink", GST_PAD_SINK, GST_PAD_ALWAYS,
    GST_STATIC_CAPS("video/x-raw, format=(string)BGR"));
static GstStaticPadTemplate src_t = GST_STATIC_PAD_TEMPLATE("src", GST_PAD_SRC, GST_PAD_ALWAYS,
    GST_STATIC_CAPS("video/x-raw, format=(string)BGR"));

static gboolean body_set_caps(GstBaseTransform* t, GstCaps* in, GstCaps*) {
    GstSentinelBody* self = GST_SENTINEL_BODY(t);
    self->have_info = gst_video_info_from_caps(&self->info, in);
    return self->have_info;
}

static gboolean body_start(GstBaseTransform* t) {
    GstSentinelBody* self = GST_SENTINEL_BODY(t);
    std::lock_guard<std::mutex> g(*self->lock);
    if (!self->embedder) {
        self->embedder = body_embedder_create(self->s->model.c_str(), self->s->device.c_str(), 0);
        if (!self->embedder) {
            GST_ELEMENT_ERROR(self, RESOURCE, FAILED, ("sentinelbody: model load failed"), ("%s on %s",
                              self->s->model.c_str(), self->s->device.c_str()));
            return FALSE;
        }
    }
    self->tracks->clear();
    return TRUE;
}

static GstFlowReturn body_transform_ip(GstBaseTransform* t, GstBuffer* buf) {
    GstSentinelBody* self = GST_SENTINEL_BODY(t);
    std::lock_guard<std::mutex> g(*self->lock);
    BodySettings& s = *self->s;
    self->st->frames++;
    const GstClockTime pts = GST_BUFFER_PTS(buf);
    GstAnalyticsRelationMeta* rmeta = gst_buffer_get_analytics_relation_meta(buf);
    if (!rmeta || !GST_CLOCK_TIME_IS_VALID(pts) || !self->have_info) return GST_FLOW_OK;
    const double now = double(pts) / GST_SECOND;

    struct Job { guint64 tid; int x1, y1, x2, y2; };
    std::vector<Job> jobs;
    gpointer state = nullptr;
    GstAnalyticsMtd od;
    while (gst_analytics_relation_meta_iterate(rmeta, &state, gst_analytics_od_mtd_get_mtd_type(), &od)) {
        if (gst_analytics_od_mtd_get_obj_type(&od) != s.person) continue;
        gint x, y, w, h;
        gfloat angle, lconf;
        if (!gst_analytics_od_mtd_get_oriented_location(&od, &x, &y, &w, &h, &angle, &lconf)) continue;
        // Same FIRST-related-track rule as v8's native detection reader.
        gpointer ts = nullptr;
        GstAnalyticsMtd trk;
        bool found = false;
        guint64 tid = 0;
        while (gst_analytics_relation_meta_iterate(rmeta, &ts, gst_analytics_tracking_mtd_get_mtd_type(), &trk)) {
            if (trk.id == od.id || gst_analytics_relation_meta_get_relation(rmeta, od.id, trk.id) ==
                                       GST_ANALYTICS_REL_TYPE_NONE) continue;
            GstClockTime fs, ls;
            gboolean lost;
            found = gst_analytics_tracking_mtd_get_info((GstAnalyticsTrackingMtd*)&trk, &tid, &fs, &ls, &lost);
            break;
        }
        if (!found) continue;
        auto& tr = (*self->tracks)[tid];
        bool fresh = tr.second == 0.0 && tr.first == 0.0;
        tr.second = now;
        if (!fresh && now - tr.first < s.interval) continue;
        if (w < s.min_width || h < s.min_height) { self->st->skipped_small++; continue; }
        jobs.push_back({tid, x, y, x + w, y + h});
    }
    // expire old tracks
    if (self->st->frames % 64 == 0) {
        for (auto it = self->tracks->begin(); it != self->tracks->end();)
            it = (now - it->second.second > s.expiry || now < it->second.second - 1.0) ? self->tracks->erase(it) : ++it;
    }
    if (jobs.empty()) return GST_FLOW_OK;

    GstMapInfo map;
    if (!gst_buffer_map(buf, &map, GST_MAP_READ)) { self->st->errors++; return GST_FLOW_OK; }
    const int W = GST_VIDEO_INFO_WIDTH(&self->info), H = GST_VIDEO_INFO_HEIGHT(&self->info);
    const int stride = GST_VIDEO_INFO_PLANE_STRIDE(&self->info, 0);
    std::vector<guint64> tids;
    std::vector<gint32> boxes;
    std::vector<float> vecs;
    gint64 t0 = g_get_monotonic_time();
    std::vector<int> jb; jb.reserve(jobs.size() * 4);
    for (const Job& j : jobs) jb.insert(jb.end(), {j.x1, j.y1, j.x2, j.y2});
    std::vector<float> all(jobs.size() * s.dim);
    std::vector<int> status(jobs.size(), 1);
    int rc = body_embedder_embed_many(self->embedder, map.data + GST_VIDEO_INFO_PLANE_OFFSET(&self->info, 0), W, H,
                                      stride, jb.data(), (int)jobs.size(), all.data(), s.dim, status.data());
    for (size_t k = 0; k < jobs.size(); ++k) {
        const Job& j = jobs[k];
        if (rc != 0 || status[k] != 0) { self->st->errors++; continue; }
        tids.push_back(j.tid);
        boxes.insert(boxes.end(), {j.x1, j.y1, j.x2, j.y2});
        vecs.insert(vecs.end(), all.begin() + k * s.dim, all.begin() + (k + 1) * s.dim);
        (*self->tracks)[j.tid].first = now;
        self->st->embeds++;
    }
    self->st->infer_seconds += (g_get_monotonic_time() - t0) / 1e6;
    gst_buffer_unmap(buf, &map);
    if (tids.empty()) return GST_FLOW_OK;
    if (!gst_buffer_is_writable(buf)) { self->st->errors++; return GST_FLOW_OK; }
    GstCustomMeta* cm = gst_buffer_add_custom_meta(buf, "SentinelBodyMeta");
    if (!cm) { self->st->errors++; return GST_FLOW_OK; }
    GstStructure* st = gst_custom_meta_get_structure(cm);
    GBytes* b1 = g_bytes_new(tids.data(), tids.size() * sizeof(guint64));
    GBytes* b2 = g_bytes_new(boxes.data(), boxes.size() * sizeof(gint32));
    GBytes* b3 = g_bytes_new(vecs.data(), vecs.size() * sizeof(float));
    gst_structure_set(st, "count", G_TYPE_UINT, (guint)tids.size(), "dim", G_TYPE_INT, s.dim,
                      "track_ids", G_TYPE_BYTES, b1, "boxes", G_TYPE_BYTES, b2, "vectors", G_TYPE_BYTES, b3, nullptr);
    g_bytes_unref(b1); g_bytes_unref(b2); g_bytes_unref(b3);
    return GST_FLOW_OK;
}

static void body_set_property(GObject* o, guint id, const GValue* v, GParamSpec* p) {
    GstSentinelBody* self = GST_SENTINEL_BODY(o);
    std::lock_guard<std::mutex> g(*self->lock);
    switch (id) {
    case PROP_MODEL: self->s->model = g_value_get_string(v); break;
    case PROP_DEVICE: self->s->device = g_value_get_string(v); break;
    case PROP_INTERVAL: self->s->interval = g_value_get_double(v); break;
    case PROP_MIN_W: self->s->min_width = g_value_get_int(v); break;
    case PROP_MIN_H: self->s->min_height = g_value_get_int(v); break;
    default: G_OBJECT_WARN_INVALID_PROPERTY_ID(o, id, p);
    }
}
static void body_get_property(GObject* o, guint id, GValue* v, GParamSpec* p) {
    GstSentinelBody* self = GST_SENTINEL_BODY(o);
    std::lock_guard<std::mutex> g(*self->lock);
    switch (id) {
    case PROP_MODEL: g_value_set_string(v, self->s->model.c_str()); break;
    case PROP_DEVICE: g_value_set_string(v, self->s->device.c_str()); break;
    case PROP_INTERVAL: g_value_set_double(v, self->s->interval); break;
    case PROP_MIN_W: g_value_set_int(v, self->s->min_width); break;
    case PROP_MIN_H: g_value_set_int(v, self->s->min_height); break;
    case PROP_STATS: {
        gchar* txt = g_strdup_printf("sentinelbody, frames=(guint64)%" G_GUINT64_FORMAT ", embeds=(guint64)%" G_GUINT64_FORMAT
                                     ", errors=(guint64)%" G_GUINT64_FORMAT ", skipped_small=(guint64)%" G_GUINT64_FORMAT
                                     ", infer_seconds=(double)%.3f, tracks=(guint)%u",
                                     self->st->frames, self->st->embeds, self->st->errors, self->st->skipped_small,
                                     self->st->infer_seconds, (guint)self->tracks->size());
        g_value_take_string(v, txt);
        break;
    }
    default: G_OBJECT_WARN_INVALID_PROPERTY_ID(o, id, p);
    }
}
static void body_finalize(GObject* o) {
    GstSentinelBody* self = GST_SENTINEL_BODY(o);
    if (self->embedder) body_embedder_destroy(self->embedder);
    delete self->s; delete self->st; delete self->tracks; delete self->lock;
    G_OBJECT_CLASS(gst_sentinel_body_parent_class)->finalize(o);
}
static void gst_sentinel_body_init(GstSentinelBody* self) {
    self->s = new BodySettings();
    self->s->person = g_quark_from_string("person");
    self->st = new BodyStats();
    self->tracks = new std::unordered_map<guint64, std::pair<double, double>>();
    self->lock = new std::mutex();
    self->embedder = nullptr;
    self->have_info = FALSE;
    gst_base_transform_set_in_place(GST_BASE_TRANSFORM(self), TRUE);
    gst_base_transform_set_passthrough(GST_BASE_TRANSFORM(self), FALSE);
}
static void gst_sentinel_body_class_init(GstSentinelBodyClass* k) {
    GObjectClass* oc = G_OBJECT_CLASS(k);
    GstElementClass* ec = GST_ELEMENT_CLASS(k);
    GstBaseTransformClass* bc = GST_BASE_TRANSFORM_CLASS(k);
    oc->set_property = body_set_property; oc->get_property = body_get_property; oc->finalize = body_finalize;
    const GParamFlags rw = (GParamFlags)(G_PARAM_READWRITE | G_PARAM_STATIC_STRINGS);
    g_object_class_install_property(oc, PROP_MODEL, g_param_spec_string("model", "Model", "TransReID IR path",
        "/models/surveillance/transreid_ssl_int8.xml", rw));
    g_object_class_install_property(oc, PROP_DEVICE, g_param_spec_string("device", "Device", "OpenVINO device", "NPU", rw));
    g_object_class_install_property(oc, PROP_INTERVAL, g_param_spec_double("interval", "Interval",
        "Seconds (source PTS) between embeddings of one track", 0.0, 3600.0, 1.0, rw));
    g_object_class_install_property(oc, PROP_MIN_W, g_param_spec_int("min-width", "Min width", "px", 0, 10000, 28, rw));
    g_object_class_install_property(oc, PROP_MIN_H, g_param_spec_int("min-height", "Min height", "px", 0, 10000, 56, rw));
    g_object_class_install_property(oc, PROP_STATS, g_param_spec_string("stats", "Stats", "Counters", "",
        (GParamFlags)(G_PARAM_READABLE | G_PARAM_STATIC_STRINGS)));
    gst_element_class_set_static_metadata(ec, "Sentinel body ReID", "Filter/Analytics/Video",
        "Exact v8 TransReID body embeddings attached as SentinelBodyMeta", "Sentry");
    gst_element_class_add_static_pad_template(ec, &sink_t);
    gst_element_class_add_static_pad_template(ec, &src_t);
    bc->transform_ip = GST_DEBUG_FUNCPTR(body_transform_ip);
    bc->set_caps = GST_DEBUG_FUNCPTR(body_set_caps);
    bc->start = GST_DEBUG_FUNCPTR(body_start);
}
static gboolean plugin_init(GstPlugin* plugin) {
    GST_DEBUG_CATEGORY_INIT(sentinelbody_debug, "sentinelbody", 0, "Sentry body ReID");
    static const gchar* tags[] = {nullptr};
    gst_meta_register_custom("SentinelBodyMeta", tags, nullptr, nullptr, nullptr);
    return gst_element_register(plugin, "sentinelbody", GST_RANK_NONE, GST_TYPE_SENTINEL_BODY);
}
#define PACKAGE "sentry"
GST_PLUGIN_DEFINE(GST_VERSION_MAJOR, GST_VERSION_MINOR, sentinelbody, "Sentry exact body ReID", plugin_init, "1.0",
                  "Proprietary", "sentry", "sentry")
