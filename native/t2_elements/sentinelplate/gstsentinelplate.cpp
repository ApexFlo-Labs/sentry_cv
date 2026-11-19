// sentinelplate: exact v8 plate detection + OCR inference inside the DL Streamer graph (after in-graph BGR).
// For every plate_due region (sentinelstate; box = the vehicle box, same track id):
//   crop = frame[y1:y2, x1:x2]                          (v8 surface_stream plate loop)
//   letterbox(crop, 224): s = 224/max(h,w); nw,nh = round(w*s), round(h*s) (Python banker's rounding);
//                         cv::resize INTER_LINEAR; canvas 114; centered       (detectors/backends/openvino_detector.py)
//   plate model: RGB float32 /255, [1,3,224,224] -> [1,300,6]                  (plate_raw_model_proc.json)
//   yolo_detections(conf, max 1) with NumPy-2 float32 arithmetic, then shift by the vehicle origin
//   size/aspect usability (min_w, min_h, aspect range; v8 sharpness = 0 -> not used)
//   OCR: sf._crop(frame, plate box) -> cv::resize(128x64, INTER_LINEAR) -> RGB float32 0..255 -> [1,15,37] logits
// Attaches GstCustomMeta "SentinelPlateMeta": count, track_ids u64[n], vehicle_boxes i32[n*4], has_plate u8[n],
// plate_boxes i32[n*4], plate_conf f32[n], logits f32[n*15*37]. Python decodes logits with v8 decode_logits and keeps
// OcrStabilizer.should_ocr / observe (confirmation rules unchanged).
#include <gst/gst.h>
#include <gst/base/gstbasetransform.h>
#include <gst/video/video.h>
#include <gst/analytics/analytics.h>
#include <opencv2/core.hpp>
#include <opencv2/imgproc.hpp>
#include <openvino/openvino.hpp>
#include <openvino/core/preprocess/pre_post_process.hpp>
#include <cmath>
#include <map>
#include <memory>
#include <mutex>
#include <string>
#include <vector>

GST_DEBUG_CATEGORY_STATIC(sentinelplate_debug);
#define GST_CAT_DEFAULT sentinelplate_debug

namespace {
std::mutex g_models_lock;
std::map<std::string, std::shared_ptr<ov::CompiledModel>> g_models;
std::shared_ptr<ov::CompiledModel> model_for(const std::string& path, const std::string& device) {
    std::lock_guard<std::mutex> lk(g_models_lock);
    auto key = path + "|" + device;
    auto it = g_models.find(key);
    if (it != g_models.end()) return it->second;
    static ov::Core core;
    auto m = std::make_shared<ov::CompiledModel>(core.compile_model(path, device));
    g_models[key] = m;
    return m;
}
// Plate model compiled the way DL Streamer feeds it (measured exact, tests/parity/plate/ppp_probe.py):
// u8 NCHW RGB tensor -> convert to f32 -> scale(255) on the device.
std::shared_ptr<ov::CompiledModel> plate_model_u8(const std::string& path, const std::string& device) {
    std::lock_guard<std::mutex> lk(g_models_lock);
    auto key = path + "|" + device + "|u8scale255";
    auto it = g_models.find(key);
    if (it != g_models.end()) return it->second;
    static ov::Core core;
    auto model = core.read_model(path);
    ov::preprocess::PrePostProcessor ppp(model);
    ppp.input().tensor().set_element_type(ov::element::u8).set_layout("NCHW");
    ppp.input().model().set_layout("NCHW");
    ppp.input().preprocess().convert_element_type(ov::element::f32).scale(255.0f);
    auto m = std::make_shared<ov::CompiledModel>(core.compile_model(ppp.build(), device));
    g_models[key] = m;
    return m;
}
inline int py_round(double v) { return static_cast<int>(std::nearbyint(v)); }  // Python round(): half to even
}  // namespace

struct PlateSettings {
    std::string plate_model = "/models/traffic/openvino/license_plate.xml";
    std::string ocr_model = "/models/traffic/openvino/ocr.xml";
    std::string device = "NPU", ocr_device = "NPU";
    double conf = 0.35;
    int min_w = 16, min_h = 5;
    double min_aspect = 0.8, max_aspect = 12.0;
    GQuark due = 0;
};
struct PlateStats { guint64 frames = 0, vehicles = 0, plates = 0, ocr = 0, errors = 0; double seconds = 0; };

typedef struct _GstSentinelPlate {
    GstBaseTransform parent;
    PlateSettings* s; PlateStats* st; std::mutex* lock;
    std::shared_ptr<ov::CompiledModel>* pm; std::shared_ptr<ov::CompiledModel>* om;
    ov::InferRequest* preq; ov::InferRequest* oreq;
    std::vector<ov::InferRequest>* ppool; std::vector<ov::InferRequest>* opool;
    GstVideoInfo info; gboolean have_info;
} GstSentinelPlate;
typedef struct _GstSentinelPlateClass { GstBaseTransformClass parent_class; } GstSentinelPlateClass;
#define GST_TYPE_SENTINEL_PLATE (gst_sentinel_plate_get_type())
#define GST_SENTINEL_PLATE(o) (G_TYPE_CHECK_INSTANCE_CAST((o), GST_TYPE_SENTINEL_PLATE, GstSentinelPlate))
G_DEFINE_TYPE(GstSentinelPlate, gst_sentinel_plate, GST_TYPE_BASE_TRANSFORM)
enum { PROP_0, PROP_PLATE_MODEL, PROP_OCR_MODEL, PROP_DEVICE, PROP_OCR_DEVICE, PROP_CONF, PROP_MIN_W, PROP_MIN_H,
       PROP_MIN_ASPECT, PROP_MAX_ASPECT, PROP_STATS };
static GstStaticPadTemplate sink_t = GST_STATIC_PAD_TEMPLATE("sink", GST_PAD_SINK, GST_PAD_ALWAYS,
    GST_STATIC_CAPS("video/x-raw, format=(string)BGR"));
static GstStaticPadTemplate src_t = GST_STATIC_PAD_TEMPLATE("src", GST_PAD_SRC, GST_PAD_ALWAYS,
    GST_STATIC_CAPS("video/x-raw, format=(string)BGR"));

static void to_rgb_planar(const cv::Mat& bgr, float* dst, float scale, bool divide = false) {
    const int H = bgr.rows, W = bgr.cols;
    for (int y = 0; y < H; ++y) {
        const uint8_t* p = bgr.ptr<uint8_t>(y);
        for (int x = 0; x < W; ++x) {
            dst[0 * H * W + y * W + x] = divide ? static_cast<float>(p[x * 3 + 2]) / 255.0f : static_cast<float>(p[x * 3 + 2]) * scale;
            dst[1 * H * W + y * W + x] = divide ? static_cast<float>(p[x * 3 + 1]) / 255.0f : static_cast<float>(p[x * 3 + 1]) * scale;
            dst[2 * H * W + y * W + x] = divide ? static_cast<float>(p[x * 3 + 0]) / 255.0f : static_cast<float>(p[x * 3 + 0]) * scale;
        }
    }
}

static gboolean plate_set_caps(GstBaseTransform* t, GstCaps* in, GstCaps*) {
    GstSentinelPlate* self = GST_SENTINEL_PLATE(t);
    self->have_info = gst_video_info_from_caps(&self->info, in);
    return self->have_info;
}
static gboolean plate_start(GstBaseTransform* t) {
    GstSentinelPlate* self = GST_SENTINEL_PLATE(t);
    std::lock_guard<std::mutex> g(*self->lock);
    try {
        if (!*self->pm) {
            *self->pm = plate_model_u8(self->s->plate_model, self->s->device);
            *self->preq = (*self->pm)->create_infer_request();
        }
        if (!*self->om) {
            *self->om = model_for(self->s->ocr_model, self->s->ocr_device);
            *self->oreq = (*self->om)->create_infer_request();
        }
    } catch (const std::exception& e) {
        GST_ELEMENT_ERROR(self, RESOURCE, FAILED, ("sentinelplate: model load failed"), ("%s", e.what()));
        return FALSE;
    }
    return TRUE;
}

static GstFlowReturn plate_transform_ip(GstBaseTransform* t, GstBuffer* buf) {
    GstSentinelPlate* self = GST_SENTINEL_PLATE(t);
    std::lock_guard<std::mutex> g(*self->lock);
    PlateSettings& s = *self->s;
    self->st->frames++;
    GstAnalyticsRelationMeta* rmeta = gst_buffer_get_analytics_relation_meta(buf);
    if (!rmeta || !self->have_info) return GST_FLOW_OK;
    struct V { guint64 tid; int x1, y1, x2, y2; };
    std::vector<V> due;
    gpointer state = nullptr; GstAnalyticsMtd od;
    while (gst_analytics_relation_meta_iterate(rmeta, &state, gst_analytics_od_mtd_get_mtd_type(), &od)) {
        if (gst_analytics_od_mtd_get_obj_type(&od) != s.due) continue;
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
        if (found) due.push_back({tid, x, y, x + w, y + h});
    }
    if (due.empty()) return GST_FLOW_OK;
    GstMapInfo map;
    if (!gst_buffer_map(buf, &map, GST_MAP_READ)) return GST_FLOW_OK;
    gint64 t0 = g_get_monotonic_time();
    const int W = GST_VIDEO_INFO_WIDTH(&self->info), H = GST_VIDEO_INFO_HEIGHT(&self->info);
    cv::Mat frame(H, W, CV_8UC3, map.data + GST_VIDEO_INFO_PLANE_OFFSET(&self->info, 0),
                  GST_VIDEO_INFO_PLANE_STRIDE(&self->info, 0));
    // Phase 1: all plate detections of this frame in parallel (one request each); Phase 2: OCR in parallel.
    const size_t n = due.size();
    std::vector<int> okv(n, 0), cxv(n, 0), cyv(n, 0), padxv(n, 0), padyv(n, 0), wv(n, 0), hv(n, 0);
    std::vector<double> scv(n, 1.0);
    std::vector<int> P(n * 4, 0); std::vector<float> PC(n, 0.f), LG(n * 15 * 37, 0.f);
    auto& pp = *self->ppool; auto& op = *self->opool;
    try {
        while (pp.size() < std::min<size_t>(n, 16)) pp.push_back((*self->pm)->create_infer_request());
        const int size = 224;
        for (size_t base = 0; base < n; base += pp.size()) {
            size_t m = std::min(pp.size(), n - base);
            std::vector<int> started(m, 0);
            for (size_t j = 0; j < m; ++j) {
                const V& v = due[base + j];
                int cx1 = std::max(0, v.x1), cy1 = std::max(0, v.y1), cx2 = std::min(W, v.x2), cy2 = std::min(H, v.y2);
                if (cx2 <= cx1 || cy2 <= cy1) continue;
                cv::Mat crop = frame(cv::Range(cy1, cy2), cv::Range(cx1, cx2));
                const int h = crop.rows, w = crop.cols;
                double sc = static_cast<double>(size) / std::max(h, w);
                int nw = py_round(w * sc), nh = py_round(h * sc);
                cv::Mat resized;
                cv::resize(crop, resized, cv::Size(nw, nh), 0, 0, cv::INTER_LINEAR);
                cv::Mat canvas(size, size, CV_8UC3, cv::Scalar(114, 114, 114));
                int padx = (size - nw) / 2, pady = (size - nh) / 2;
                resized.copyTo(canvas(cv::Rect(padx, pady, nw, nh)));
                uint8_t* dst = pp[j].get_input_tensor().data<uint8_t>();
                for (int yy = 0; yy < size; ++yy) {
                    const uint8_t* q = canvas.ptr<uint8_t>(yy);
                    for (int xx = 0; xx < size; ++xx) {
                        dst[0 * size * size + yy * size + xx] = q[xx * 3 + 2];
                        dst[1 * size * size + yy * size + xx] = q[xx * 3 + 1];
                        dst[2 * size * size + yy * size + xx] = q[xx * 3 + 0];
                    }
                }
                size_t k = base + j;
                cxv[k] = cx1; cyv[k] = cy1; padxv[k] = padx; padyv[k] = pady; wv[k] = w; hv[k] = h; scv[k] = sc;
                pp[j].start_async(); started[j] = 1;
            }
            for (size_t j = 0; j < m; ++j) {
                if (!started[j]) continue;
                pp[j].wait();
                size_t k = base + j;
                const float* out = pp[j].get_output_tensor().data<float>();
                const float scf = static_cast<float>(scv[k]);
                for (int r = 0; r < 300; ++r) {
                    const float* row = out + r * 6;
                    float score = row[4];
                    if (static_cast<double>(score) < s.conf) break;  // Python: float(row[4]) < confidence (float64)
                    auto cvt = [&](float val, int pad, int lim) {  // NumPy 2 float32 arithmetic, int() truncation
                        float q = (val - static_cast<float>(pad)) / scf;
                        float mm = std::min(static_cast<float>(lim), q);
                        if (static_cast<float>(lim) <= q) mm = static_cast<float>(lim);
                        return static_cast<int>(std::max(0.0f, mm));
                    };
                    int x1 = cvt(row[0], padxv[k], wv[k]), y1 = cvt(row[1], padyv[k], hv[k]);
                    int x2 = cvt(row[2], padxv[k], wv[k]), y2 = cvt(row[3], padyv[k], hv[k]);
                    if (x2 <= x1 || y2 <= y1) continue;
                    P[k * 4 + 0] = cxv[k] + x1; P[k * 4 + 1] = cyv[k] + y1; P[k * 4 + 2] = cxv[k] + x2; P[k * 4 + 3] = cyv[k] + y2;
                    PC[k] = score; okv[k] = 1;
                    break;  // max_detections = 1
                }
                if (okv[k]) {
                    int pw = P[k * 4 + 2] - P[k * 4 + 0], ph = P[k * 4 + 3] - P[k * 4 + 1];
                    double aspect = static_cast<double>(pw) / std::max(static_cast<double>(ph), 1.0);
                    int ox1 = std::max(0, P[k * 4 + 0]), oy1 = std::max(0, P[k * 4 + 1]);
                    int ox2 = std::min(W, P[k * 4 + 2]), oy2 = std::min(H, P[k * 4 + 3]);
                    if (pw < s.min_w || ph < s.min_h || aspect < s.min_aspect || aspect > s.max_aspect ||
                        ox2 <= ox1 || oy2 <= oy1) okv[k] = 0;
                }
            }
        }
        std::vector<size_t> ocr_idx;
        for (size_t k = 0; k < n; ++k) if (okv[k]) ocr_idx.push_back(k);
        while (op.size() < std::min<size_t>(ocr_idx.size(), 16)) op.push_back((*self->om)->create_infer_request());
        for (size_t base = 0; base < ocr_idx.size(); base += op.size()) {
            size_t m = std::min(op.size(), ocr_idx.size() - base);
            for (size_t j = 0; j < m; ++j) {
                size_t k = ocr_idx[base + j];
                int ox1 = std::max(0, P[k * 4 + 0]), oy1 = std::max(0, P[k * 4 + 1]);
                int ox2 = std::min(W, P[k * 4 + 2]), oy2 = std::min(H, P[k * 4 + 3]);
                cv::Mat o;
                cv::resize(frame(cv::Range(oy1, oy2), cv::Range(ox1, ox2)), o, cv::Size(128, 64), 0, 0, cv::INTER_LINEAR);
                to_rgb_planar(o, op[j].get_input_tensor().data<float>(), 1.0f);
                op[j].start_async();
            }
            for (size_t j = 0; j < m; ++j) {
                size_t k = ocr_idx[base + j];
                op[j].wait();
                const float* lo = op[j].get_output_tensor().data<float>();
                std::copy(lo, lo + 15 * 37, LG.begin() + k * 15 * 37);
                self->st->plates++; self->st->ocr++;
            }
        }
    } catch (const std::exception& e) {
        self->st->errors++;
        std::fill(okv.begin(), okv.end(), 0);
    }
    std::vector<guint64> tids; std::vector<gint32> vboxes, pboxes; std::vector<guint8> has;
    std::vector<float> confs, logits;
    for (size_t k = 0; k < n; ++k) {
        const V& v = due[k];
        self->st->vehicles++;
        tids.push_back(v.tid);
        vboxes.insert(vboxes.end(), {v.x1, v.y1, v.x2, v.y2});
        has.push_back(okv[k] ? 1 : 0);
        if (okv[k]) pboxes.insert(pboxes.end(), {P[k * 4], P[k * 4 + 1], P[k * 4 + 2], P[k * 4 + 3]});
        else pboxes.insert(pboxes.end(), {0, 0, 0, 0});
        confs.push_back(okv[k] ? PC[k] : 0.f);
        logits.insert(logits.end(), LG.begin() + k * 15 * 37, LG.begin() + (k + 1) * 15 * 37);
    }
    gst_buffer_unmap(buf, &map);
    self->st->seconds += (g_get_monotonic_time() - t0) / 1e6;
    GstCustomMeta* cm = gst_buffer_add_custom_meta(buf, "SentinelPlateMeta");
    if (!cm) return GST_FLOW_OK;
    GstStructure* st = gst_custom_meta_get_structure(cm);
    auto B = [](const void* p, size_t n) { return g_bytes_new(p, n); };
    GBytes *b1 = B(tids.data(), tids.size() * 8), *b2 = B(vboxes.data(), vboxes.size() * 4),
           *b3 = B(has.data(), has.size()), *b4 = B(pboxes.data(), pboxes.size() * 4),
           *b5 = B(confs.data(), confs.size() * 4), *b6 = B(logits.data(), logits.size() * 4);
    gst_structure_set(st, "count", G_TYPE_UINT, (guint)tids.size(), "track_ids", G_TYPE_BYTES, b1, "vehicle_boxes",
                      G_TYPE_BYTES, b2, "has_plate", G_TYPE_BYTES, b3, "plate_boxes", G_TYPE_BYTES, b4, "plate_conf",
                      G_TYPE_BYTES, b5, "logits", G_TYPE_BYTES, b6, nullptr);
    for (GBytes* b : {b1, b2, b3, b4, b5, b6}) g_bytes_unref(b);
    return GST_FLOW_OK;
}

static void plate_set_property(GObject* o, guint id, const GValue* v, GParamSpec* p) {
    GstSentinelPlate* self = GST_SENTINEL_PLATE(o);
    std::lock_guard<std::mutex> g(*self->lock);
    PlateSettings& s = *self->s;
    switch (id) {
    case PROP_PLATE_MODEL: s.plate_model = g_value_get_string(v); break;
    case PROP_OCR_MODEL: s.ocr_model = g_value_get_string(v); break;
    case PROP_DEVICE: s.device = g_value_get_string(v); break;
    case PROP_OCR_DEVICE: s.ocr_device = g_value_get_string(v); break;
    case PROP_CONF: s.conf = g_value_get_double(v); break;
    case PROP_MIN_W: s.min_w = g_value_get_int(v); break;
    case PROP_MIN_H: s.min_h = g_value_get_int(v); break;
    case PROP_MIN_ASPECT: s.min_aspect = g_value_get_double(v); break;
    case PROP_MAX_ASPECT: s.max_aspect = g_value_get_double(v); break;
    default: G_OBJECT_WARN_INVALID_PROPERTY_ID(o, id, p);
    }
}
static void plate_get_property(GObject* o, guint id, GValue* v, GParamSpec* p) {
    GstSentinelPlate* self = GST_SENTINEL_PLATE(o);
    std::lock_guard<std::mutex> g(*self->lock);
    if (id == PROP_STATS)
        g_value_take_string(v, g_strdup_printf("sentinelplate, frames=(guint64)%" G_GUINT64_FORMAT ", vehicles=(guint64)%"
            G_GUINT64_FORMAT ", plates=(guint64)%" G_GUINT64_FORMAT ", ocr=(guint64)%" G_GUINT64_FORMAT ", errors=(guint64)%"
            G_GUINT64_FORMAT ", seconds=(double)%.3f", self->st->frames, self->st->vehicles, self->st->plates,
            self->st->ocr, self->st->errors, self->st->seconds));
    else if (id == PROP_CONF) g_value_set_double(v, self->s->conf);
    else G_OBJECT_WARN_INVALID_PROPERTY_ID(o, id, p);
}
static void plate_finalize(GObject* o) {
    GstSentinelPlate* self = GST_SENTINEL_PLATE(o);
    delete self->ppool; delete self->opool; delete self->preq; delete self->oreq; delete self->pm; delete self->om;
    delete self->s; delete self->st; delete self->lock;
    G_OBJECT_CLASS(gst_sentinel_plate_parent_class)->finalize(o);
}
static void gst_sentinel_plate_init(GstSentinelPlate* self) {
    self->s = new PlateSettings(); self->s->due = g_quark_from_string("plate_due");
    self->st = new PlateStats(); self->lock = new std::mutex();
    self->pm = new std::shared_ptr<ov::CompiledModel>(); self->om = new std::shared_ptr<ov::CompiledModel>();
    self->preq = new ov::InferRequest(); self->oreq = new ov::InferRequest();
    self->ppool = new std::vector<ov::InferRequest>(); self->opool = new std::vector<ov::InferRequest>();
    self->have_info = FALSE;
    gst_base_transform_set_in_place(GST_BASE_TRANSFORM(self), TRUE);
    gst_base_transform_set_passthrough(GST_BASE_TRANSFORM(self), FALSE);
}
static void gst_sentinel_plate_class_init(GstSentinelPlateClass* k) {
    GObjectClass* oc = G_OBJECT_CLASS(k);
    GstElementClass* ec = GST_ELEMENT_CLASS(k);
    GstBaseTransformClass* bc = GST_BASE_TRANSFORM_CLASS(k);
    oc->set_property = plate_set_property; oc->get_property = plate_get_property; oc->finalize = plate_finalize;
    const GParamFlags rw = (GParamFlags)(G_PARAM_READWRITE | G_PARAM_STATIC_STRINGS);
    g_object_class_install_property(oc, PROP_PLATE_MODEL, g_param_spec_string("plate-model", "", "", "/models/traffic/openvino/license_plate.xml", rw));
    g_object_class_install_property(oc, PROP_OCR_MODEL, g_param_spec_string("ocr-model", "", "", "/models/traffic/openvino/ocr.xml", rw));
    g_object_class_install_property(oc, PROP_DEVICE, g_param_spec_string("device", "", "", "NPU", rw));
    g_object_class_install_property(oc, PROP_OCR_DEVICE, g_param_spec_string("ocr-device", "", "", "NPU", rw));
    g_object_class_install_property(oc, PROP_CONF, g_param_spec_double("confidence", "", "", 0, 1, 0.35, rw));
    g_object_class_install_property(oc, PROP_MIN_W, g_param_spec_int("min-width", "", "", 0, 10000, 16, rw));
    g_object_class_install_property(oc, PROP_MIN_H, g_param_spec_int("min-height", "", "", 0, 10000, 5, rw));
    g_object_class_install_property(oc, PROP_MIN_ASPECT, g_param_spec_double("min-aspect", "", "", 0, 1000, 0.8, rw));
    g_object_class_install_property(oc, PROP_MAX_ASPECT, g_param_spec_double("max-aspect", "", "", 0, 1000, 12.0, rw));
    g_object_class_install_property(oc, PROP_STATS, g_param_spec_string("stats", "", "", "",
        (GParamFlags)(G_PARAM_READABLE | G_PARAM_STATIC_STRINGS)));
    gst_element_class_set_static_metadata(ec, "Sentinel plate + OCR", "Filter/Analytics/Video",
        "Exact v8 plate detection + OCR logits attached as SentinelPlateMeta", "Sentry");
    gst_element_class_add_static_pad_template(ec, &sink_t);
    gst_element_class_add_static_pad_template(ec, &src_t);
    bc->transform_ip = GST_DEBUG_FUNCPTR(plate_transform_ip);
    bc->set_caps = GST_DEBUG_FUNCPTR(plate_set_caps);
    bc->start = GST_DEBUG_FUNCPTR(plate_start);
}
static gboolean plugin_init(GstPlugin* plugin) {
    GST_DEBUG_CATEGORY_INIT(sentinelplate_debug, "sentinelplate", 0, "Sentry plate + OCR");
    static const gchar* tags[] = {nullptr};
    gst_meta_register_custom("SentinelPlateMeta", tags, nullptr, nullptr, nullptr);
    return gst_element_register(plugin, "sentinelplate", GST_RANK_NONE, GST_TYPE_SENTINEL_PLATE);
}
#define PACKAGE "sentry"
GST_PLUGIN_DEFINE(GST_VERSION_MAJOR, GST_VERSION_MINOR, sentinelplate, "Sentry exact plate + OCR", plugin_init, "1.0",
                  "Proprietary", "sentry", "sentry")
