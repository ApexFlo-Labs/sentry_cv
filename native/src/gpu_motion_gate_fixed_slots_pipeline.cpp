#include <CL/cl.h>
#include <CL/cl_ext.h>
#include <CL/cl_va_api_media_sharing_intel.h>

#include <opencv2/core.hpp>
#include <opencv2/core/ocl.hpp>
#include <opencv2/imgproc.hpp>
#include <opencv2/video/tracking.hpp>

#include <cmath>
#include <algorithm>
#include <chrono>
#include <cstdlib>
#include <array>
#include <cstddef>
#include <cstdint>
#include <cstring>
#include <deque>
#include <exception>
#include <thread>
#include <memory>
#include <stdexcept>
#include <string>
#include <vector>

namespace {

constexpr int kMaxCorners = 300;
constexpr int kRedetectEvery = 25;
constexpr std::size_t kAsyncHistoryFrames = 64;

thread_local std::string last_error;

const char* analysis_source = R"CLC(
__kernel void analyze_and_compact(
    __global const float2* old_points,
    __global const float2* new_points,
    __global const uchar* status,
    const int count,
    __global float2* compact_points,
    __global int* result)
{
    if (get_global_id(0) != 0) return;
    int valid = 0;
    int moving = 0;
    float sum_magnitude = 0.0f;
    float sum_abs_dy = 0.0f;
    int outward = 0;
    int hist[8] = {0, 0, 0, 0, 0, 0, 0, 0};
    const float pi = 3.14159265358979323846f;

    for (int i = 0; i < count; ++i) {
        if (!status[i]) continue;
        const float2 old_p = old_points[i];
        const float2 new_p = new_points[i];
        compact_points[valid++] = new_p;
        const float dx = new_p.x - old_p.x;
        const float dy = new_p.y - old_p.y;
        const float magnitude = sqrt(dx * dx + dy * dy);
        sum_magnitude += magnitude;
        sum_abs_dy += fabs(dy);
        const int is_lower = new_p.y > 90.0f;
        if ((is_lower && dy > 0.2f) || (!is_lower && dy < -0.2f))
            ++outward;
        if (magnitude > 0.2f) {
            ++moving;
            const float angle = atan2(dy, dx);
            int bin = (int)floor((angle + pi) * (8.0f / (2.0f * pi)));
            bin = clamp(bin, 0, 7);
            ++hist[bin];
        }
    }

    int max_bin = 0;
    for (int i = 0; i < 8; ++i) max_bin = max(max_bin, hist[i]);
    const float mean_magnitude = valid ? sum_magnitude / valid : 0.0f;
    const float move_percent = valid ? 100.0f * moving / valid : 0.0f;
    const float consistency = moving ? (float)max_bin / moving : 0.0f;
    const float mean_abs_dy = valid ? sum_abs_dy / valid : 0.0f;
    const float outward_percent = valid ? 100.0f * outward / valid : 0.0f;
    const int height_motion = outward_percent > 3.0f &&
                              mean_abs_dy > 0.12f;
    const int candidate = (move_percent > 2.0f &&
                           mean_magnitude > 0.08f &&
                           consistency > 0.42f) || height_motion;
    result[0] = valid;
    result[1] = candidate;
}
)CLC";

// Device-resident replacement for the CPU sort/min-distance portion of
// OpenCV 4.6 ocl_goodFeaturesToTrack. The corner response itself is produced
// by cv::cornerMinEigenVal on UMat, which dispatches OpenCV's OpenCL kernels.
const char* gpu_redetect_source = R"CLC(
inline int corner_better(const float2 a, const float2 b)
{
    if (a.x > b.x) return 1;
    if (a.x < b.x) return 0;
    const uint ap = as_uint(a.y);
    const uint bp = as_uint(b.y);
    const int ay = (int)(ap & 0xffffu);
    const int by = (int)(bp & 0xffffu);
    if (ay > by) return 1;
    if (ay < by) return 0;
    return (int)(ap >> 16) > (int)(bp >> 16);
}

inline void corner_swap(__global float2* values, const int a, const int b)
{
    const float2 tmp = values[a];
    values[a] = values[b];
    values[b] = tmp;
}

inline void sift_down(__global float2* values, int root, const int end)
{
    while (root * 2 + 1 <= end) {
        int child = root * 2 + 1;
        if (child + 1 <= end && corner_better(values[child + 1], values[child]))
            ++child;
        if (!corner_better(values[child], values[root])) return;
        corner_swap(values, root, child);
        root = child;
    }
}

__kernel void redetect_reduce_max(
    __global const uchar* eigptr, const int eig_step, const int eig_offset,
    const int eig_rows, const int eig_cols, __global float* group_max)
{
    __local float scratch[256];
    const int local_id = (int)get_local_id(0);
    const int global_id = (int)get_global_id(0);
    const int global_size = (int)get_global_size(0);
    const int total = eig_rows * eig_cols;
    float value = -3.402823466e+38f;
    for (int index = global_id; index < total; index += global_size) {
        const int y = index / eig_cols;
        const int x = index - y * eig_cols;
        __global const float* row = (__global const float*)(
            eigptr + eig_offset + y * eig_step);
        value = fmax(value, row[x]);
    }
    scratch[local_id] = value;
    barrier(CLK_LOCAL_MEM_FENCE);
    for (int stride = (int)get_local_size(0) / 2; stride > 0;
         stride >>= 1) {
        if (local_id < stride)
            scratch[local_id] =
                fmax(scratch[local_id], scratch[local_id + stride]);
        barrier(CLK_LOCAL_MEM_FENCE);
    }
    if (local_id == 0)
        group_max[get_group_id(0)] = scratch[0];
}

__kernel void redetect_prepare(
    __global const float* group_max, const int group_count,
    const float quality_level, __global float* threshold,
    __global int* candidate_count)
{
    if (get_global_id(0) != 0) return;
    float max_value = -3.402823466e+38f;
    for (int index = 0; index < group_count; ++index)
        max_value = fmax(max_value, group_max[index]);
    threshold[0] = max_value * quality_level;
    candidate_count[0] = 0;
}

__kernel void redetect_collect(
    __global const uchar* eigptr, const int eig_step, const int eig_offset,
    const int eig_rows, const int eig_cols,
    __global const float* threshold, __global float2* candidates,
    volatile __global int* candidate_count, const int capacity)
{
    int x = (int)get_global_id(0) + 1;
    int y = (int)get_global_id(1) + 1;
    if (x >= eig_cols - 1 || y >= eig_rows - 1) return;

    __global const float* prior = (__global const float*)(
        eigptr + eig_offset + (y - 1) * eig_step);
    __global const float* row = (__global const float*)(
        eigptr + eig_offset + y * eig_step);
    __global const float* next = (__global const float*)(
        eigptr + eig_offset + (y + 1) * eig_step);
    const float value = row[x];
    if (value <= threshold[0]) return;

    float maximum = value;
    maximum = fmax(maximum, prior[x - 1]);
    maximum = fmax(maximum, prior[x]);
    maximum = fmax(maximum, prior[x + 1]);
    maximum = fmax(maximum, row[x - 1]);
    maximum = fmax(maximum, row[x + 1]);
    maximum = fmax(maximum, next[x - 1]);
    maximum = fmax(maximum, next[x]);
    maximum = fmax(maximum, next[x + 1]);
    if (value != maximum) return;

    const int index = atomic_inc(candidate_count);
    if (index < capacity) {
        const uint packed = ((uint)x << 16) | (uint)y;
        candidates[index] = (float2)(value, as_float(packed));
    }
}

__kernel void redetect_sort_filter(
    __global float2* candidates, __global const int* candidate_count,
    __global float2* selected_points, __global int* selected_count,
    const int capacity, const int max_corners, const float min_distance_sq)
{
    if (get_global_id(0) != 0) return;
    const int count = min(candidate_count[0], capacity);

    for (int start = count / 2 - 1; start >= 0; --start)
        sift_down(candidates, start, count - 1);
    for (int end = count - 1; end > 0; --end) {
        corner_swap(candidates, 0, end);
        sift_down(candidates, 0, end - 1);
    }

    int accepted = 0;
    for (int index = count - 1; index >= 0 && accepted < max_corners;
         --index) {
        const uint packed = as_uint(candidates[index].y);
        const float x = (float)(packed >> 16);
        const float y = (float)(packed & 0xffffu);
        int good = 1;
        for (int prior = 0; prior < accepted; ++prior) {
            const float dx = x - selected_points[prior].x;
            const float dy = y - selected_points[prior].y;
            if (dx * dx + dy * dy < min_distance_sq) {
                good = 0;
                break;
            }
        }
        if (good) selected_points[accepted++] = (float2)(x, y);
    }
    selected_count[0] = accepted;
    for (int slot = accepted; slot < max_corners; ++slot)
        selected_points[slot] = (float2)(-1000.0f, -1000.0f);
}
)CLC";

std::string platform_name(cl_platform_id platform) {
    std::size_t size = 0;
    if (clGetPlatformInfo(platform, CL_PLATFORM_NAME, 0, nullptr, &size) !=
        CL_SUCCESS) {
        throw std::runtime_error("clGetPlatformInfo size failed");
    }
    std::string name(size, '\0');
    if (clGetPlatformInfo(platform, CL_PLATFORM_NAME, size, name.data(), nullptr) !=
        CL_SUCCESS) {
        throw std::runtime_error("clGetPlatformInfo name failed");
    }
    if (!name.empty() && name.back() == '\0') name.pop_back();
    return name;
}

int queue_family_index(cl_device_id device, const std::string& name) {
    std::size_t bytes = 0;
    if (clGetDeviceInfo(device, CL_DEVICE_QUEUE_FAMILY_PROPERTIES_INTEL,
                        0, nullptr, &bytes) != CL_SUCCESS ||
        bytes < sizeof(cl_queue_family_properties_intel)) return -1;
    std::vector<cl_queue_family_properties_intel> families(
        bytes / sizeof(cl_queue_family_properties_intel));
    if (clGetDeviceInfo(device, CL_DEVICE_QUEUE_FAMILY_PROPERTIES_INTEL,
                        bytes, families.data(), nullptr) != CL_SUCCESS) {
        return -1;
    }
    for (std::size_t index = 0; index < families.size(); ++index) {
        if (families[index].count > 0 &&
            std::string(families[index].name) == name) {
            return static_cast<int>(index);
        }
    }
    return -1;
}

struct GpuMotionGate {
    struct GrayFrame {
        std::int64_t id;
        cv::UMat gray;
    };

    cv::Ptr<cv::CLAHE> clahe;
    cv::Ptr<cv::CLAHE> cpu_clahe;
    cv::Ptr<cv::SparsePyrLKOpticalFlow> lk;
    cv::ocl::Kernel analyze;
    cv::ocl::Kernel redetect_reduce_max;
    cv::ocl::Kernel redetect_prepare;
    cv::ocl::Kernel redetect_collect;
    cv::ocl::Kernel redetect_sort_filter;
    cv::UMat previous_gray;
    cv::UMat previous_points;
    cv::UMat cpu_points_umat;
    cv::UMat redetect_eigen;
    cv::UMat redetect_group_max;
    cv::UMat redetect_threshold;
    cv::UMat redetect_candidates;
    cv::UMat redetect_candidate_count;
    cv::UMat redetect_selected_count;
    cl_mem cpu_points_buffer = nullptr;
    clEnqueueAcquireVA_APIMediaSurfacesINTEL_fn acquire_va = nullptr;
    clEnqueueReleaseVA_APIMediaSurfacesINTEL_fn release_va = nullptr;
    cl_command_queue opencl_queue = nullptr;
    cv::ocl::OpenCLExecutionContext profiling_execution_context;
    cl_command_queue interop_queue = nullptr;
    int interop_family = -1;
    bool interop_high_priority = false;
    bool interop_user_sync = false;
    std::vector<std::uint8_t> cpu_gray_storage;
    int point_count = 0;
    int frame_count = 0;
    int redetect_group_count = 1;
    std::size_t redetect_local_size = 1;
    std::deque<GrayFrame> async_history;

    std::array<double, 4> acquire_event_ms{{-1.0, -1.0, -1.0, -1.0}};

    void record_acquire_event(cl_event event) {
        cl_ulong queued = 0, submitted = 0, started = 0, ended = 0;
        const cl_int status =
            clGetEventProfilingInfo(event, CL_PROFILING_COMMAND_QUEUED,
                                    sizeof(queued), &queued, nullptr) |
            clGetEventProfilingInfo(event, CL_PROFILING_COMMAND_SUBMIT,
                                    sizeof(submitted), &submitted, nullptr) |
            clGetEventProfilingInfo(event, CL_PROFILING_COMMAND_START,
                                    sizeof(started), &started, nullptr) |
            clGetEventProfilingInfo(event, CL_PROFILING_COMMAND_END,
                                    sizeof(ended), &ended, nullptr);
        if (status == CL_SUCCESS && queued <= submitted &&
            submitted <= started && started <= ended) {
            acquire_event_ms = {{
                (submitted - queued) / 1e6,
                (started - submitted) / 1e6,
                (ended - started) / 1e6,
                (ended - queued) / 1e6,
            }};
        }
    }

    explicit GpuMotionGate(cl_context context) {
        std::size_t device_bytes = 0;
        if (clGetContextInfo(context, CL_CONTEXT_DEVICES, 0, nullptr,
                             &device_bytes) != CL_SUCCESS ||
            device_bytes < sizeof(cl_device_id)) {
            throw std::runtime_error("could not query OpenCL context device");
        }
        std::vector<cl_device_id> devices(device_bytes / sizeof(cl_device_id));
        if (clGetContextInfo(context, CL_CONTEXT_DEVICES, device_bytes,
                             devices.data(), nullptr) != CL_SUCCESS) {
            throw std::runtime_error("could not read OpenCL context devices");
        }
        cl_platform_id platform = nullptr;
        if (clGetDeviceInfo(devices.front(), CL_DEVICE_PLATFORM, sizeof(platform),
                            &platform, nullptr) != CL_SUCCESS) {
            throw std::runtime_error("could not query OpenCL platform");
        }

        cl_uint compute_units = 0;
        std::size_t max_work_group = 0;
        if (clGetDeviceInfo(devices.front(), CL_DEVICE_MAX_COMPUTE_UNITS,
                            sizeof(compute_units), &compute_units, nullptr) !=
                CL_SUCCESS ||
            clGetDeviceInfo(devices.front(), CL_DEVICE_MAX_WORK_GROUP_SIZE,
                            sizeof(max_work_group), &max_work_group, nullptr) !=
                CL_SUCCESS ||
            compute_units == 0 || max_work_group == 0) {
            throw std::runtime_error(
                "could not query GPU reduction work-group limits");
        }
        redetect_group_count = static_cast<int>(compute_units);
        while (redetect_local_size * 2 <= max_work_group &&
               redetect_local_size * 2 <= 256) {
            redetect_local_size *= 2;
        }
        cv::ocl::attachContext(platform_name(platform), platform, context,
                               devices.front());
        cv::ocl::setUseOpenCL(true);
        if (!cv::ocl::useOpenCL()) {
            throw std::runtime_error("OpenCV did not activate OpenCL");
        }

        if (std::getenv("MOTION_GATE_PROFILE_QUEUE")) {
            const cv::ocl::Queue& profiling_queue =
                cv::ocl::Queue::getDefault().getProfilingQueue();
            profiling_execution_context =
                cv::ocl::OpenCLExecutionContext::getCurrent().cloneWithNewQueue(
                    profiling_queue);
            profiling_execution_context.bind();
        }
        opencl_queue = reinterpret_cast<cl_command_queue>(
            cv::ocl::Queue::getDefault().ptr());
        cl_command_queue_properties queue_properties = 0;
        if (!opencl_queue ||
            clGetCommandQueueInfo(opencl_queue, CL_QUEUE_PROPERTIES,
                                  sizeof(queue_properties), &queue_properties,
                                  nullptr) != CL_SUCCESS) {
            throw std::runtime_error("could not query OpenCV OpenCL queue");
        }
        if (queue_properties & CL_QUEUE_OUT_OF_ORDER_EXEC_MODE_ENABLE) {
            throw std::runtime_error(
                "VA/OpenCL interop requires an in-order OpenCV queue");
        }

        interop_family = queue_family_index(devices.front(), "rcs");
        if (interop_family < 0) {
            throw std::runtime_error("Intel rcs queue family is unavailable");
        }
        const cl_queue_properties interop_properties[] = {
            CL_QUEUE_PROPERTIES, CL_QUEUE_PROFILING_ENABLE,
            CL_QUEUE_FAMILY_INTEL,
            static_cast<cl_queue_properties>(interop_family),
            CL_QUEUE_INDEX_INTEL, 0,
            CL_QUEUE_PRIORITY_KHR, CL_QUEUE_PRIORITY_HIGH_KHR, 0};
        cl_int interop_status = CL_SUCCESS;
        interop_queue = clCreateCommandQueueWithProperties(
            context, devices.front(), interop_properties, &interop_status);
        if (interop_status != CL_SUCCESS || !interop_queue) {
            const cl_queue_properties fallback_properties[] = {
                CL_QUEUE_PROPERTIES, CL_QUEUE_PROFILING_ENABLE,
                CL_QUEUE_FAMILY_INTEL,
                static_cast<cl_queue_properties>(interop_family),
                CL_QUEUE_INDEX_INTEL, 0, 0};
            interop_queue = clCreateCommandQueueWithProperties(
                context, devices.front(), fallback_properties, &interop_status);
        }
        if (interop_status != CL_SUCCESS || !interop_queue) {
            throw std::runtime_error(
                "could not create dedicated VA interop queue");
        }

        acquire_va =
            reinterpret_cast<clEnqueueAcquireVA_APIMediaSurfacesINTEL_fn>(
                clGetExtensionFunctionAddressForPlatform(
                    platform, "clEnqueueAcquireVA_APIMediaSurfacesINTEL"));
        release_va =
            reinterpret_cast<clEnqueueReleaseVA_APIMediaSurfacesINTEL_fn>(
                clGetExtensionFunctionAddressForPlatform(
                    platform, "clEnqueueReleaseVA_APIMediaSurfacesINTEL"));
        if (!acquire_va || !release_va) {
            throw std::runtime_error(
                "Intel VA/OpenCL acquire/release extension is unavailable");
        }

        std::size_t property_bytes = 0;
        if (clGetContextInfo(context, CL_CONTEXT_PROPERTIES, 0, nullptr,
                             &property_bytes) != CL_SUCCESS) {
            throw std::runtime_error("could not query OpenCL context properties");
        }
        std::vector<cl_context_properties> properties(
            property_bytes / sizeof(cl_context_properties));
        if (property_bytes &&
            clGetContextInfo(context, CL_CONTEXT_PROPERTIES, property_bytes,
                             properties.data(), nullptr) != CL_SUCCESS) {
            throw std::runtime_error("could not read OpenCL context properties");
        }
        for (std::size_t i = 0; i + 1 < properties.size() &&
                                properties[i] != 0;
             i += 2) {
            if (properties[i] == CL_CONTEXT_INTEROP_USER_SYNC) {
                interop_user_sync = properties[i + 1] == CL_TRUE;
                break;
            }
        }
        if (interop_user_sync) {
            throw std::runtime_error(
                "VA OpenCL context requires user synchronization");
        }

        clahe = cv::createCLAHE(2.0, cv::Size(8, 8));
        cpu_clahe = cv::createCLAHE(2.0, cv::Size(8, 8));
        lk = cv::SparsePyrLKOpticalFlow::create(
            cv::Size(15, 15), 2,
            cv::TermCriteria(cv::TermCriteria::COUNT | cv::TermCriteria::EPS,
                             10, 0.03));
        cv::String build_error;
        analyze = cv::ocl::Kernel(
            "analyze_and_compact", cv::ocl::ProgramSource(analysis_source),
            cv::String(), &build_error);
        if (analyze.empty()) {
            throw std::runtime_error("motion analysis kernel build failed: " +
                                     std::string(build_error));
        }

        cv::ocl::ProgramSource redetect_program(gpu_redetect_source);
        build_error.clear();
        redetect_reduce_max = cv::ocl::Kernel(
            "redetect_reduce_max", redetect_program, cv::String(), &build_error);
        redetect_prepare = cv::ocl::Kernel(
            "redetect_prepare", redetect_program, cv::String(), &build_error);
        redetect_collect = cv::ocl::Kernel(
            "redetect_collect", redetect_program, cv::String(), &build_error);
        redetect_sort_filter = cv::ocl::Kernel(
            "redetect_sort_filter", redetect_program, cv::String(), &build_error);
        if (redetect_reduce_max.empty() || redetect_prepare.empty() ||
            redetect_collect.empty() ||
            redetect_sort_filter.empty()) {
            throw std::runtime_error(
                "GPU redetection kernel build failed: " +
                std::string(build_error));
        }
        cl_int buffer_status = CL_SUCCESS;
        cpu_points_buffer = clCreateBuffer(
            context, CL_MEM_READ_WRITE,
            kMaxCorners * sizeof(float) * 2, nullptr, &buffer_status);
        if (buffer_status != CL_SUCCESS || !cpu_points_buffer) {
            throw std::runtime_error("could not allocate persistent point buffer");
        }
        cv::ocl::convertFromBuffer(
            reinterpret_cast<void*>(cpu_points_buffer),
            kMaxCorners * sizeof(float) * 2, 1, kMaxCorners,
            CV_32FC2, cpu_points_umat);
    }

    struct SurfaceOwnership {
        GpuMotionGate& gate;
        cl_mem image;
        bool active = false;
        double acquire_ms = 0.0;

        SurfaceOwnership(GpuMotionGate& owner, cl_mem surface_image,
                         bool profile_acquire)
            : gate(owner), image(surface_image) {
            using OwnershipClock = std::chrono::steady_clock;
            const auto start = OwnershipClock::now();
            cl_event acquire_event = nullptr;
            const cl_int status = gate.acquire_va(
                gate.opencl_queue, 1, &image, 0, nullptr,
                profile_acquire ? &acquire_event : nullptr);
            if (status != CL_SUCCESS) {
                throw std::runtime_error(
                    "clEnqueueAcquireVA_APIMediaSurfacesINTEL failed: " +
                    std::to_string(status));
            }
            active = true;
            if (profile_acquire) {
                if (clFinish(gate.opencl_queue) != CL_SUCCESS) {
                    throw std::runtime_error(
                        "OpenCL finish after VA acquire failed");
                }
                if (acquire_event) {
                    gate.record_acquire_event(acquire_event);
                    clReleaseEvent(acquire_event);
                }
                acquire_ms =
                    std::chrono::duration<double, std::milli>(
                        OwnershipClock::now() - start).count();
            }
        }

        double release_checked() {
            using OwnershipClock = std::chrono::steady_clock;
            const auto start = OwnershipClock::now();
            const cl_int status = gate.release_va(
                gate.opencl_queue, 1, &image, 0, nullptr, nullptr);
            if (status != CL_SUCCESS) {
                throw std::runtime_error(
                    "clEnqueueReleaseVA_APIMediaSurfacesINTEL failed: " +
                    std::to_string(status));
            }
            active = false;
            if (clFinish(gate.opencl_queue) != CL_SUCCESS) {
                throw std::runtime_error(
                    "OpenCL finish after VA release failed");
            }
            return std::chrono::duration<double, std::milli>(
                       OwnershipClock::now() - start).count();
        }

        ~SurfaceOwnership() {
            if (!active) return;
            gate.release_va(gate.opencl_queue, 1, &image, 0, nullptr, nullptr);
            clFinish(gate.opencl_queue);
        }
    };

    ~GpuMotionGate() {
        if (interop_queue) {
            clFinish(interop_queue);
            clReleaseCommandQueue(interop_queue);
        }
        previous_points.release();
        cpu_points_umat.release();
        if (cpu_points_buffer) clReleaseMemObject(cpu_points_buffer);
    }

    cv::UMat copy_and_release_va(cl_mem y_image) {
        cv::UMat raw_gray(180, 320, CV_8UC1);
        cl_mem raw_buffer = reinterpret_cast<cl_mem>(
            raw_gray.handle(cv::ACCESS_WRITE));
        cl_event release_event = nullptr;
        cl_int status = acquire_va(
            interop_queue, 1, &y_image, 0, nullptr, nullptr);
        if (status != CL_SUCCESS) {
            throw std::runtime_error(
                "split queue VA acquire failed: " + std::to_string(status));
        }
        const std::size_t origin[3] = {0, 0, 0};
        const std::size_t region[3] = {320, 180, 1};
        status = clEnqueueCopyImageToBuffer(
            interop_queue, y_image, raw_buffer, origin, region,
            raw_gray.offset, 0, nullptr, nullptr);
        if (status == CL_SUCCESS) {
            status = release_va(
                interop_queue, 1, &y_image, 0, nullptr, &release_event);
        }
        if (status != CL_SUCCESS || !release_event) {
            release_va(interop_queue, 1, &y_image, 0, nullptr, nullptr);
            clFinish(interop_queue);
            throw std::runtime_error(
                "split queue VA copy/release failed: " +
                std::to_string(status));
        }
        if (clFlush(interop_queue) != CL_SUCCESS ||
            clEnqueueBarrierWithWaitList(
                opencl_queue, 1, &release_event, nullptr) != CL_SUCCESS) {
            clReleaseEvent(release_event);
            throw std::runtime_error("split queue cross-queue barrier failed");
        }
        clReleaseEvent(release_event);
        return raw_gray;
    }

    void detect_features_lean(const cv::UMat& gray) {
        // Lean GPU redetect: eigen + 3x3 local-max + threshold stay on GPU
        // (fully parallel, no atomics); only the few hundred surviving
        // candidate coords (~2KB) come down, and the exact Shi-Tomasi
        // strength-sort + greedy min-distance selection (300 / 0.01 / 7px,
        // same as the tuned CPU baseline) runs on CPU where serial
        // selection is trivially fast. No sort kernels, no count readback.
        cv::cornerMinEigenVal(gray, redetect_eigen, 7, 3, cv::BORDER_DEFAULT);
        double max_val = 0.0;
        cv::minMaxLoc(redetect_eigen, nullptr, &max_val, nullptr, nullptr,
                       cv::noArray());
        const float thresh = static_cast<float>(max_val * 0.01);
        cv::UMat dilated, is_max, strong, keep;
        cv::dilate(redetect_eigen, dilated, cv::Mat());
        cv::compare(redetect_eigen, dilated, is_max, cv::CMP_EQ);
        cv::compare(redetect_eigen, thresh, strong, cv::CMP_GT);
        cv::bitwise_and(is_max, strong, keep);
        std::vector<cv::Point> cand;
        cv::findNonZero(keep, cand);
        cv::Mat eig = redetect_eigen.getMat(cv::ACCESS_READ);
        struct Scored { float v; int x; int y; };
        std::vector<Scored> scored;
        scored.reserve(cand.size());
        for (const auto& pt : cand) {
            if (pt.x < 4 || pt.y < 4 || pt.x >= 316 || pt.y >= 176) continue;
            scored.push_back({eig.at<float>(pt.y, pt.x), pt.x, pt.y});
        }
        std::sort(scored.begin(), scored.end(),
                  [](const Scored& a, const Scored& b) { return a.v > b.v; });
        std::vector<cv::Point2f> accepted;
        accepted.reserve(kMaxCorners);
        for (const auto& s : scored) {
            if (static_cast<int>(accepted.size()) >= kMaxCorners) break;
            bool ok = true;
            for (const auto& a : accepted) {
                const float dx = s.x - a.x, dy = s.y - a.y;
                if (dx * dx + dy * dy < 49.0F) { ok = false; break; }
            }
            if (ok) accepted.emplace_back(static_cast<float>(s.x),
                                          static_cast<float>(s.y));
        }
        point_count = static_cast<int>(accepted.size());
        if (point_count > 0) {
            const std::size_t upload_bytes =
                accepted.size() * sizeof(cv::Point2f);
            auto queue = reinterpret_cast<cl_command_queue>(
                cv::ocl::Queue::getDefault().ptr());
            if (clEnqueueWriteBuffer(queue, cpu_points_buffer, CL_TRUE, 0,
                                     upload_bytes, accepted.data(),
                                     0, nullptr, nullptr) != CL_SUCCESS) {
                throw std::runtime_error("lean redetect point upload failed");
            }
            previous_points = cpu_points_umat.colRange(0, point_count);
        } else {
            previous_points.release();
        }
    }

    void detect_features(const cv::UMat& gray) {
        if (std::getenv("MOTION_GATE_REDETECT_LEAN")) {
            detect_features_lean(gray);
            return;
        }
        if (std::getenv("MOTION_GATE_REDETECT_CPU")) {
            // CPU corner-find: same routine and thresholds as the tuned CPU
            // baseline (300 / 0.01 / 7px). One 57KB download every 25th
            // frame (~0.3ms find + transfer) replaces ~18ms of GPU
            // collect/sort kernels with identical semantics.
            cv::Mat host_gray = gray.getMat(cv::ACCESS_READ);
            if (host_gray.empty()) {
                throw std::runtime_error("CPU redetection got empty frame");
            }
            cv::Mat cpu_points;
            cv::goodFeaturesToTrack(host_gray, cpu_points, kMaxCorners, 0.01,
                                    7.0, cv::noArray(), 7, false, 0.04);
            point_count = static_cast<int>(cpu_points.total());
            if (point_count > 0) {
                const std::size_t upload_bytes =
                    cpu_points.total() * cpu_points.elemSize();
                auto queue = reinterpret_cast<cl_command_queue>(
                    cv::ocl::Queue::getDefault().ptr());
                if (clEnqueueWriteBuffer(queue, cpu_points_buffer, CL_TRUE, 0,
                                         upload_bytes, cpu_points.ptr(),
                                         0, nullptr, nullptr) != CL_SUCCESS) {
                    throw std::runtime_error(
                        "CPU redetect point upload failed");
                }
                previous_points = cpu_points_umat.colRange(0, point_count);
            } else {
                previous_points.release();
            }
            return;
        }
        cv::cornerMinEigenVal(gray, redetect_eigen, 7, 3, cv::BORDER_DEFAULT);
        const int capacity = gray.rows * gray.cols;
        redetect_group_max.create(1, redetect_group_count, CV_32FC1);
        redetect_threshold.create(1, 1, CV_32FC1);
        redetect_candidates.create(1, capacity, CV_32FC2);
        redetect_candidate_count.create(1, 1, CV_32SC1);
        redetect_selected_count.create(1, 1, CV_32SC1);

        const cl_mem eigen_mem = reinterpret_cast<cl_mem>(
            redetect_eigen.handle(cv::ACCESS_READ));
        const cl_mem group_max_mem = reinterpret_cast<cl_mem>(
            redetect_group_max.handle(cv::ACCESS_WRITE));
        const cl_mem threshold_mem = reinterpret_cast<cl_mem>(
            redetect_threshold.handle(cv::ACCESS_WRITE));
        const cl_mem candidates_mem = reinterpret_cast<cl_mem>(
            redetect_candidates.handle(cv::ACCESS_WRITE));
        const cl_mem candidate_count_mem = reinterpret_cast<cl_mem>(
            redetect_candidate_count.handle(cv::ACCESS_WRITE));
        const cl_mem points_mem = reinterpret_cast<cl_mem>(
            cpu_points_umat.handle(cv::ACCESS_WRITE));
        const cl_mem selected_count_mem = reinterpret_cast<cl_mem>(
            redetect_selected_count.handle(cv::ACCESS_WRITE));
        const int eigen_step = static_cast<int>(redetect_eigen.step[0]);
        const int eigen_offset = static_cast<int>(redetect_eigen.offset);
        const int rows = redetect_eigen.rows;
        const int cols = redetect_eigen.cols;
        const float quality_level = 0.01F;
        const float min_distance_sq = 49.0F;

        auto set_arg = [](cl_kernel kernel, cl_uint index,
                          std::size_t size, const void* value) {
            const cl_int status = clSetKernelArg(kernel, index, size, value);
            if (status != CL_SUCCESS) {
                throw std::runtime_error(
                    "GPU redetection clSetKernelArg failed: " +
                    std::to_string(status));
            }
        };
        auto enqueue = [&](cl_kernel kernel, cl_uint dimensions,
                           const std::size_t* global,
                           const std::size_t* local = nullptr) {
            const cl_int status = clEnqueueNDRangeKernel(
                opencl_queue, kernel, dimensions, nullptr, global, local,
                0, nullptr, nullptr);
            if (status != CL_SUCCESS) {
                throw std::runtime_error(
                    "GPU redetection enqueue failed: " +
                    std::to_string(status));
            }
        };

        cl_kernel reduce =
            reinterpret_cast<cl_kernel>(redetect_reduce_max.ptr());
        set_arg(reduce, 0, sizeof(eigen_mem), &eigen_mem);
        set_arg(reduce, 1, sizeof(eigen_step), &eigen_step);
        set_arg(reduce, 2, sizeof(eigen_offset), &eigen_offset);
        set_arg(reduce, 3, sizeof(rows), &rows);
        set_arg(reduce, 4, sizeof(cols), &cols);
        set_arg(reduce, 5, sizeof(group_max_mem), &group_max_mem);
        const std::size_t reduce_global =
            static_cast<std::size_t>(redetect_group_count) *
            redetect_local_size;
        enqueue(reduce, 1, &reduce_global, &redetect_local_size);

        cl_kernel prepare =
            reinterpret_cast<cl_kernel>(redetect_prepare.ptr());
        set_arg(prepare, 0, sizeof(group_max_mem), &group_max_mem);
        set_arg(prepare, 1, sizeof(redetect_group_count),
                &redetect_group_count);
        set_arg(prepare, 2, sizeof(quality_level), &quality_level);
        set_arg(prepare, 3, sizeof(threshold_mem), &threshold_mem);
        set_arg(prepare, 4, sizeof(candidate_count_mem),
                &candidate_count_mem);
        const std::size_t one = 1;
        enqueue(prepare, 1, &one);

        cl_kernel collect =
            reinterpret_cast<cl_kernel>(redetect_collect.ptr());
        set_arg(collect, 0, sizeof(eigen_mem), &eigen_mem);
        set_arg(collect, 1, sizeof(eigen_step), &eigen_step);
        set_arg(collect, 2, sizeof(eigen_offset), &eigen_offset);
        set_arg(collect, 3, sizeof(rows), &rows);
        set_arg(collect, 4, sizeof(cols), &cols);
        set_arg(collect, 5, sizeof(threshold_mem), &threshold_mem);
        set_arg(collect, 6, sizeof(candidates_mem), &candidates_mem);
        set_arg(collect, 7, sizeof(candidate_count_mem), &candidate_count_mem);
        set_arg(collect, 8, sizeof(capacity), &capacity);
        const std::size_t collect_global[2] = {
            static_cast<std::size_t>(cols - 2),
            static_cast<std::size_t>(rows - 2)};
        enqueue(collect, 2, collect_global);

        cl_kernel sort_filter =
            reinterpret_cast<cl_kernel>(redetect_sort_filter.ptr());
        set_arg(sort_filter, 0, sizeof(candidates_mem), &candidates_mem);
        set_arg(sort_filter, 1, sizeof(candidate_count_mem),
                &candidate_count_mem);
        set_arg(sort_filter, 2, sizeof(points_mem), &points_mem);
        set_arg(sort_filter, 3, sizeof(selected_count_mem),
                &selected_count_mem);
        set_arg(sort_filter, 4, sizeof(capacity), &capacity);
        set_arg(sort_filter, 5, sizeof(kMaxCorners), &kMaxCorners);
        set_arg(sort_filter, 6, sizeof(min_distance_sq), &min_distance_sq);
        enqueue(sort_filter, 1, &one);

        if (std::getenv("MOTION_GATE_REDETECT_FIXED_SLOTS")) {
            point_count = kMaxCorners;
            previous_points = cpu_points_umat;
            return;
        }
        if (point_count > 0 && std::getenv("MOTION_GATE_REDETECT_NO_READBACK")) {
            return;
        }
        const cl_bool blocking_read = std::getenv("MOTION_GATE_REDETECT_ASYNC_READBACK")
                                          ? CL_FALSE : CL_TRUE;
        const cl_int read_status = clEnqueueReadBuffer(
            opencl_queue, selected_count_mem, blocking_read,
            redetect_selected_count.offset, sizeof(point_count),
            &point_count, 0, nullptr, nullptr);
        if (read_status == CL_SUCCESS && !blocking_read &&
            clFinish(opencl_queue) != CL_SUCCESS) {
            throw std::runtime_error(
                "OpenCL finish after asynchronous selected-count read failed");
        }
        if (read_status != CL_SUCCESS ||
            point_count < 0 || point_count > kMaxCorners) {
            throw std::runtime_error(
                "GPU redetection returned invalid point count");
        }
        previous_points = point_count > 0
                              ? cpu_points_umat.colRange(0, point_count)
                              : cv::UMat();
    }

    void detect_features_cpu_host(const std::uint8_t* host_gray,
                                  int rows, int cols,
                                  double* hybrid_stage_ms) {
        using HybridClock = std::chrono::steady_clock;
        const std::size_t gray_bytes =
            static_cast<std::size_t>(rows) * cols;
        if (!host_gray || gray_bytes == 0) {
            throw std::runtime_error("missing host grayscale for CPU redetection");
        }
        auto queue = reinterpret_cast<cl_command_queue>(
            cv::ocl::Queue::getDefault().ptr());

        cv::Mat cpu_raw(rows, cols, CV_8UC1,
                        const_cast<std::uint8_t*>(host_gray), cols);
        cv::Mat cpu_gray;
        auto start = HybridClock::now();
        cpu_clahe->apply(cpu_raw, cpu_gray);
        hybrid_stage_ms[1] = std::chrono::duration<double, std::milli>(
                                 HybridClock::now() - start)
                                 .count();

        cv::Mat cpu_points;
        start = HybridClock::now();
        cv::goodFeaturesToTrack(cpu_gray, cpu_points, kMaxCorners, 0.01, 7.0,
                                cv::noArray(), 7, false, 0.04);
        hybrid_stage_ms[2] = std::chrono::duration<double, std::milli>(
                                 HybridClock::now() - start)
                                 .count();

        start = HybridClock::now();
        point_count = static_cast<int>(cpu_points.total());
        if (point_count > 0) {
            const std::size_t upload_bytes =
                cpu_points.total() * cpu_points.elemSize();
            const cl_int upload_status = clEnqueueWriteBuffer(
                queue, cpu_points_buffer, CL_TRUE, 0, upload_bytes,
                cpu_points.ptr(), 0, nullptr, nullptr);
            if (upload_status != CL_SUCCESS) {
                throw std::runtime_error("direct point-buffer upload failed");
            }
            previous_points = cpu_points_umat.colRange(0, point_count);
        } else {
            previous_points.release();
        }
        hybrid_stage_ms[3] = std::chrono::duration<double, std::milli>(
                                 HybridClock::now() - start)
                                 .count();
    }

    void reset() {
        previous_gray.release();
        previous_points.release();
        point_count = 0;
        frame_count = 0;
        async_history.clear();
        cv::ocl::Queue::getDefault().finish();
    }

    int track_points(const cv::UMat& from_gray, const cv::UMat& to_gray,
                     int* candidate_out = nullptr) {
        if (point_count <= 0) {
            if (candidate_out) *candidate_out = 0;
            return 0;
        }
        cv::UMat next_points;
        cv::UMat status;
        cv::UMat error;
        lk->calc(from_gray, to_gray, previous_points, next_points, status,
                 error);

        cv::UMat compact_points(1, kMaxCorners, CV_32FC2);
        cv::UMat result(1, 2, CV_32SC1);
        analyze.args(cv::ocl::KernelArg::PtrReadOnly(previous_points),
                     cv::ocl::KernelArg::PtrReadOnly(next_points),
                     cv::ocl::KernelArg::PtrReadOnly(status), point_count,
                     cv::ocl::KernelArg::PtrWriteOnly(compact_points),
                     cv::ocl::KernelArg::PtrWriteOnly(result));
        std::size_t global = 1;
        if (!analyze.run(1, &global, nullptr, true)) {
            throw std::runtime_error("motion analysis kernel failed");
        }
        const cv::Mat host_result = result.getMat(cv::ACCESS_READ);
        point_count = host_result.at<int>(0, 0);
        if (candidate_out) *candidate_out = host_result.at<int>(0, 1);
        previous_points = point_count > 0
                              ? compact_points.colRange(0, point_count)
                              : cv::UMat();
        return point_count;
    }

    int update_async(cl_mem y_image, std::int64_t frame_id,
                     int* output_points) {
        SurfaceOwnership ownership(*this, y_image, false);
        cv::UMat raw_full;
        cv::ocl::convertFromImage(reinterpret_cast<void*>(y_image), raw_full);
        // Input contract is 320x180: every threshold below is tuned in those
        // pixels. Downscale on-chip when a full-res VA surface is fed
        // (single-appsink pipelines); a 1:1 wrap would run 6x-hot thresholds.
        cv::UMat raw_gray;
        if (raw_full.cols == 320 && raw_full.rows == 180) {
            raw_gray = raw_full;
        } else {
            cv::resize(raw_full, raw_gray, cv::Size(320, 180), 0, 0,
                       cv::INTER_LINEAR);
        }
        cv::UMat gray;
        clahe->apply(raw_gray, gray);

        int candidate = 0;
        if (!previous_gray.empty() && point_count > 0) {
            track_points(previous_gray, gray, &candidate);
        }
        previous_gray = gray;
        async_history.push_back({frame_id, gray});
        while (async_history.size() > kAsyncHistoryFrames) {
            async_history.pop_front();
        }
        ++frame_count;
        ownership.release_checked();
        if (output_points) *output_points = point_count;
        return candidate;
    }

    bool install_async_points(const float* host_points, int count,
                              std::int64_t source_frame, int* lag_frames,
                              int* output_points, double* upload_ms,
                              double* catchup_ms) {
        using AsyncClock = std::chrono::steady_clock;
        if (!host_points || count < 0 || count > kMaxCorners) {
            throw std::runtime_error("invalid asynchronous point result");
        }
        auto source = async_history.end();
        for (auto it = async_history.begin(); it != async_history.end(); ++it) {
            if (it->id == source_frame) {
                source = it;
                break;
            }
        }
        if (source == async_history.end()) {
            if (lag_frames) *lag_frames = -1;
            if (output_points) *output_points = point_count;
            if (upload_ms) *upload_ms = 0.0;
            if (catchup_ms) *catchup_ms = 0.0;
            return false;
        }

        auto queue = reinterpret_cast<cl_command_queue>(
            cv::ocl::Queue::getDefault().ptr());
        auto start = AsyncClock::now();
        point_count = count;
        if (point_count > 0) {
            const std::size_t bytes = static_cast<std::size_t>(point_count) *
                                      sizeof(float) * 2;
            const cl_int status = clEnqueueWriteBuffer(
                queue, cpu_points_buffer, CL_TRUE, 0, bytes, host_points,
                0, nullptr, nullptr);
            if (status != CL_SUCCESS) {
                throw std::runtime_error("asynchronous point upload failed");
            }
            previous_points = cpu_points_umat.colRange(0, point_count);
        } else {
            previous_points.release();
        }
        if (upload_ms) {
            *upload_ms = std::chrono::duration<double, std::milli>(
                             AsyncClock::now() - start)
                             .count();
        }

        start = AsyncClock::now();
        auto prior = source;
        for (auto next = std::next(source); next != async_history.end(); ++next) {
            if (point_count <= 0) break;
            track_points(prior->gray, next->gray, nullptr);
            prior = next;
        }
        cv::ocl::Queue::getDefault().finish();
        if (catchup_ms) {
            *catchup_ms = std::chrono::duration<double, std::milli>(
                              AsyncClock::now() - start)
                              .count();
        }
        if (lag_frames) {
            *lag_frames = static_cast<int>(async_history.back().id -
                                           source_frame);
        }
        if (output_points) *output_points = point_count;
        return true;
    }

    int update(cl_mem y_image, int* output_points,
               double* stage_ms = nullptr, int* output_redetected = nullptr) {
        using ProfileClock = std::chrono::steady_clock;
        const auto total_start = ProfileClock::now();
        acquire_event_ms = {{-1.0, -1.0, -1.0, -1.0}};
        SurfaceOwnership ownership(*this, y_image, stage_ms != nullptr);
        if (stage_ms) {
            for (int i = 0; i < 8; ++i) stage_ms[i] = 0.0;
            stage_ms[0] = ownership.acquire_ms;
        }
        if (output_redetected) *output_redetected = 0;
        auto finish_stage = [&](int index, ProfileClock::time_point start) {
            if (!stage_ms) return;
            cv::ocl::Queue::getDefault().finish();
            stage_ms[index] = std::chrono::duration<double, std::milli>(
                                  ProfileClock::now() - start)
                                  .count();
        };

        auto stage_start = ProfileClock::now();
        cv::UMat raw_full;
        cv::ocl::convertFromImage(reinterpret_cast<void*>(y_image), raw_full);
        // Input contract is 320x180: every threshold below is tuned in those
        // pixels. Downscale on-chip when a full-res VA surface is fed
        // (single-appsink pipelines); a 1:1 wrap would run 6x-hot thresholds.
        cv::UMat raw_gray;
        if (raw_full.cols == 320 && raw_full.rows == 180) {
            raw_gray = raw_full;
        } else {
            cv::resize(raw_full, raw_gray, cv::Size(320, 180), 0, 0,
                       cv::INTER_LINEAR);
        }
        finish_stage(1, stage_start);

        stage_start = ProfileClock::now();
        cv::UMat gray;
        clahe->apply(raw_gray, gray);
        finish_stage(2, stage_start);

        int candidate = 0;
        if (!previous_gray.empty() && point_count > 0) {
            cv::UMat next_points;
            cv::UMat status;
            cv::UMat error;
            stage_start = ProfileClock::now();
            lk->calc(previous_gray, gray, previous_points, next_points, status,
                     error);
            finish_stage(3, stage_start);

            stage_start = ProfileClock::now();
            cv::UMat compact_points(1, kMaxCorners, CV_32FC2);
            cv::UMat result(1, 2, CV_32SC1);
            analyze.args(cv::ocl::KernelArg::PtrReadOnly(previous_points),
                         cv::ocl::KernelArg::PtrReadOnly(next_points),
                         cv::ocl::KernelArg::PtrReadOnly(status), point_count,
                         cv::ocl::KernelArg::PtrWriteOnly(compact_points),
                         cv::ocl::KernelArg::PtrWriteOnly(result));
            std::size_t global = 1;
            if (!analyze.run(1, &global, nullptr, true)) {
                throw std::runtime_error("motion analysis kernel failed");
            }
            const cv::Mat host_result = result.getMat(cv::ACCESS_READ);
            point_count = host_result.at<int>(0, 0);
            candidate = host_result.at<int>(0, 1);
            previous_points = point_count > 0
                                  ? compact_points.colRange(0, point_count)
                                  : cv::UMat();
            finish_stage(4, stage_start);
        }

        ++frame_count;
        if (point_count == 0 || frame_count % kRedetectEvery == 1) {
            stage_start = ProfileClock::now();
            if (point_count > 0 && std::getenv("MOTION_GATE_REDETECT_CPU_DELAY")) {
                std::this_thread::sleep_for(std::chrono::milliseconds(2));
            } else if (point_count > 0 && std::getenv("MOTION_GATE_REDETECT_CORNER_ONLY")) {
                cv::cornerMinEigenVal(gray, redetect_eigen, 7, 3, cv::BORDER_DEFAULT);
            } else {
                detect_features(gray);
            }
            finish_stage(5, stage_start);
            if (output_redetected) *output_redetected = 1;
        }
        previous_gray = gray;
        const double release_ms = ownership.release_checked();
        if (stage_ms) {
            stage_ms[6] = release_ms;
            stage_ms[7] = std::chrono::duration<double, std::milli>(
                              ProfileClock::now() - total_start)
                              .count();
        }
        if (output_points) *output_points = point_count;
        return candidate;
    }

    int update_split_queue(cl_mem y_image, int* output_points,
                           double* stage_ms, int* output_redetected) {
        using SplitClock = std::chrono::steady_clock;
        const auto total_start = SplitClock::now();
        for (int i = 0; i < 5; ++i) stage_ms[i] = 0.0;
        *output_redetected = 0;

        auto start = SplitClock::now();
        cv::UMat raw_gray = copy_and_release_va(y_image);
        stage_ms[0] = std::chrono::duration<double, std::milli>(
                          SplitClock::now() - start).count();

        start = SplitClock::now();
        cv::UMat gray;
        clahe->apply(raw_gray, gray);
        cv::ocl::Queue::getDefault().finish();
        stage_ms[1] = std::chrono::duration<double, std::milli>(
                          SplitClock::now() - start).count();

        int candidate = 0;
        if (!previous_gray.empty() && point_count > 0) {
            start = SplitClock::now();
            track_points(previous_gray, gray, &candidate);
            cv::ocl::Queue::getDefault().finish();
            stage_ms[2] = std::chrono::duration<double, std::milli>(
                              SplitClock::now() - start).count();
        }

        ++frame_count;
        if (point_count == 0 || frame_count % kRedetectEvery == 1) {
            start = SplitClock::now();
            detect_features(gray);
            cv::ocl::Queue::getDefault().finish();
            stage_ms[3] = std::chrono::duration<double, std::milli>(
                              SplitClock::now() - start).count();
            *output_redetected = 1;
        }
        previous_gray = gray;
        stage_ms[4] = std::chrono::duration<double, std::milli>(
                          SplitClock::now() - total_start).count();
        if (output_points) *output_points = point_count;
        return candidate;
    }

    int update_hybrid(cl_mem y_image,
                      const std::uint8_t* host_gray,
                      std::size_t host_gray_size,
                      int* output_points,
                      double* hybrid_stage_ms, int* output_redetected) {
        using HybridClock = std::chrono::steady_clock;
        SurfaceOwnership ownership(*this, y_image, false);
        const auto total_start = HybridClock::now();
        for (int i = 0; i < 5; ++i) hybrid_stage_ms[i] = 0.0;
        *output_redetected = 0;

        cv::UMat raw_full;
        cv::ocl::convertFromImage(reinterpret_cast<void*>(y_image), raw_full);
        // Input contract is 320x180: every threshold below is tuned in those
        // pixels. Downscale on-chip when a full-res VA surface is fed
        // (single-appsink pipelines); a 1:1 wrap would run 6x-hot thresholds.
        cv::UMat raw_gray;
        if (raw_full.cols == 320 && raw_full.rows == 180) {
            raw_gray = raw_full;
        } else {
            cv::resize(raw_full, raw_gray, cv::Size(320, 180), 0, 0,
                       cv::INTER_LINEAR);
        }
        cv::UMat gray;
        clahe->apply(raw_gray, gray);

        int candidate = 0;
        if (!previous_gray.empty() && point_count > 0) {
            cv::UMat next_points;
            cv::UMat status;
            cv::UMat error;
            lk->calc(previous_gray, gray, previous_points, next_points, status,
                     error);

            cv::UMat compact_points(1, kMaxCorners, CV_32FC2);
            cv::UMat result(1, 2, CV_32SC1);
            analyze.args(cv::ocl::KernelArg::PtrReadOnly(previous_points),
                         cv::ocl::KernelArg::PtrReadOnly(next_points),
                         cv::ocl::KernelArg::PtrReadOnly(status), point_count,
                         cv::ocl::KernelArg::PtrWriteOnly(compact_points),
                         cv::ocl::KernelArg::PtrWriteOnly(result));
            std::size_t global = 1;
            if (!analyze.run(1, &global, nullptr, true)) {
                throw std::runtime_error("motion analysis kernel failed");
            }
            const cv::Mat host_result = result.getMat(cv::ACCESS_READ);
            point_count = host_result.at<int>(0, 0);
            candidate = host_result.at<int>(0, 1);
            previous_points = point_count > 0
                                  ? compact_points.colRange(0, point_count)
                                  : cv::UMat();
        }

        ++frame_count;
        if (point_count == 0 || frame_count % kRedetectEvery == 1) {
            const std::size_t required =
                static_cast<std::size_t>(gray.rows) * gray.cols;
            if (!host_gray || host_gray_size != required) {
                throw std::runtime_error(
                    "CPU redetection frame lacks exact 320x180 grayscale");
            }
            detect_features_cpu_host(host_gray, gray.rows, gray.cols,
                                     hybrid_stage_ms);
            *output_redetected = 1;
        }
        previous_gray = gray;
        ownership.release_checked();
        hybrid_stage_ms[4] = std::chrono::duration<double, std::milli>(
                                 HybridClock::now() - total_start)
                                 .count();
        if (output_points) *output_points = point_count;
        return candidate;
    }
};

struct CpuFeatureDetector {
    cv::Ptr<cv::CLAHE> clahe = cv::createCLAHE(2.0, cv::Size(8, 8));

    int detect(const std::uint8_t* host_gray, int rows, int cols,
               float* output_points, int capacity, double* stage_ms) {
        using DetectClock = std::chrono::steady_clock;
        if (!host_gray || rows <= 0 || cols <= 0 || !output_points ||
            capacity < kMaxCorners || !stage_ms) {
            throw std::runtime_error("invalid CPU feature detector argument");
        }
        cv::Mat raw(rows, cols, CV_8UC1,
                    const_cast<std::uint8_t*>(host_gray), cols);
        cv::Mat enhanced;
        auto start = DetectClock::now();
        clahe->apply(raw, enhanced);
        stage_ms[0] = std::chrono::duration<double, std::milli>(
                          DetectClock::now() - start).count();

        cv::Mat points;
        start = DetectClock::now();
        cv::goodFeaturesToTrack(enhanced, points, kMaxCorners, 0.01, 7.0,
                                cv::noArray(), 7, false, 0.04);
        stage_ms[1] = std::chrono::duration<double, std::milli>(
                          DetectClock::now() - start).count();
        const int count = static_cast<int>(points.total());
        if (count > 0) {
            std::memcpy(output_points, points.ptr(),
                        static_cast<std::size_t>(count) * sizeof(float) * 2);
        }
        return count;
    }
};

}  // namespace

extern "C" {

void* gpu_motion_create(std::uintptr_t context_pointer) {
    try {
        auto gate = std::make_unique<GpuMotionGate>(
            reinterpret_cast<cl_context>(context_pointer));
        last_error.clear();
        return gate.release();
    } catch (const std::exception& error) {
        last_error = error.what();
        return nullptr;
    }
}

int gpu_motion_update_async(void* opaque,
                            std::uintptr_t image_pointer,
                            std::int64_t frame_id,
                            int* candidate,
                            int* point_count) {
    try {
        if (!opaque || !image_pointer || !candidate) {
            throw std::runtime_error("invalid asynchronous GPU motion argument");
        }
        auto& gate = *static_cast<GpuMotionGate*>(opaque);
        *candidate = gate.update_async(
            reinterpret_cast<cl_mem>(image_pointer), frame_id, point_count);
        last_error.clear();
        return 0;
    } catch (const std::exception& error) {
        last_error = error.what();
        return -1;
    }
}

int gpu_motion_install_async_points(void* opaque,
                                    const float* points,
                                    int count,
                                    std::int64_t source_frame,
                                    int* accepted,
                                    int* lag_frames,
                                    int* point_count,
                                    double* upload_ms,
                                    double* catchup_ms) {
    try {
        if (!opaque || !points || !accepted || !lag_frames || !point_count ||
            !upload_ms || !catchup_ms) {
            throw std::runtime_error("invalid asynchronous install argument");
        }
        auto& gate = *static_cast<GpuMotionGate*>(opaque);
        *accepted = gate.install_async_points(
            points, count, source_frame, lag_frames, point_count,
            upload_ms, catchup_ms) ? 1 : 0;
        last_error.clear();
        return 0;
    } catch (const std::exception& error) {
        last_error = error.what();
        return -1;
    }
}

void* cpu_feature_create() {
    try {
        auto detector = std::make_unique<CpuFeatureDetector>();
        last_error.clear();
        return detector.release();
    } catch (const std::exception& error) {
        last_error = error.what();
        return nullptr;
    }
}

int cpu_feature_detect(void* opaque,
                       const std::uint8_t* host_gray,
                       int rows,
                       int cols,
                       float* output_points,
                       int capacity,
                       int* point_count,
                       double* stage_ms,
                       int stage_count) {
    try {
        if (!opaque || !point_count || !stage_ms || stage_count < 2) {
            throw std::runtime_error("invalid CPU feature call");
        }
        auto& detector = *static_cast<CpuFeatureDetector*>(opaque);
        *point_count = detector.detect(
            host_gray, rows, cols, output_points, capacity, stage_ms);
        last_error.clear();
        return 0;
    } catch (const std::exception& error) {
        last_error = error.what();
        return -1;
    }
}

void cpu_feature_destroy(void* opaque) {
    delete static_cast<CpuFeatureDetector*>(opaque);
}

int gpu_motion_update(void* opaque,
                      std::uintptr_t image_pointer,
                      int* candidate,
                      int* point_count) {
    try {
        if (!opaque || !image_pointer || !candidate) {
            throw std::runtime_error("null GPU motion argument");
        }
        auto& gate = *static_cast<GpuMotionGate*>(opaque);
        *candidate = gate.update(reinterpret_cast<cl_mem>(image_pointer),
                                 point_count);
        last_error.clear();
        return 0;
    } catch (const std::exception& error) {
        last_error = error.what();
        return -1;
    }
}

int gpu_motion_update_profiled(void* opaque,
                               std::uintptr_t image_pointer,
                               int* candidate,
                               int* point_count,
                               double* stage_ms,
                               int stage_count,
                               int* redetected) {
    try {
        if (!opaque || !image_pointer || !candidate || !stage_ms ||
            stage_count < 8 || !redetected) {
            throw std::runtime_error("invalid profiled GPU motion argument");
        }
        auto& gate = *static_cast<GpuMotionGate*>(opaque);
        *candidate = gate.update(reinterpret_cast<cl_mem>(image_pointer),
                                 point_count, stage_ms, redetected);
        last_error.clear();
        return 0;
    } catch (const std::exception& error) {
        last_error = error.what();
        return -1;
    }
}

int gpu_motion_last_acquire_profile(void* opaque, double* output,
                                    int output_count) {
    try {
        if (!opaque || !output || output_count < 4) {
            throw std::runtime_error("invalid acquire profiling argument");
        }
        auto& gate = *static_cast<GpuMotionGate*>(opaque);
        for (int i = 0; i < 4; ++i) output[i] = gate.acquire_event_ms[i];
        last_error.clear();
        return 0;
    } catch (const std::exception& error) {
        last_error = error.what();
        return -1;
    }
}

int gpu_motion_update_split_queue(void* opaque,
                                  std::uintptr_t image_pointer,
                                  int* candidate,
                                  int* point_count,
                                  double* stage_ms,
                                  int stage_count,
                                  int* redetected) {
    try {
        if (!opaque || !image_pointer || !candidate || !stage_ms ||
            stage_count < 5 || !redetected) {
            throw std::runtime_error("invalid split-queue motion argument");
        }
        auto& gate = *static_cast<GpuMotionGate*>(opaque);
        *candidate = gate.update_split_queue(
            reinterpret_cast<cl_mem>(image_pointer), point_count,
            stage_ms, redetected);
        last_error.clear();
        return 0;
    } catch (const std::exception& error) {
        last_error = error.what();
        return -1;
    }
}

int gpu_motion_update_hybrid(void* opaque,
                             std::uintptr_t image_pointer,
                             const std::uint8_t* host_gray,
                             std::size_t host_gray_size,
                             int* candidate,
                             int* point_count,
                             double* hybrid_stage_ms,
                             int stage_count,
                             int* redetected) {
    try {
        if (!opaque || !image_pointer || !candidate || !hybrid_stage_ms ||
            stage_count < 5 || !redetected) {
            throw std::runtime_error("invalid hybrid GPU motion argument");
        }
        auto& gate = *static_cast<GpuMotionGate*>(opaque);
        *candidate = gate.update_hybrid(
            reinterpret_cast<cl_mem>(image_pointer), host_gray, host_gray_size,
            point_count,
            hybrid_stage_ms, redetected);
        last_error.clear();
        return 0;
    } catch (const std::exception& error) {
        last_error = error.what();
        return -1;
    }
}

int gpu_motion_opencl_enabled(void*) {
    return cv::ocl::useOpenCL() ? 1 : 0;
}

void gpu_motion_reset(void* opaque) {
    if (opaque) {
        static_cast<GpuMotionGate*>(opaque)->reset();
    }
}

void gpu_motion_destroy(void* opaque) {
    if (!opaque) return;
    // Destroy all UMats/kernels first, while their attached VA context is alive.
    cv::ocl::Queue::getDefault().finish();
    delete static_cast<GpuMotionGate*>(opaque);
    // Kernel callbacks defer UMatData destruction in OpenCV's allocator queue.
    // A plain UMat allocation drains that queue while the VA bridge/Core is alive.
    // The flush UMat never enters a kernel, so its own release is synchronous.
    {
        cv::UMat cleanup_flush(1, 1, CV_8UC1);
    }
    for (const char* pool : {"OCL", "HOST_ALLOC"}) {
        auto* controller = cv::ocl::getOpenCLAllocator()->getBufferPoolController(pool);
        if (controller) controller->freeAllReservedBuffers();
    }
    // attachContext retains a thread-local context and queue beyond gate lifetime.
    // Release them before the caller destroys the VA bridge/decoder display.
    cv::ocl::OpenCLExecutionContext::getCurrentRef().release();
}

const char* gpu_motion_last_error() {
    return last_error.c_str();
}

}  // extern "C"
