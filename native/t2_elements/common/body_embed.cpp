// Exact v8 body ReID embedding in C++ (crop -> Pillow-identical bilinear 128x256 on RGB -> [-1,1] -> TransReID).
// v8 reference: dls_secondary.DlsBodyExtractor._prepare + body_reid_model_proc.json (range [-1,1], RGB).
// Exported C API is used both by the parity test (ctypes) and by the sentinelbody GStreamer element.
#include "pillow_resize.hpp"
#include <openvino/openvino.hpp>
#include <cmath>
#include <map>
#include <memory>
#include <mutex>
#include <string>
#include <vector>

namespace {
std::mutex g_lock;
std::map<std::string, std::shared_ptr<ov::CompiledModel>> g_models;

std::shared_ptr<ov::CompiledModel> model_for(const std::string& path, const std::string& device) {
    std::lock_guard<std::mutex> lk(g_lock);
    auto key = path + "|" + device;
    auto it = g_models.find(key);
    if (it != g_models.end()) return it->second;
    static ov::Core core;
    auto m = std::make_shared<ov::CompiledModel>(core.compile_model(path, device));
    g_models[key] = m;
    return m;
}
}  // namespace

struct BodyEmbedder {
    std::shared_ptr<ov::CompiledModel> model;
    ov::InferRequest request;
    std::vector<ov::InferRequest> pool;
    std::vector<uint8_t> rgb_crop, resized;
    int norm_mode = 0;
};

// Prepare the [1,3,256,128] float input exactly like v8 from a BGR frame region.
static void prepare_input(BodyEmbedder* e, const uint8_t* bgr, int W, int H, int stride,
                          int x1, int y1, int x2, int y2, float* dst) {
    // v8 _crop: int, clamp to frame
    if (x1 < 0) x1 = 0;
    if (y1 < 0) y1 = 0;
    if (x2 > W) x2 = W;
    if (y2 > H) y2 = H;
    int cw = x2 - x1, ch = y2 - y1;
    // cv2.cvtColor(crop, BGR2RGB)
    e->rgb_crop.resize(static_cast<size_t>(cw) * ch * 3);
    for (int y = 0; y < ch; ++y) {
        const uint8_t* s = bgr + static_cast<size_t>(y + y1) * stride + static_cast<size_t>(x1) * 3;
        uint8_t* d = &e->rgb_crop[static_cast<size_t>(y) * cw * 3];
        for (int x = 0; x < cw; ++x) {
            d[x * 3 + 0] = s[x * 3 + 2];
            d[x * 3 + 1] = s[x * 3 + 1];
            d[x * 3 + 2] = s[x * 3 + 0];
        }
    }
    // Image.fromarray(rgb).resize((128, 256), BILINEAR)
    e->resized.resize(128 * 256 * 3);
    pillow::resize_bilinear(e->rgb_crop.data(), cw, ch, cw * 3, e->resized.data(), 128, 256);
    // v8 then converts RGB->BGR for appsrc and GVA converts back to RGB: net = RGB planes.
    const float a = 2.0f / 255.0f;
    for (int c = 0; c < 3; ++c)
        for (int i = 0; i < 128 * 256; ++i) {
            float v = static_cast<float>(e->resized[static_cast<size_t>(i) * 3 + c]);
            dst[static_cast<size_t>(c) * 128 * 256 + i] =
                e->norm_mode == 0 ? v / 255.0f * 2.0f - 1.0f : v * a - 1.0f;
        }
}

extern "C" {

void* body_embedder_create(const char* model_path, const char* device, int norm_mode) {
    try {
        auto* e = new BodyEmbedder();
        e->model = model_for(model_path, device);
        e->request = e->model->create_infer_request();
        e->norm_mode = norm_mode;
        return e;
    } catch (...) {
        return nullptr;
    }
}

void body_embedder_destroy(void* h) { delete static_cast<BodyEmbedder*>(h); }

// Returns 0 on success; out = L2-normalized 384 floats (v8 _l2).
int body_embedder_embed(void* h, const uint8_t* bgr, int W, int H, int stride,
                        int x1, int y1, int x2, int y2, float* out, int out_len) {
    auto* e = static_cast<BodyEmbedder*>(h);
    if (!e) return 1;
    if (std::min(x2, W) - std::max(x1, 0) <= 0 || std::min(y2, H) - std::max(y1, 0) <= 0) return 2;
    try {
        ov::Tensor input = e->request.get_input_tensor();
        prepare_input(e, bgr, W, H, stride, x1, y1, x2, y2, input.data<float>());
        e->request.infer();
        ov::Tensor o = e->request.get_output_tensor();
        const float* v = o.data<float>();
        int n = static_cast<int>(o.get_size());
        if (n != out_len) return 3;
        double ss = 0;
        for (int i = 0; i < n; ++i) ss += static_cast<double>(v[i]) * v[i];
        float norm = static_cast<float>(std::sqrt(ss));
        for (int i = 0; i < n; ++i) out[i] = norm > 0 ? v[i] / norm : v[i];
        return 0;
    } catch (...) {
        return 4;
    }
}


// T2: several crops of one frame in parallel (one InferRequest each, max 8 in flight). Per-crop computation is identical
// to body_embedder_embed; only concurrency changes. out = n * out_len floats; status[i] = 0 on success.
int body_embedder_embed_many(void* h, const uint8_t* bgr, int W, int H, int stride, const int* boxes, int n,
                             float* out, int out_len, int* status) {
    auto* e = static_cast<BodyEmbedder*>(h);
    if (!e) return 1;
    try {
        const int K = 8;
        while (static_cast<int>(e->pool.size()) < std::min(n, K)) e->pool.push_back(e->model->create_infer_request());
        for (int base = 0; base < n; base += K) {
            int m = std::min(K, n - base);
            std::vector<int> started(m, 0);
            for (int j = 0; j < m; ++j) {
                const int* b = boxes + (base + j) * 4;
                status[base + j] = 2;
                if (std::min(b[2], W) - std::max(b[0], 0) <= 0 || std::min(b[3], H) - std::max(b[1], 0) <= 0) continue;
                ov::Tensor in = e->pool[j].get_input_tensor();
                prepare_input(e, bgr, W, H, stride, b[0], b[1], b[2], b[3], in.data<float>());
                e->pool[j].start_async();
                started[j] = 1;
            }
            for (int j = 0; j < m; ++j) {
                if (!started[j]) continue;
                e->pool[j].wait();
                ov::Tensor o = e->pool[j].get_output_tensor();
                const float* v = o.data<float>();
                int nn = static_cast<int>(o.get_size());
                if (nn != out_len) { status[base + j] = 3; continue; }
                double ss = 0;
                for (int i = 0; i < nn; ++i) ss += static_cast<double>(v[i]) * v[i];
                float norm = static_cast<float>(std::sqrt(ss));
                float* dst = out + static_cast<size_t>(base + j) * out_len;
                for (int i = 0; i < nn; ++i) dst[i] = norm > 0 ? v[i] / norm : v[i];
                status[base + j] = 0;
            }
        }
        return 0;
    } catch (...) {
        return 4;
    }
}

}  // extern "C"
