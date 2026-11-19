// sentinelrategate: native, exact port of v8 dlstreamer_bridge.DlsPtsRateGate.probe (selection by source PTS).
// - buffer without PTS: dropped (counted)
// - PTS going backwards: new media epoch, accept immediately
// - first frame: accept, next = pts + interval
// - pts < next: drop; else advance next by whole intervals past pts and accept
// Timestamps and pixels are never modified. Counters are readable; action signal "reset" == DlsPtsRateGate.reset().
#include <gst/gst.h>
#include <gst/base/gstbasetransform.h>
#include <mutex>

typedef struct _GstSentinelRateGate {
    GstBaseTransform parent;
    std::mutex* lock;
    guint64 interval;
    gboolean have_next, have_last;
    guint64 next_pts, last_pts;
    guint64 accepted, dropped, received;
} GstSentinelRateGate;
typedef struct _GstSentinelRateGateClass { GstBaseTransformClass parent_class; } GstSentinelRateGateClass;
#define GST_TYPE_SENTINEL_RATE_GATE (gst_sentinel_rate_gate_get_type())
#define GST_SENTINEL_RATE_GATE(o) (G_TYPE_CHECK_INSTANCE_CAST((o), GST_TYPE_SENTINEL_RATE_GATE, GstSentinelRateGate))
G_DEFINE_TYPE(GstSentinelRateGate, gst_sentinel_rate_gate, GST_TYPE_BASE_TRANSFORM)
enum { PROP_0, PROP_INTERVAL, PROP_ACCEPTED, PROP_DROPPED, PROP_RECEIVED };
enum { SIGNAL_RESET, LAST_SIGNAL };
static guint signals[LAST_SIGNAL];

static GstStaticPadTemplate any_sink = GST_STATIC_PAD_TEMPLATE("sink", GST_PAD_SINK, GST_PAD_ALWAYS, GST_STATIC_CAPS_ANY);
static GstStaticPadTemplate any_src = GST_STATIC_PAD_TEMPLATE("src", GST_PAD_SRC, GST_PAD_ALWAYS, GST_STATIC_CAPS_ANY);

static GstFlowReturn rate_transform_ip(GstBaseTransform* t, GstBuffer* buf) {
    GstSentinelRateGate* self = GST_SENTINEL_RATE_GATE(t);
    std::lock_guard<std::mutex> g(*self->lock);
    GstClockTime pts = GST_BUFFER_PTS(buf);
    if (!GST_CLOCK_TIME_IS_VALID(pts)) { self->dropped++; return GST_BASE_TRANSFORM_FLOW_DROPPED; }
    self->received++;
    if (self->have_last && pts < self->last_pts) self->have_next = FALSE;
    self->last_pts = pts; self->have_last = TRUE;
    if (!self->have_next) {
        self->next_pts = pts + self->interval; self->have_next = TRUE;
        self->accepted++;
        return GST_FLOW_OK;
    }
    if (pts < self->next_pts) { self->dropped++; return GST_BASE_TRANSFORM_FLOW_DROPPED; }
    while (self->next_pts <= pts) self->next_pts += self->interval;
    self->accepted++;
    return GST_FLOW_OK;
}

static void rate_reset(GstSentinelRateGate* self) {
    std::lock_guard<std::mutex> g(*self->lock);
    self->have_next = FALSE; self->have_last = FALSE;
}

static void rate_set_property(GObject* o, guint id, const GValue* v, GParamSpec* p) {
    GstSentinelRateGate* self = GST_SENTINEL_RATE_GATE(o);
    std::lock_guard<std::mutex> g(*self->lock);
    if (id == PROP_INTERVAL) self->interval = MAX((guint64)1, g_value_get_uint64(v));
    else G_OBJECT_WARN_INVALID_PROPERTY_ID(o, id, p);
}
static void rate_get_property(GObject* o, guint id, GValue* v, GParamSpec* p) {
    GstSentinelRateGate* self = GST_SENTINEL_RATE_GATE(o);
    std::lock_guard<std::mutex> g(*self->lock);
    switch (id) {
    case PROP_INTERVAL: g_value_set_uint64(v, self->interval); break;
    case PROP_ACCEPTED: g_value_set_uint64(v, self->accepted); break;
    case PROP_DROPPED: g_value_set_uint64(v, self->dropped); break;
    case PROP_RECEIVED: g_value_set_uint64(v, self->received); break;
    default: G_OBJECT_WARN_INVALID_PROPERTY_ID(o, id, p);
    }
}
static void rate_finalize(GObject* o) {
    delete GST_SENTINEL_RATE_GATE(o)->lock;
    G_OBJECT_CLASS(gst_sentinel_rate_gate_parent_class)->finalize(o);
}
static void gst_sentinel_rate_gate_init(GstSentinelRateGate* self) {
    self->lock = new std::mutex();
    self->interval = GST_SECOND / 8;
    self->have_next = self->have_last = FALSE;
    self->next_pts = self->last_pts = 0;
    self->accepted = self->dropped = self->received = 0;
    gst_base_transform_set_in_place(GST_BASE_TRANSFORM(self), TRUE);
    gst_base_transform_set_passthrough(GST_BASE_TRANSFORM(self), FALSE);
}
static void gst_sentinel_rate_gate_class_init(GstSentinelRateGateClass* k) {
    GObjectClass* oc = G_OBJECT_CLASS(k);
    GstElementClass* ec = GST_ELEMENT_CLASS(k);
    GstBaseTransformClass* bc = GST_BASE_TRANSFORM_CLASS(k);
    oc->set_property = rate_set_property; oc->get_property = rate_get_property; oc->finalize = rate_finalize;
    const GParamFlags ro = (GParamFlags)(G_PARAM_READABLE | G_PARAM_STATIC_STRINGS);
    g_object_class_install_property(oc, PROP_INTERVAL, g_param_spec_uint64("interval", "Interval",
        "Accept interval in ns (round(GST_SECOND / fps), as v8)", 1, G_MAXUINT64, GST_SECOND / 8,
        (GParamFlags)(G_PARAM_READWRITE | G_PARAM_STATIC_STRINGS)));
    g_object_class_install_property(oc, PROP_ACCEPTED, g_param_spec_uint64("accepted", "Accepted", "", 0, G_MAXUINT64, 0, ro));
    g_object_class_install_property(oc, PROP_DROPPED, g_param_spec_uint64("dropped", "Dropped", "", 0, G_MAXUINT64, 0, ro));
    g_object_class_install_property(oc, PROP_RECEIVED, g_param_spec_uint64("received", "Received", "", 0, G_MAXUINT64, 0, ro));
    signals[SIGNAL_RESET] = g_signal_new_class_handler("reset", G_TYPE_FROM_CLASS(k),
        (GSignalFlags)(G_SIGNAL_RUN_LAST | G_SIGNAL_ACTION), G_CALLBACK(rate_reset), nullptr, nullptr, nullptr,
        G_TYPE_NONE, 0);
    gst_element_class_set_static_metadata(ec, "Sentinel PTS rate gate", "Filter/Video",
        "Exact native port of v8 DlsPtsRateGate (selection only)", "Sentry");
    gst_element_class_add_static_pad_template(ec, &any_sink);
    gst_element_class_add_static_pad_template(ec, &any_src);
    bc->transform_ip = GST_DEBUG_FUNCPTR(rate_transform_ip);
}

// ---- sentinelstamp: attach decode wall-clock time (CLOCK_REALTIME, ns) as GstReferenceTimestampMeta
// caps "timestamp/x-unix". Replaces v8's per-decoded-frame Python stamp probe (same capture point: decoder src).
typedef struct _GstSentinelStamp { GstBaseTransform parent; GstCaps* caps; } GstSentinelStamp;
typedef struct _GstSentinelStampClass { GstBaseTransformClass parent_class; } GstSentinelStampClass;
#define GST_TYPE_SENTINEL_STAMP (gst_sentinel_stamp_get_type())
#define GST_SENTINEL_STAMP(o) (G_TYPE_CHECK_INSTANCE_CAST((o), GST_TYPE_SENTINEL_STAMP, GstSentinelStamp))
G_DEFINE_TYPE(GstSentinelStamp, gst_sentinel_stamp, GST_TYPE_BASE_TRANSFORM)
static GstFlowReturn stamp_transform_ip(GstBaseTransform* t, GstBuffer* buf) {
    GstSentinelStamp* self = GST_SENTINEL_STAMP(t);
    gst_buffer_add_reference_timestamp_meta(buf, self->caps, (GstClockTime)g_get_real_time() * 1000, GST_CLOCK_TIME_NONE);
    return GST_FLOW_OK;
}
static void stamp_finalize(GObject* o) {
    gst_caps_unref(GST_SENTINEL_STAMP(o)->caps);
    G_OBJECT_CLASS(gst_sentinel_stamp_parent_class)->finalize(o);
}
static void gst_sentinel_stamp_init(GstSentinelStamp* self) {
    self->caps = gst_caps_from_string("timestamp/x-unix");
    gst_base_transform_set_in_place(GST_BASE_TRANSFORM(self), TRUE);
    gst_base_transform_set_passthrough(GST_BASE_TRANSFORM(self), FALSE);
}
static void gst_sentinel_stamp_class_init(GstSentinelStampClass* k) {
    G_OBJECT_CLASS(k)->finalize = stamp_finalize;
    GstElementClass* ec = GST_ELEMENT_CLASS(k);
    gst_element_class_set_static_metadata(ec, "Sentinel decode stamp", "Filter/Video",
        "Attach wall-clock decode time as reference timestamp meta (timestamp/x-unix)", "Sentry");
    gst_element_class_add_static_pad_template(ec, &any_sink);
    gst_element_class_add_static_pad_template(ec, &any_src);
    GST_BASE_TRANSFORM_CLASS(k)->transform_ip = GST_DEBUG_FUNCPTR(stamp_transform_ip);
}
static gboolean plugin_init(GstPlugin* plugin) {
    return gst_element_register(plugin, "sentinelrategate", GST_RANK_NONE, GST_TYPE_SENTINEL_RATE_GATE)
        && gst_element_register(plugin, "sentinelstamp", GST_RANK_NONE, GST_TYPE_SENTINEL_STAMP);
}
#define PACKAGE "sentry"
GST_PLUGIN_DEFINE(GST_VERSION_MAJOR, GST_VERSION_MINOR, sentinelrategate, "Sentry native PTS rate gate", plugin_init,
                  "1.0", "Proprietary", "sentry", "sentry")
