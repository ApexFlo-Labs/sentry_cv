// sentinelstate — per-camera track state and "due" gates for Sentry (build step 1).
//
// Place after gvatrack. Metadata only: never reads or changes pixels.
// For every tracked detection it keeps a small state record (UNKNOWN / STABLE /
// AT_RISK) and, when a modality (plate for now) is due, adds a region labelled
// "<modality>_due" with the same box and the same track ID, written exactly the
// way DL Streamer's VideoFrame::add_region + RegionOfInterest::set_object_id do
// (GstAnalytics OD mtd + legacy ROI meta with the same id + "detection" and
// "object_id" params + tracking mtd). Downstream gvadetect/gvaclassify with
// inference-region=roi-list object-class=plate_due therefore run only on due
// objects, and Python can read the due track IDs.
//
// Policy feedback: action signal "report" (track_id, modality, outcome) where
// outcome is "confirmed" | "miss" | "reset".
//
// Design reference: SENTRY_TARGET_ARCHITECTURE_LOW_LEVEL.md (build spec v2) §2–3.

#include <gst/gst.h>
#include <gst/base/gstbasetransform.h>
#include <gst/video/video.h>
#include <gst/analytics/analytics.h>

#include <algorithm>
#include <cmath>
#include <cstdint>
#include <mutex>
#include <set>
#include <sstream>
#include <string>
#include <unordered_map>
#include <vector>

GST_DEBUG_CATEGORY_STATIC(sentinelstate_debug);
#define GST_CAT_DEFAULT sentinelstate_debug

namespace {

enum class TrackState { UNKNOWN = 0, STABLE = 1, AT_RISK = 2 };

[[maybe_unused]] const char *state_name(TrackState s) {
    switch (s) {
    case TrackState::UNKNOWN: return "UNKNOWN";
    case TrackState::STABLE: return "STABLE";
    case TrackState::AT_RISK: return "AT_RISK";
    }
    return "?";
}

struct Box { int x = 0, y = 0, w = 0, h = 0; };

double iou(const Box &a, const Box &b) {
    const int x1 = std::max(a.x, b.x), y1 = std::max(a.y, b.y);
    const int x2 = std::min(a.x + a.w, b.x + b.w), y2 = std::min(a.y + a.h, b.y + b.h);
    if (x2 <= x1 || y2 <= y1) return 0.0;
    const double inter = double(x2 - x1) * double(y2 - y1);
    const double uni = double(a.w) * a.h + double(b.w) * b.h - inter;
    return uni > 0 ? inter / uni : 0.0;
}

struct PlateGate {
    TrackState state = TrackState::UNKNOWN;
    double last_due = -1e18;      // stream seconds of last due mark
    int misses = 0;               // consecutive misses while UNKNOWN
    bool recheck = false;         // set when an AT_RISK episode ends
    int last_due_width = 0;       // vehicle width at the last due mark (growth trigger)
    guint64 due_count = 0;
};

struct Track {
    guint64 id = 0;
    GQuark label = 0;
    bool vehicle = false;
    bool person = false;
    Box box;
    double first_seen = 0, last_seen = 0;
    guint generation = 0;
    TrackState geometry = TrackState::UNKNOWN; // UNKNOWN = no overlap; AT_RISK = overlapping
    int separated_frames = 0;
    PlateGate plate;
};

struct Detection {
    GstAnalyticsODMtd od;
    guint64 track_id;
    Box box;
    GQuark label;
};

struct Settings {
    std::set<GQuark> vehicle_labels;
    GQuark person_label = 0;
    int plate_min_width = 120;
    int edge_margin = 4;
    double plate_unknown_interval = 0.25;
    double plate_stable_interval = 60.0;
    int plate_backoff_misses = 8;
    double plate_backoff_max = 5.0;
    double plate_budget_per_second = 8.0;
    double plate_growth_factor = 1.25; // UNKNOWN: due again when the box grew this much (0 = off)
    double at_risk_iou = 0.3;
    double separated_iou = 0.1;
    int separated_frames = 2;
    double jump_fraction = 0.5;
    double track_expiry = 30.0;
    guint max_tracks = 512;
    int pressure = 0; // 0 normal, 1 elevated (x2), 2 critical (x4) for UNKNOWN intervals
    bool plate_enabled = true;
};

struct Stats {
    guint64 frames = 0, frames_without_pts = 0, detections = 0, tracks_created = 0, tracks_expired = 0,
            tracks_evicted = 0, generation_resets = 0, plate_eligible = 0, plate_due = 0,
            plate_budget_deferred = 0, plate_growth_triggers = 0, at_risk_entries = 0, reports = 0, reports_unknown_track = 0;
};

} // namespace

#define GST_TYPE_SENTINEL_STATE (gst_sentinel_state_get_type())
G_DECLARE_FINAL_TYPE(GstSentinelState, gst_sentinel_state, GST, SENTINEL_STATE, GstBaseTransform)

struct _GstSentinelState {
    GstBaseTransform parent;
    GstVideoInfo info;
    gboolean have_info;
    std::mutex *lock;
    Settings *settings;
    std::unordered_map<guint64, Track> *tracks;
    Stats *stats;
    double budget_tokens;
    double budget_time;
    guint next_generation;
};

G_DEFINE_TYPE(GstSentinelState, gst_sentinel_state, GST_TYPE_BASE_TRANSFORM)

enum {
    PROP_0,
    PROP_VEHICLE_LABELS,
    PROP_PERSON_LABEL,
    PROP_PLATE_ENABLED,
    PROP_PLATE_MIN_WIDTH,
    PROP_EDGE_MARGIN,
    PROP_PLATE_UNKNOWN_INTERVAL,
    PROP_PLATE_STABLE_INTERVAL,
    PROP_PLATE_BACKOFF_MISSES,
    PROP_PLATE_BACKOFF_MAX,
    PROP_PLATE_BUDGET,
    PROP_PLATE_GROWTH,
    PROP_AT_RISK_IOU,
    PROP_TRACK_EXPIRY,
    PROP_MAX_TRACKS,
    PROP_PRESSURE,
    PROP_STATS,
};

enum { SIGNAL_REPORT, N_SIGNALS };
static guint signals[N_SIGNALS];

static GstStaticPadTemplate sink_template =
    GST_STATIC_PAD_TEMPLATE("sink", GST_PAD_SINK, GST_PAD_ALWAYS, GST_STATIC_CAPS_ANY);
static GstStaticPadTemplate src_template =
    GST_STATIC_PAD_TEMPLATE("src", GST_PAD_SRC, GST_PAD_ALWAYS, GST_STATIC_CAPS_ANY);

static const char *DEFAULT_VEHICLE_LABELS = "bus,car,minivan,motorcycle,rickshaw,truck";

static void set_vehicle_labels(Settings *s, const gchar *csv) {
    s->vehicle_labels.clear();
    if (!csv) return;
    gchar **parts = g_strsplit(csv, ",", -1);
    for (gchar **p = parts; *p; ++p) {
        gchar *t = g_strstrip(*p);
        if (*t) s->vehicle_labels.insert(g_quark_from_string(t));
    }
    g_strfreev(parts);
}

static std::string vehicle_labels_csv(const Settings *s) {
    std::string out;
    for (GQuark q : s->vehicle_labels) {
        if (!out.empty()) out += ",";
        out += g_quark_to_string(q);
    }
    return out;
}

static std::string stats_string(GstSentinelState *self) {
    const Stats &st = *self->stats;
    guint64 by_state[3] = {0, 0, 0};
    guint64 at_risk = 0;
    for (const auto &kv : *self->tracks) {
        if (kv.second.vehicle) by_state[int(kv.second.plate.state)]++;
        if (kv.second.geometry == TrackState::AT_RISK) at_risk++;
    }
    std::ostringstream o;
    o << "sentinelstate, frames=(guint64)" << st.frames << ", frames_without_pts=(guint64)" << st.frames_without_pts
      << ", detections=(guint64)" << st.detections << ", tracks=(guint64)" << self->tracks->size()
      << ", tracks_created=(guint64)" << st.tracks_created << ", tracks_expired=(guint64)" << st.tracks_expired
      << ", tracks_evicted=(guint64)" << st.tracks_evicted << ", generation_resets=(guint64)" << st.generation_resets
      << ", at_risk_now=(guint64)" << at_risk << ", at_risk_entries=(guint64)" << st.at_risk_entries
      << ", plate_unknown=(guint64)" << by_state[0] << ", plate_stable=(guint64)" << by_state[1]
      << ", plate_eligible=(guint64)" << st.plate_eligible << ", plate_due=(guint64)" << st.plate_due
      << ", plate_budget_deferred=(guint64)" << st.plate_budget_deferred
      << ", plate_growth_triggers=(guint64)" << st.plate_growth_triggers << ", reports=(guint64)" << st.reports
      << ", reports_unknown_track=(guint64)" << st.reports_unknown_track << ";";
    return o.str();
}

// Add a "<modality>_due" region exactly as DL Streamer's add_region + set_object_id do.
static gboolean add_due_region(GstSentinelState *self, GstBuffer *buf, GstAnalyticsRelationMeta *rmeta,
                               const Detection &d, GQuark due_label, double confidence) {
    GstAnalyticsODMtd od;
    if (!gst_analytics_relation_meta_add_od_mtd(rmeta, due_label, d.box.x, d.box.y, d.box.w, d.box.h,
                                                (gfloat)confidence, &od))
        return FALSE;
    gst_analytics_relation_meta_set_relation(rmeta, GST_ANALYTICS_REL_TYPE_IS_PART_OF, od.id, d.od.id);
    gst_analytics_relation_meta_set_relation(rmeta, GST_ANALYTICS_REL_TYPE_CONTAIN, d.od.id, od.id);

    GstAnalyticsTrackingMtd trk;
    if (gst_analytics_relation_meta_add_tracking_mtd(rmeta, d.track_id, 0, &trk))
        gst_analytics_relation_meta_set_relation(rmeta, GST_ANALYTICS_REL_TYPE_RELATE_TO, od.id, trk.id);

    GstVideoRegionOfInterestMeta *roi = gst_buffer_add_video_region_of_interest_meta(
        buf, g_quark_to_string(due_label), (guint)std::max(0, d.box.x), (guint)std::max(0, d.box.y),
        (guint)std::max(0, d.box.w), (guint)std::max(0, d.box.h));
    if (!roi) return FALSE;
    roi->id = od.id;
    const double W = self->have_info && self->info.width > 0 ? self->info.width : 1.0;
    const double H = self->have_info && self->info.height > 0 ? self->info.height : 1.0;
    GstStructure *det = gst_structure_new("detection", "x_min", G_TYPE_DOUBLE, d.box.x / W, "x_max", G_TYPE_DOUBLE,
                                          (d.box.x + d.box.w) / W, "y_min", G_TYPE_DOUBLE, d.box.y / H, "y_max",
                                          G_TYPE_DOUBLE, (d.box.y + d.box.h) / H, "confidence", G_TYPE_DOUBLE,
                                          confidence, NULL);
    gst_video_region_of_interest_meta_add_param(roi, det);
    gst_video_region_of_interest_meta_add_param(
        roi, gst_structure_new("object_id", "id", G_TYPE_INT, (gint)d.track_id, NULL));
    return TRUE;
}

static bool touches_edge(const GstSentinelState *self, const Box &b, int margin) {
    if (!self->have_info) return false;
    return b.x <= margin || b.y <= margin || b.x + b.w >= self->info.width - margin ||
           b.y + b.h >= self->info.height - margin;
}

static GstFlowReturn gst_sentinel_state_transform_ip(GstBaseTransform *trans, GstBuffer *buf) {
    GstSentinelState *self = GST_SENTINEL_STATE(trans);
    std::lock_guard<std::mutex> guard(*self->lock);
    Settings &cfg = *self->settings;
    Stats &st = *self->stats;
    st.frames++;

    const GstClockTime pts = GST_BUFFER_PTS(buf);
    if (!GST_CLOCK_TIME_IS_VALID(pts)) {
        // No trustworthy time: never mark anything due on a guessed clock (F7).
        st.frames_without_pts++;
        return GST_FLOW_OK;
    }
    const double now = double(pts) / GST_SECOND;

    GstAnalyticsRelationMeta *rmeta = gst_buffer_get_analytics_relation_meta(buf);
    std::vector<Detection> dets;
    if (rmeta) {
        gpointer state = nullptr;
        GstAnalyticsMtd mtd;
        while (gst_analytics_relation_meta_iterate(rmeta, &state, gst_analytics_od_mtd_get_mtd_type(), &mtd)) {
            Detection d;
            d.od = mtd;
            d.label = gst_analytics_od_mtd_get_obj_type(&d.od);
            const bool vehicle = cfg.vehicle_labels.count(d.label) > 0;
            if (!vehicle && d.label != cfg.person_label) continue; // faces, our own *_due boxes, others
            gfloat conf = 0;
            if (!gst_analytics_od_mtd_get_location(&d.od, &d.box.x, &d.box.y, &d.box.w, &d.box.h, &conf)) continue;
            gpointer tstate = nullptr;
            GstAnalyticsTrackingMtd trk;
            bool have_track = false;
            while (gst_analytics_relation_meta_get_direct_related(rmeta, d.od.id, GST_ANALYTICS_REL_TYPE_ANY,
                                                                  gst_analytics_tracking_mtd_get_mtd_type(),
                                                                  &tstate, &trk)) {
                guint64 tid = 0;
                GstClockTime fs = 0, ls = 0;
                gboolean lost = FALSE;
                if (gst_analytics_tracking_mtd_get_info(&trk, &tid, &fs, &ls, &lost)) {
                    d.track_id = tid;
                    have_track = true;
                    break;
                }
            }
            if (!have_track) continue; // untracked detection: nothing to gate
            dets.push_back(d);
        }
    }
    st.detections += dets.size();

    // ---- update track table -------------------------------------------------
    for (const Detection &d : dets) {
        auto it = self->tracks->find(d.track_id);
        if (it != self->tracks->end() && it->second.label != d.label) {
            // Tracker reused the ID for another class, or class flipped: start over (TRACK-06).
            st.generation_resets++;
            self->tracks->erase(it);
            it = self->tracks->end();
        }
        if (it == self->tracks->end()) {
            if (self->tracks->size() >= cfg.max_tracks) {
                // Bounded memory (F18): evict the oldest-seen track.
                auto oldest = std::min_element(self->tracks->begin(), self->tracks->end(),
                                               [](const auto &a, const auto &b) {
                                                   return a.second.last_seen < b.second.last_seen;
                                               });
                if (oldest != self->tracks->end()) {
                    self->tracks->erase(oldest);
                    st.tracks_evicted++;
                }
            }
            Track t;
            t.id = d.track_id;
            t.label = d.label;
            t.vehicle = cfg.vehicle_labels.count(d.label) > 0;
            t.person = d.label == cfg.person_label;
            t.first_seen = now;
            t.box = d.box;
            t.generation = ++self->next_generation;
            it = self->tracks->emplace(d.track_id, t).first;
            st.tracks_created++;
        } else {
            // Box jump in one frame → AT_RISK (possible ID swap).
            const Track &t = it->second;
            const double cx0 = t.box.x + t.box.w / 2.0, cy0 = t.box.y + t.box.h / 2.0;
            const double cx1 = d.box.x + d.box.w / 2.0, cy1 = d.box.y + d.box.h / 2.0;
            const double jump = std::hypot(cx1 - cx0, cy1 - cy0);
            if (jump > cfg.jump_fraction * std::max(1, t.box.w) && it->second.geometry != TrackState::AT_RISK) {
                it->second.geometry = TrackState::AT_RISK;
                it->second.separated_frames = 0;
                st.at_risk_entries++;
            }
        }
        it->second.box = d.box;
        it->second.last_seen = now;
    }

    // ---- overlap → AT_RISK, separation → recheck --------------------------
    for (size_t i = 0; i < dets.size(); ++i) {
        Track &a = self->tracks->at(dets[i].track_id);
        double worst = 0.0;
        for (size_t j = 0; j < dets.size(); ++j) {
            if (i == j) continue;
            const Track &b = self->tracks->at(dets[j].track_id);
            if (a.vehicle != b.vehicle) continue; // compare like with like
            worst = std::max(worst, iou(dets[i].box, dets[j].box));
        }
        if (worst >= cfg.at_risk_iou) {
            if (a.geometry != TrackState::AT_RISK) st.at_risk_entries++;
            a.geometry = TrackState::AT_RISK;
            a.separated_frames = 0;
        } else if (a.geometry == TrackState::AT_RISK && worst < cfg.separated_iou) {
            if (++a.separated_frames >= cfg.separated_frames) {
                a.geometry = TrackState::UNKNOWN;
                a.separated_frames = 0;
                // Identity may now belong to someone else: confirm again, now.
                a.plate.state = TrackState::UNKNOWN;
                a.plate.recheck = true;
                a.plate.misses = 0;
            }
        }
    }

    // ---- expire tracks not seen for a while --------------------------------
    for (auto it = self->tracks->begin(); it != self->tracks->end();) {
        if (now - it->second.last_seen > cfg.track_expiry || now < it->second.last_seen - 1.0) {
            // second clause: PTS went backwards by >1 s (new stream session) → state is stale
            it = self->tracks->erase(it);
            st.tracks_expired++;
        } else {
            ++it;
        }
    }

    // ---- plate due gate ------------------------------------------------------
    if (cfg.plate_enabled && rmeta) {
        // Token bucket per camera: at most plate_budget_per_second marks, burst = 1 s worth.
        if (self->budget_time > now) self->budget_time = now; // time went backwards
        self->budget_tokens = std::min(cfg.plate_budget_per_second,
                                       self->budget_tokens + (now - self->budget_time) * cfg.plate_budget_per_second);
        self->budget_time = now;

        struct Candidate { const Detection *d; int priority; int area; };
        std::vector<Candidate> due;
        const double pressure_mult = cfg.pressure >= 2 ? 4.0 : cfg.pressure == 1 ? 2.0 : 1.0;
        for (const Detection &d : dets) {
            Track &t = self->tracks->at(d.track_id);
            if (!t.vehicle) continue;
            if (t.geometry == TrackState::AT_RISK) continue; // overlapping crop is ambiguous; wait for separation
            if (d.box.w < cfg.plate_min_width || touches_edge(self, d.box, cfg.edge_margin)) continue;
            st.plate_eligible++;
            PlateGate &g = t.plate;
            double interval;
            int priority;
            const bool grew = g.state == TrackState::UNKNOWN && cfg.plate_growth_factor > 0 && g.last_due_width > 0 &&
                              d.box.w >= cfg.plate_growth_factor * g.last_due_width;
            if (g.recheck) {
                interval = 0.0;
                priority = 0;
            } else if (grew) {
                // Vehicle came closer: the plate is now larger, so a read is worth trying again now.
                interval = 0.0;
                priority = 1;
                g.misses = 0;
                st.plate_growth_triggers++;
            } else if (g.state == TrackState::STABLE) {
                interval = cfg.plate_stable_interval;
                priority = 2;
            } else {
                const int doublings = cfg.plate_backoff_misses > 0 ? g.misses / cfg.plate_backoff_misses : 0;
                interval = std::min(cfg.plate_backoff_max,
                                    cfg.plate_unknown_interval * std::pow(2.0, std::min(doublings, 16)));
                interval *= pressure_mult;
                priority = 1;
            }
            if (now - g.last_due >= interval) due.push_back({&d, priority, d.box.w * d.box.h});
        }
        // Recheck first, then UNKNOWN (largest first), then STABLE.
        std::sort(due.begin(), due.end(), [](const Candidate &a, const Candidate &b) {
            return a.priority != b.priority ? a.priority < b.priority : a.area > b.area;
        });
        static const GQuark plate_due = g_quark_from_static_string("plate_due");
        for (const Candidate &c : due) {
            if (self->budget_tokens < 1.0) {
                st.plate_budget_deferred++; // stays due; retried next frame
                continue;
            }
            gfloat x = 0;
            gint dx, dy, dw, dh;
            gst_analytics_od_mtd_get_location(&c.d->od, &dx, &dy, &dw, &dh, &x);
            if (!add_due_region(self, buf, rmeta, *c.d, plate_due, x)) {
                GST_WARNING_OBJECT(self, "failed to add plate_due region for track %" G_GUINT64_FORMAT,
                                   c.d->track_id);
                continue;
            }
            self->budget_tokens -= 1.0;
            Track &t = self->tracks->at(c.d->track_id);
            t.plate.last_due = now;
            t.plate.recheck = false;
            t.plate.last_due_width = c.d->box.w;
            t.plate.due_count++;
            st.plate_due++;
        }
    }
    return GST_FLOW_OK;
}

static gboolean gst_sentinel_state_report(GstSentinelState *self, guint64 track_id, const gchar *modality,
                                          const gchar *outcome) {
    std::lock_guard<std::mutex> guard(*self->lock);
    self->stats->reports++;
    auto it = self->tracks->find(track_id);
    if (it == self->tracks->end()) {
        self->stats->reports_unknown_track++;
        return FALSE;
    }
    if (g_strcmp0(modality, "plate") != 0) return FALSE;
    PlateGate &g = it->second.plate;
    if (g_strcmp0(outcome, "confirmed") == 0) {
        g.state = TrackState::STABLE;
        g.misses = 0;
        g.recheck = false;
    } else if (g_strcmp0(outcome, "miss") == 0) {
        if (g.state == TrackState::UNKNOWN) g.misses++;
    } else if (g_strcmp0(outcome, "reset") == 0) {
        g.state = TrackState::UNKNOWN;
        g.misses = 0;
        g.recheck = true;
    } else {
        return FALSE;
    }
    return TRUE;
}

static gboolean gst_sentinel_state_set_caps(GstBaseTransform *trans, GstCaps *incaps, GstCaps *) {
    GstSentinelState *self = GST_SENTINEL_STATE(trans);
    self->have_info = gst_video_info_from_caps(&self->info, incaps);
    if (!self->have_info)
        GST_WARNING_OBJECT(self, "caps without video info; edge checks disabled: %" GST_PTR_FORMAT, incaps);
    return TRUE;
}

static gboolean gst_sentinel_state_start(GstBaseTransform *trans) {
    GstSentinelState *self = GST_SENTINEL_STATE(trans);
    std::lock_guard<std::mutex> guard(*self->lock);
    self->tracks->clear();
    self->budget_tokens = self->settings->plate_budget_per_second;
    self->budget_time = 0;
    return TRUE;
}

static void gst_sentinel_state_set_property(GObject *object, guint prop_id, const GValue *value, GParamSpec *pspec) {
    GstSentinelState *self = GST_SENTINEL_STATE(object);
    std::lock_guard<std::mutex> guard(*self->lock);
    Settings &s = *self->settings;
    switch (prop_id) {
    case PROP_VEHICLE_LABELS: set_vehicle_labels(&s, g_value_get_string(value)); break;
    case PROP_PERSON_LABEL: s.person_label = g_quark_from_string(g_value_get_string(value)); break;
    case PROP_PLATE_ENABLED: s.plate_enabled = g_value_get_boolean(value); break;
    case PROP_PLATE_MIN_WIDTH: s.plate_min_width = g_value_get_int(value); break;
    case PROP_EDGE_MARGIN: s.edge_margin = g_value_get_int(value); break;
    case PROP_PLATE_UNKNOWN_INTERVAL: s.plate_unknown_interval = g_value_get_double(value); break;
    case PROP_PLATE_STABLE_INTERVAL: s.plate_stable_interval = g_value_get_double(value); break;
    case PROP_PLATE_BACKOFF_MISSES: s.plate_backoff_misses = g_value_get_int(value); break;
    case PROP_PLATE_BACKOFF_MAX: s.plate_backoff_max = g_value_get_double(value); break;
    case PROP_PLATE_BUDGET: s.plate_budget_per_second = g_value_get_double(value); break;
    case PROP_PLATE_GROWTH: s.plate_growth_factor = g_value_get_double(value); break;
    case PROP_AT_RISK_IOU: s.at_risk_iou = g_value_get_double(value); break;
    case PROP_TRACK_EXPIRY: s.track_expiry = g_value_get_double(value); break;
    case PROP_MAX_TRACKS: s.max_tracks = g_value_get_uint(value); break;
    case PROP_PRESSURE: s.pressure = g_value_get_int(value); break;
    default: G_OBJECT_WARN_INVALID_PROPERTY_ID(object, prop_id, pspec);
    }
}

static void gst_sentinel_state_get_property(GObject *object, guint prop_id, GValue *value, GParamSpec *pspec) {
    GstSentinelState *self = GST_SENTINEL_STATE(object);
    std::lock_guard<std::mutex> guard(*self->lock);
    const Settings &s = *self->settings;
    switch (prop_id) {
    case PROP_VEHICLE_LABELS: g_value_set_string(value, vehicle_labels_csv(&s).c_str()); break;
    case PROP_PERSON_LABEL: g_value_set_string(value, s.person_label ? g_quark_to_string(s.person_label) : ""); break;
    case PROP_PLATE_ENABLED: g_value_set_boolean(value, s.plate_enabled); break;
    case PROP_PLATE_MIN_WIDTH: g_value_set_int(value, s.plate_min_width); break;
    case PROP_EDGE_MARGIN: g_value_set_int(value, s.edge_margin); break;
    case PROP_PLATE_UNKNOWN_INTERVAL: g_value_set_double(value, s.plate_unknown_interval); break;
    case PROP_PLATE_STABLE_INTERVAL: g_value_set_double(value, s.plate_stable_interval); break;
    case PROP_PLATE_BACKOFF_MISSES: g_value_set_int(value, s.plate_backoff_misses); break;
    case PROP_PLATE_BACKOFF_MAX: g_value_set_double(value, s.plate_backoff_max); break;
    case PROP_PLATE_BUDGET: g_value_set_double(value, s.plate_budget_per_second); break;
    case PROP_PLATE_GROWTH: g_value_set_double(value, s.plate_growth_factor); break;
    case PROP_AT_RISK_IOU: g_value_set_double(value, s.at_risk_iou); break;
    case PROP_TRACK_EXPIRY: g_value_set_double(value, s.track_expiry); break;
    case PROP_MAX_TRACKS: g_value_set_uint(value, s.max_tracks); break;
    case PROP_PRESSURE: g_value_set_int(value, s.pressure); break;
    case PROP_STATS: {
        // already holding the lock
        g_value_set_string(value, stats_string(self).c_str());
        break;
    }
    default: G_OBJECT_WARN_INVALID_PROPERTY_ID(object, prop_id, pspec);
    }
}

static void gst_sentinel_state_finalize(GObject *object) {
    GstSentinelState *self = GST_SENTINEL_STATE(object);
    delete self->tracks;
    delete self->settings;
    delete self->stats;
    delete self->lock;
    G_OBJECT_CLASS(gst_sentinel_state_parent_class)->finalize(object);
}

static void gst_sentinel_state_init(GstSentinelState *self) {
    self->lock = new std::mutex();
    self->settings = new Settings();
    self->tracks = new std::unordered_map<guint64, Track>();
    self->stats = new Stats();
    set_vehicle_labels(self->settings, DEFAULT_VEHICLE_LABELS);
    self->settings->person_label = g_quark_from_string("person");
    self->have_info = FALSE;
    self->budget_tokens = self->settings->plate_budget_per_second;
    self->budget_time = 0;
    self->next_generation = 0;
    gst_base_transform_set_in_place(GST_BASE_TRANSFORM(self), TRUE);
    gst_base_transform_set_passthrough(GST_BASE_TRANSFORM(self), FALSE);
}

static void gst_sentinel_state_class_init(GstSentinelStateClass *klass) {
    GObjectClass *gobject_class = G_OBJECT_CLASS(klass);
    GstElementClass *element_class = GST_ELEMENT_CLASS(klass);
    GstBaseTransformClass *bt_class = GST_BASE_TRANSFORM_CLASS(klass);

    gobject_class->set_property = gst_sentinel_state_set_property;
    gobject_class->get_property = gst_sentinel_state_get_property;
    gobject_class->finalize = gst_sentinel_state_finalize;

    const GParamFlags rw = (GParamFlags)(G_PARAM_READWRITE | G_PARAM_STATIC_STRINGS);
    g_object_class_install_property(gobject_class, PROP_VEHICLE_LABELS,
        g_param_spec_string("vehicle-labels", "Vehicle labels", "Comma-separated detector labels treated as vehicles",
                            DEFAULT_VEHICLE_LABELS, rw));
    g_object_class_install_property(gobject_class, PROP_PERSON_LABEL,
        g_param_spec_string("person-label", "Person label", "Detector label for persons", "person", rw));
    g_object_class_install_property(gobject_class, PROP_PLATE_ENABLED,
        g_param_spec_boolean("plate-enabled", "Plate gate", "Emit plate_due regions", TRUE, rw));
    g_object_class_install_property(gobject_class, PROP_PLATE_MIN_WIDTH,
        g_param_spec_int("plate-min-width", "Plate min vehicle width", "Vehicle box width (px) needed for a plate read",
                         0, 10000, 120, rw));
    g_object_class_install_property(gobject_class, PROP_EDGE_MARGIN,
        g_param_spec_int("edge-margin", "Edge margin", "Boxes within this many px of the frame edge are not due",
                         0, 1000, 4, rw));
    g_object_class_install_property(gobject_class, PROP_PLATE_UNKNOWN_INTERVAL,
        g_param_spec_double("plate-unknown-interval", "UNKNOWN interval", "Seconds between plate reads until confirmed",
                            0.0, 3600.0, 0.25, rw));
    g_object_class_install_property(gobject_class, PROP_PLATE_STABLE_INTERVAL,
        g_param_spec_double("plate-stable-interval", "STABLE interval", "Seconds between re-checks once confirmed",
                            0.0, 86400.0, 60.0, rw));
    g_object_class_install_property(gobject_class, PROP_PLATE_BACKOFF_MISSES,
        g_param_spec_int("plate-backoff-misses", "Backoff misses", "Misses before the UNKNOWN interval doubles (0=off)",
                         0, 1000, 8, rw));
    g_object_class_install_property(gobject_class, PROP_PLATE_BACKOFF_MAX,
        g_param_spec_double("plate-backoff-max", "Backoff max", "Largest UNKNOWN interval after backoff (s)",
                            0.0, 3600.0, 5.0, rw));
    g_object_class_install_property(gobject_class, PROP_PLATE_BUDGET,
        g_param_spec_double("plate-budget", "Plate budget", "Max plate_due marks per second for this camera",
                            0.0, 10000.0, 8.0, rw));
    g_object_class_install_property(gobject_class, PROP_PLATE_GROWTH,
        g_param_spec_double("plate-growth-factor", "Growth trigger",
                            "UNKNOWN vehicle is due again once its box width grew by this factor (0 = off)", 0.0, 100.0,
                            1.25, rw));
    g_object_class_install_property(gobject_class, PROP_AT_RISK_IOU,
        g_param_spec_double("at-risk-iou", "AT_RISK IoU", "Overlap that marks a track AT_RISK", 0.0, 1.0, 0.3, rw));
    g_object_class_install_property(gobject_class, PROP_TRACK_EXPIRY,
        g_param_spec_double("track-expiry", "Track expiry", "Seconds unseen before a track's state is dropped",
                            0.0, 86400.0, 30.0, rw));
    g_object_class_install_property(gobject_class, PROP_MAX_TRACKS,
        g_param_spec_uint("max-tracks", "Max tracks", "Upper bound on tracks kept (oldest evicted)", 1, 100000, 512,
                          rw));
    g_object_class_install_property(gobject_class, PROP_PRESSURE,
        g_param_spec_int("pressure", "Pressure", "0 normal, 1 elevated (UNKNOWN x2), 2 critical (UNKNOWN x4)", 0, 2,
                         0, rw));
    g_object_class_install_property(gobject_class, PROP_STATS,
        g_param_spec_string("stats", "Stats", "Counters as a GstStructure string", "",
                            (GParamFlags)(G_PARAM_READABLE | G_PARAM_STATIC_STRINGS)));

    signals[SIGNAL_REPORT] = g_signal_new_class_handler(
        "report", G_TYPE_FROM_CLASS(klass), (GSignalFlags)(G_SIGNAL_RUN_LAST | G_SIGNAL_ACTION),
        G_CALLBACK(gst_sentinel_state_report), nullptr, nullptr, nullptr, G_TYPE_BOOLEAN, 3, G_TYPE_UINT64,
        G_TYPE_STRING, G_TYPE_STRING);

    gst_element_class_set_static_metadata(element_class, "Sentinel track state", "Filter/Analytics/Video",
                                          "Per-track state and due gates; adds <modality>_due regions",
                                          "Sentry");
    gst_element_class_add_static_pad_template(element_class, &sink_template);
    gst_element_class_add_static_pad_template(element_class, &src_template);

    bt_class->transform_ip = GST_DEBUG_FUNCPTR(gst_sentinel_state_transform_ip);
    bt_class->set_caps = GST_DEBUG_FUNCPTR(gst_sentinel_state_set_caps);
    bt_class->start = GST_DEBUG_FUNCPTR(gst_sentinel_state_start);
}

static gboolean plugin_init(GstPlugin *plugin) {
    GST_DEBUG_CATEGORY_INIT(sentinelstate_debug, "sentinelstate", 0, "Sentry track state");
    return gst_element_register(plugin, "sentinelstate", GST_RANK_NONE, GST_TYPE_SENTINEL_STATE);
}

#define PACKAGE "sentry"
GST_PLUGIN_DEFINE(GST_VERSION_MAJOR, GST_VERSION_MINOR, sentinelstate, "Sentry track state and due gates",
                  plugin_init, "0.1", "Proprietary", "sentry", "https://example.invalid")
