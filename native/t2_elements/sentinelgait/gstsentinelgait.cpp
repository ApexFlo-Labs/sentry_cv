// sentinelgait: exact v8 gait silhouette preparation inside the DL Streamer graph.
// Per camera (one element instance per camera graph): MOG2(history=300, varThreshold=25, detectShadows=false)
// updated on the full BGR frame on every `cadence`-th frame that contains a tracked person (v8: packet.index % 3 == 0
// when a person reaches gait collection). For each tracked person: clean_person_regions + opengait_pretreat
// (C++ ports in common/gait_silhouette.hpp, OpenCV 4.14.0 like the image's cv2).
// Attaches GstCustomMeta "SentinelGaitMeta" { count, track_ids u64[n], boxes i32[n*4], valid u8[n], masks u8[n*64*44] }.
// Python keeps v8's motion check, sequence buffers, emission gates and the GaitBase model call.
#include "gait_silhouette.hpp"
#include <gst/gst.h>
#include <gst/base/gstbasetransform.h>
#include <gst/video/video.h>
#include <gst/analytics/analytics.h>
#include <mutex>
#include <vector>

GST_DEBUG_CATEGORY_STATIC(sentinelgait_debug);
#define GST_CAT_DEFAULT sentinelgait_debug

struct GaitStats { guint64 frames = 0, gait_frames = 0, silhouettes = 0, valid = 0; double seconds = 0; };

typedef struct _GstSentinelGait {
    GstBaseTransform parent;
    std::mutex* lock;
    cv::Ptr<cv::BackgroundSubtractorMOG2>* mog;
    GaitStats* st;
    guint64 counter;
    gint cadence;
    GQuark person;
    GstVideoInfo info;
    gboolean have_info;
} GstSentinelGait;
typedef struct _GstSentinelGaitClass { GstBaseTransformClass parent_class; } GstSentinelGaitClass;
#define GST_TYPE_SENTINEL_GAIT (gst_sentinel_gait_get_type())
#define GST_SENTINEL_GAIT(o) (G_TYPE_CHECK_INSTANCE_CAST((o), GST_TYPE_SENTINEL_GAIT, GstSentinelGait))
G_DEFINE_TYPE(GstSentinelGait, gst_sentinel_gait, GST_TYPE_BASE_TRANSFORM)
enum { PROP_0, PROP_CADENCE, PROP_STATS };

static GstStaticPadTemplate sink_t = GST_STATIC_PAD_TEMPLATE("sink", GST_PAD_SINK, GST_PAD_ALWAYS,
    GST_STATIC_CAPS("video/x-raw, format=(string)BGR"));
static GstStaticPadTemplate src_t = GST_STATIC_PAD_TEMPLATE("src", GST_PAD_SRC, GST_PAD_ALWAYS,
    GST_STATIC_CAPS("video/x-raw, format=(string)BGR"));

static void reset_model(GstSentinelGait* self) {
    *self->mog = cv::createBackgroundSubtractorMOG2(300, 25, false);
    self->counter = 0;
}

static gboolean gait_set_caps(GstBaseTransform* t, GstCaps* in, GstCaps*) {
    GstSentinelGait* self = GST_SENTINEL_GAIT(t);
    self->have_info = gst_video_info_from_caps(&self->info, in);
    return self->have_info;
}
static gboolean gait_start(GstBaseTransform* t) {
    GstSentinelGait* self = GST_SENTINEL_GAIT(t);
    std::lock_guard<std::mutex> g(*self->lock);
    reset_model(self);
    return TRUE;
}

static GstFlowReturn gait_transform_ip(GstBaseTransform* t, GstBuffer* buf) {
    GstSentinelGait* self = GST_SENTINEL_GAIT(t);
    std::lock_guard<std::mutex> g(*self->lock);
    self->st->frames++;
    guint64 index = self->counter++;
    if (!self->have_info || index % (guint64)self->cadence != 0) return GST_FLOW_OK;
    GstAnalyticsRelationMeta* rmeta = gst_buffer_get_analytics_relation_meta(buf);
    if (!rmeta) return GST_FLOW_OK;
    struct P { guint64 tid; int x1, y1, x2, y2; };
    std::vector<P> people;
    gpointer state = nullptr;
    GstAnalyticsMtd od;
    while (gst_analytics_relation_meta_iterate(rmeta, &state, gst_analytics_od_mtd_get_mtd_type(), &od)) {
        if (gst_analytics_od_mtd_get_obj_type(&od) != self->person) continue;
        gint x, y, w, h; gfloat a, c;
        if (!gst_analytics_od_mtd_get_oriented_location(&od, &x, &y, &w, &h, &a, &c)) continue;
        gpointer ts = nullptr; GstAnalyticsMtd trk; bool found = false; guint64 tid = 0;
        while (gst_analytics_relation_meta_iterate(rmeta, &ts, gst_analytics_tracking_mtd_get_mtd_type(), &trk)) {
            if (trk.id == od.id || gst_analytics_relation_meta_get_relation(rmeta, od.id, trk.id) ==
                                       GST_ANALYTICS_REL_TYPE_NONE) continue;
            GstClockTime fs, ls; gboolean lost;
            found = gst_analytics_tracking_mtd_get_info((GstAnalyticsTrackingMtd*)&trk, &tid, &fs, &ls, &lost);
            break;
        }
        if (found) people.push_back({tid, x, y, x + w, y + h});
    }
    if (people.empty()) return GST_FLOW_OK;
    GstMapInfo map;
    if (!gst_buffer_map(buf, &map, GST_MAP_READ)) return GST_FLOW_OK;
    gint64 t0 = g_get_monotonic_time();
    const int W = GST_VIDEO_INFO_WIDTH(&self->info), H = GST_VIDEO_INFO_HEIGHT(&self->info);
    cv::Mat frame(H, W, CV_8UC3, map.data + GST_VIDEO_INFO_PLANE_OFFSET(&self->info, 0),
                  GST_VIDEO_INFO_PLANE_STRIDE(&self->info, 0));
    cv::Mat fg;
    (*self->mog)->apply(frame, fg);
    std::vector<guint64> tids; std::vector<gint32> boxes; std::vector<guint8> valid; std::vector<guint8> masks;
    for (const P& p : people) {
        cv::Mat sil = gaitx::opengait_pretreat(gaitx::clean_person_region(fg, p.x1, p.y1, p.x2, p.y2));
        tids.push_back(p.tid);
        boxes.insert(boxes.end(), {p.x1, p.y1, p.x2, p.y2});
        valid.push_back(sil.empty() ? 0 : 1);
        size_t off = masks.size();
        masks.resize(off + 64 * 44, 0);
        if (!sil.empty())
            for (int r = 0; r < 64; ++r) memcpy(&masks[off + r * 44], sil.ptr<uint8_t>(r), 44);
        self->st->silhouettes++;
        self->st->valid += sil.empty() ? 0 : 1;
    }
    gst_buffer_unmap(buf, &map);
    self->st->gait_frames++;
    self->st->seconds += (g_get_monotonic_time() - t0) / 1e6;
    GstCustomMeta* cm = gst_buffer_add_custom_meta(buf, "SentinelGaitMeta");
    if (!cm) return GST_FLOW_OK;
    GstStructure* s = gst_custom_meta_get_structure(cm);
    GBytes* b1 = g_bytes_new(tids.data(), tids.size() * 8);
    GBytes* b2 = g_bytes_new(boxes.data(), boxes.size() * 4);
    GBytes* b3 = g_bytes_new(valid.data(), valid.size());
    GBytes* b4 = g_bytes_new(masks.data(), masks.size());
    gst_structure_set(s, "count", G_TYPE_UINT, (guint)tids.size(), "track_ids", G_TYPE_BYTES, b1, "boxes",
                      G_TYPE_BYTES, b2, "valid", G_TYPE_BYTES, b3, "masks", G_TYPE_BYTES, b4, nullptr);
    g_bytes_unref(b1); g_bytes_unref(b2); g_bytes_unref(b3); g_bytes_unref(b4);
    return GST_FLOW_OK;
}

static gboolean gait_sink_event(GstBaseTransform* t, GstEvent* e) {
    if (GST_EVENT_TYPE(e) == GST_EVENT_FLUSH_STOP || GST_EVENT_TYPE(e) == GST_EVENT_STREAM_START) {
        GstSentinelGait* self = GST_SENTINEL_GAIT(t);
        std::lock_guard<std::mutex> g(*self->lock);
        reset_model(self);  // new stream session = new background (v8 resets per restart)
    }
    return GST_BASE_TRANSFORM_CLASS(gst_sentinel_gait_parent_class)->sink_event(t, e);
}

static void gait_set_property(GObject* o, guint id, const GValue* v, GParamSpec* p) {
    GstSentinelGait* self = GST_SENTINEL_GAIT(o);
    if (id == PROP_CADENCE) self->cadence = g_value_get_int(v); else G_OBJECT_WARN_INVALID_PROPERTY_ID(o, id, p);
}
static void gait_get_property(GObject* o, guint id, GValue* v, GParamSpec* p) {
    GstSentinelGait* self = GST_SENTINEL_GAIT(o);
    std::lock_guard<std::mutex> g(*self->lock);
    if (id == PROP_CADENCE) g_value_set_int(v, self->cadence);
    else if (id == PROP_STATS)
        g_value_take_string(v, g_strdup_printf("sentinelgait, frames=(guint64)%" G_GUINT64_FORMAT ", gait_frames=(guint64)%"
            G_GUINT64_FORMAT ", silhouettes=(guint64)%" G_GUINT64_FORMAT ", valid=(guint64)%" G_GUINT64_FORMAT
            ", seconds=(double)%.3f", self->st->frames, self->st->gait_frames, self->st->silhouettes, self->st->valid,
            self->st->seconds));
    else G_OBJECT_WARN_INVALID_PROPERTY_ID(o, id, p);
}
static void gait_finalize(GObject* o) {
    GstSentinelGait* self = GST_SENTINEL_GAIT(o);
    delete self->mog; delete self->st; delete self->lock;
    G_OBJECT_CLASS(gst_sentinel_gait_parent_class)->finalize(o);
}
static void gst_sentinel_gait_init(GstSentinelGait* self) {
    self->lock = new std::mutex();
    self->mog = new cv::Ptr<cv::BackgroundSubtractorMOG2>();
    self->st = new GaitStats();
    self->cadence = 3;
    self->person = g_quark_from_string("person");
    self->have_info = FALSE;
    reset_model(self);
    gst_base_transform_set_in_place(GST_BASE_TRANSFORM(self), TRUE);
    gst_base_transform_set_passthrough(GST_BASE_TRANSFORM(self), FALSE);
}
static void gst_sentinel_gait_class_init(GstSentinelGaitClass* k) {
    GObjectClass* oc = G_OBJECT_CLASS(k);
    GstElementClass* ec = GST_ELEMENT_CLASS(k);
    GstBaseTransformClass* bc = GST_BASE_TRANSFORM_CLASS(k);
    oc->set_property = gait_set_property; oc->get_property = gait_get_property; oc->finalize = gait_finalize;
    g_object_class_install_property(oc, PROP_CADENCE, g_param_spec_int("cadence", "Cadence",
        "Gait frame every N admitted frames (v8: 3)", 1, 100, 3, (GParamFlags)(G_PARAM_READWRITE | G_PARAM_STATIC_STRINGS)));
    g_object_class_install_property(oc, PROP_STATS, g_param_spec_string("stats", "Stats", "Counters", "",
        (GParamFlags)(G_PARAM_READABLE | G_PARAM_STATIC_STRINGS)));
    gst_element_class_set_static_metadata(ec, "Sentinel gait silhouettes", "Filter/Analytics/Video",
        "Exact v8 MOG2 gait silhouettes attached as SentinelGaitMeta", "Sentry");
    gst_element_class_add_static_pad_template(ec, &sink_t);
    gst_element_class_add_static_pad_template(ec, &src_t);
    bc->transform_ip = GST_DEBUG_FUNCPTR(gait_transform_ip);
    bc->set_caps = GST_DEBUG_FUNCPTR(gait_set_caps);
    bc->start = GST_DEBUG_FUNCPTR(gait_start);
    bc->sink_event = GST_DEBUG_FUNCPTR(gait_sink_event);
}
static gboolean plugin_init(GstPlugin* plugin) {
    GST_DEBUG_CATEGORY_INIT(sentinelgait_debug, "sentinelgait", 0, "Sentry gait silhouettes");
    static const gchar* tags[] = {nullptr};
    gst_meta_register_custom("SentinelGaitMeta", tags, nullptr, nullptr, nullptr);
    return gst_element_register(plugin, "sentinelgait", GST_RANK_NONE, GST_TYPE_SENTINEL_GAIT);
}
#define PACKAGE "sentry"
GST_PLUGIN_DEFINE(GST_VERSION_MAJOR, GST_VERSION_MINOR, sentinelgait, "Sentry exact gait silhouettes", plugin_init,
                  "1.0", "Proprietary", "sentry", "sentry")
