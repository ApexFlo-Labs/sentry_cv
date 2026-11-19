// Exact C++ port of v8 gait silhouette steps (OpenCV 4.14.0, same as the image's cv2):
//   pipeline/person_reid.py   OpenVINOGaitExtractor._silhouettes  (MOG2 history=300 varThreshold=25 no shadows)
//   pipeline/gait_morphology.py clean_foreground / clean_person_regions
//   pipeline/person_reid.py   opengait_pretreat (64 x 44)
#pragma once
#include <opencv2/core.hpp>
#include <opencv2/imgproc.hpp>
#include <opencv2/video/background_segm.hpp>
#include <cmath>
#include <cstdint>
#include <vector>

namespace gaitx {

inline cv::Mat clean_foreground(const cv::Mat& mask) {
    cv::Mat kernel = cv::getStructuringElement(cv::MORPH_ELLIPSE, cv::Size(3, 3));
    cv::Mat opened, closed;
    cv::morphologyEx(mask, opened, cv::MORPH_OPEN, kernel);
    cv::morphologyEx(opened, closed, cv::MORPH_CLOSE, kernel, cv::Point(-1, -1), 2);
    return closed;
}

// Python slice(start, stop).indices(n) for start already clamped >= 0 and stop = min(n, stop).
inline void py_slice(int start, int stop, int n, int& a, int& b) {
    if (start < 0) start += n;           // not reached (caller clamps), kept for parity
    if (start < 0) start = 0;
    if (start > n) start = n;
    if (stop < 0) stop += n;
    if (stop < 0) stop = 0;
    if (stop > n) stop = n;
    a = start; b = stop;
}

// Returns empty Mat for "None".
inline cv::Mat clean_person_region(const cv::Mat& fg, int bx1, int by1, int bx2, int by2) {
    int H = fg.rows, W = fg.cols;
    int y1, y2, x1, x2;
    py_slice(std::max(0, by1), std::min(H, by2), H, y1, y2);
    py_slice(std::max(0, bx1), std::min(W, bx2), W, x1, x2);
    if (x2 <= x1 || y2 <= y1) return cv::Mat();
    int left = std::max(0, x1 - 6), top = std::max(0, y1 - 6);
    int right = std::min(W, x2 + 6), bottom = std::min(H, y2 + 6);
    cv::Mat cleaned = clean_foreground(fg(cv::Range(top, bottom), cv::Range(left, right)).clone());
    return cleaned(cv::Range(y1 - top, y2 - top), cv::Range(x1 - left, x2 - left)).clone();
}

// opengait_pretreat; returns empty Mat for None, else 64x44 CV_8U of 0/255.
inline cv::Mat opengait_pretreat(const cv::Mat& mask_in, int target_h = 64, int target_w = 44) {
    if (mask_in.empty()) return cv::Mat();
    double mx;
    cv::minMaxLoc(mask_in, nullptr, &mx);
    if (mx == 0) return cv::Mat();
    std::vector<int> ys;
    for (int r = 0; r < mask_in.rows; ++r)
        if (cv::countNonZero(mask_in.row(r)) > 0) ys.push_back(r);
    if (ys.size() < 10) return cv::Mat();
    cv::Mat mask = mask_in(cv::Range(ys.front(), ys.back() + 1), cv::Range::all());
    int height = mask.rows, width = mask.cols;
    int resized_width = std::max(1, static_cast<int>(static_cast<double>(width) * target_h / height));
    cv::Mat rs;
    cv::resize(mask, rs, cv::Size(resized_width, target_h), 0, 0, cv::INTER_NEAREST);
    std::vector<uint64_t> weights(rs.cols, 0);
    uint64_t total = 0;
    for (int r = 0; r < rs.rows; ++r) {
        const uint8_t* p = rs.ptr<uint8_t>(r);
        for (int c = 0; c < rs.cols; ++c) weights[c] += p[c];
    }
    long double num = 0;
    for (int c = 0; c < rs.cols; ++c) { total += weights[c]; num += static_cast<long double>(weights[c]) * c; }
    if (total == 0) return cv::Mat();
    // numpy: (weights * arange).sum() / weights.sum() in float64, then Python round() (half to even)
    double ratio = static_cast<double>(static_cast<uint64_t>(num)) / static_cast<double>(total);
    int center = static_cast<int>(std::nearbyint(ratio));
    int half = target_w / 2;
    int L = rs.cols;
    cv::Mat padded = cv::Mat::zeros(target_h, L + 2 * target_w, CV_8U);
    rs.copyTo(padded(cv::Range::all(), cv::Range(target_w, target_w + L)));
    int a = center + target_w - half, b = center + target_w + half;
    int P = padded.cols;
    int sa, sb;
    py_slice(a, b, P, sa, sb);  // numpy slice semantics (negative wraps, clamps)
    if (a < 0) { sa = a + P; if (sa < 0) sa = 0; }
    if (sb - sa != target_w) return cv::Mat();
    cv::Mat result = padded(cv::Range::all(), cv::Range(sa, sb));
    cv::Mat out;
    cv::compare(result, 0, out, cv::CMP_GT);  // 255 where > 0
    return out;
}

}  // namespace gaitx
