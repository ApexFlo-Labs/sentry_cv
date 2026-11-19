// Pillow-identical BILINEAR resize for 8-bit, 3-channel images (port of Pillow libImaging/Resample.c).
// Same coefficient computation (double), same fixed-point normalization (PRECISION_BITS = 22),
// same two-pass order (horizontal on the needed rows, then vertical), same rounding and clipping.
// Used so in-graph identity preprocessing matches v8's `Image.fromarray(rgb).resize(size, Image.BILINEAR)`.
#pragma once
#include <cmath>
#include <cstdint>
#include <vector>

namespace pillow {

static constexpr int PRECISION_BITS = 32 - 8 - 2;

inline double bilinear_filter(double x) {
    if (x < 0.0) x = -x;
    if (x < 1.0) return 1.0 - x;
    return 0.0;
}

inline uint8_t clip8(int in) {
    int v = in >> PRECISION_BITS;
    if (v < 0) return 0;
    if (v > 255) return 255;
    return static_cast<uint8_t>(v);
}

// Returns ksize; fills bounds (xmin, xcount per output pixel) and normalized int coefficients.
inline int precompute_coeffs(int inSize, double in0, double in1, int outSize,
                             std::vector<int>& bounds, std::vector<int32_t>& kk_int) {
    const double support_base = 1.0;  // bilinear
    double scale = (in1 - in0) / outSize;
    double filterscale = scale < 1.0 ? 1.0 : scale;
    double support = support_base * filterscale;
    int ksize = static_cast<int>(std::ceil(support)) * 2 + 1;
    std::vector<double> kk(static_cast<size_t>(outSize) * ksize, 0.0);
    bounds.assign(static_cast<size_t>(outSize) * 2, 0);
    for (int xx = 0; xx < outSize; ++xx) {
        double center = in0 + (xx + 0.5) * scale;
        double ww = 0.0;
        double ss = 1.0 / filterscale;
        int xmin = static_cast<int>(center - support + 0.5);
        if (xmin < 0) xmin = 0;
        int xmax = static_cast<int>(center + support + 0.5);
        if (xmax > inSize) xmax = inSize;
        xmax -= xmin;
        double* k = &kk[static_cast<size_t>(xx) * ksize];
        int x = 0;
        for (; x < xmax; ++x) {
            double w = bilinear_filter((x + xmin - center + 0.5) * ss);
            k[x] = w;
            ww += w;
        }
        for (x = 0; x < xmax; ++x) {
            if (ww != 0.0) k[x] /= ww;
        }
        for (; x < ksize; ++x) k[x] = 0;
        bounds[xx * 2 + 0] = xmin;
        bounds[xx * 2 + 1] = xmax;
    }
    kk_int.resize(kk.size());
    for (size_t i = 0; i < kk.size(); ++i) {
        if (kk[i] < 0)
            kk_int[i] = static_cast<int32_t>(-0.5 + kk[i] * (1 << PRECISION_BITS));
        else
            kk_int[i] = static_cast<int32_t>(0.5 + kk[i] * (1 << PRECISION_BITS));
    }
    return ksize;
}

// src: HxW interleaved with `channels` bytes per pixel (3), row stride in bytes.
// dst: outH x outW x 3 contiguous. Channel order is preserved (works for RGB or BGR alike,
// because Pillow processes channels independently).
inline void resize_bilinear(const uint8_t* src, int inW, int inH, int srcStride,
                            uint8_t* dst, int outW, int outH) {
    const int C = 3;
    std::vector<int> bh, bv;
    std::vector<int32_t> kh, kv;
    bool need_h = outW != inW;
    bool need_v = outH != inH;
    int ksh = precompute_coeffs(inW, 0.0, static_cast<double>(inW), outW, bh, kh);
    int ksv = precompute_coeffs(inH, 0.0, static_cast<double>(inH), outH, bv, kv);
    int ybox_first = bv[0];
    int ybox_last = bv[outH * 2 - 2] + bv[outH * 2 - 1];

    std::vector<uint8_t> temp;
    const uint8_t* vsrc = src;
    int vstride = srcStride;
    int vrows_offset = 0;
    if (need_h) {
        for (int i = 0; i < outH; ++i) bv[i * 2] -= ybox_first;
        int tH = ybox_last - ybox_first;
        temp.assign(static_cast<size_t>(tH) * outW * C, 0);
        for (int yy = 0; yy < tH; ++yy) {
            const uint8_t* row = src + static_cast<size_t>(yy + ybox_first) * srcStride;
            uint8_t* out = &temp[static_cast<size_t>(yy) * outW * C];
            for (int xx = 0; xx < outW; ++xx) {
                int xmin = bh[xx * 2], xmax = bh[xx * 2 + 1];
                const int32_t* k = &kh[static_cast<size_t>(xx) * ksh];
                int ss0 = 1 << (PRECISION_BITS - 1), ss1 = ss0, ss2 = ss0;
                for (int x = 0; x < xmax; ++x) {
                    const uint8_t* p = row + (x + xmin) * C;
                    ss0 += p[0] * k[x];
                    ss1 += p[1] * k[x];
                    ss2 += p[2] * k[x];
                }
                out[xx * C + 0] = clip8(ss0);
                out[xx * C + 1] = clip8(ss1);
                out[xx * C + 2] = clip8(ss2);
            }
        }
        vsrc = temp.data();
        vstride = outW * C;
        vrows_offset = 0;
    } else {
        vrows_offset = 0;  // vertical pass reads the source directly with original bounds
    }
    if (need_v) {
        for (int yy = 0; yy < outH; ++yy) {
            int ymin = bv[yy * 2], ymax = bv[yy * 2 + 1];
            const int32_t* k = &kv[static_cast<size_t>(yy) * ksv];
            uint8_t* out = dst + static_cast<size_t>(yy) * outW * C;
            for (int xx = 0; xx < outW; ++xx) {
                int ss0 = 1 << (PRECISION_BITS - 1), ss1 = ss0, ss2 = ss0;
                for (int y = 0; y < ymax; ++y) {
                    const uint8_t* p = vsrc + static_cast<size_t>(y + ymin + vrows_offset) * vstride + xx * C;
                    ss0 += p[0] * k[y];
                    ss1 += p[1] * k[y];
                    ss2 += p[2] * k[y];
                }
                out[xx * C + 0] = clip8(ss0);
                out[xx * C + 1] = clip8(ss1);
                out[xx * C + 2] = clip8(ss2);
            }
        }
    } else {
        for (int yy = 0; yy < outH; ++yy) {
            const uint8_t* row = vsrc + static_cast<size_t>(yy) * vstride;
            std::copy(row, row + outW * C, dst + static_cast<size_t>(yy) * outW * C);
        }
    }
}

}  // namespace pillow

extern "C" inline void pillow_resize_bilinear_c(const uint8_t* src, int inW, int inH, int srcStride,
                                                 uint8_t* dst, int outW, int outH) {
    pillow::resize_bilinear(src, inW, inH, srcStride, dst, outW, outH);
}
