#include <openvino/openvino.hpp>
#include <openvino/opsets/opset13.hpp>
#include <openvino/core/preprocess/pre_post_process.hpp>
#include <openvino/runtime/intel_gpu/ocl/va.hpp>
#include <CL/cl_va_api_media_sharing_intel.h>
#include <gst/gst.h>
#include <va/va_vpp.h>


#include <cstddef>
#include <chrono>
#include <cstdint>
#include <cstring>
#include <exception>
#include <memory>
#include <string>

namespace {

thread_local std::string last_error;

struct YCopyBridge {
    ov::Core core;
    ov::intel_gpu::ocl::VAContext context;
    cl::CommandQueue queue;
    clEnqueueAcquireVA_APIMediaSurfacesINTEL_fn acquire_va = nullptr;
    clEnqueueReleaseVA_APIMediaSurfacesINTEL_fn release_va = nullptr;
    bool interop_user_sync = false;
    VADisplay display;
    std::size_t height;
    std::size_t width;

    static cl::CommandQueue create_queue(ov::intel_gpu::ocl::VAContext& context) {
        cl::Context cl_context = context;
        auto devices = cl_context.getInfo<CL_CONTEXT_DEVICES>();
        if (devices.empty()) {
            throw std::runtime_error("VA OpenCL context contains no devices");
        }
        return cl::CommandQueue(cl_context, devices.front());
    }

    YCopyBridge(VADisplay display, std::size_t h, std::size_t w)
        : context(core, display),
          queue(create_queue(context)),
          display(display),
          height(h),
          width(w) {
        cl::Context cl_context = context;
        const auto devices = cl_context.getInfo<CL_CONTEXT_DEVICES>();
        cl_platform_id platform = nullptr;
        if (devices.empty() ||
            clGetDeviceInfo(devices.front().get(), CL_DEVICE_PLATFORM,
                            sizeof(platform), &platform, nullptr) != CL_SUCCESS) {
            throw std::runtime_error(
                "could not query VA/OpenCL bridge platform");
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

        const auto properties =
            cl_context.getInfo<CL_CONTEXT_PROPERTIES>();
        for (std::size_t i = 0; i + 1 < properties.size() &&
                                properties[i] != 0;
             i += 2) {
            if (properties[i] == CL_CONTEXT_INTEROP_USER_SYNC) {
                interop_user_sync = properties[i + 1] == CL_TRUE;
                break;
            }
        }
    }

    void acquire(cl_mem image, std::uint32_t surface_id) {
        if (interop_user_sync) {
            const VAStatus va_status = vaSyncSurface(display, surface_id);
            if (va_status != VA_STATUS_SUCCESS) {
                throw std::runtime_error(std::string("vaSyncSurface: ") +
                                         vaErrorStr(va_status));
            }
        }
        const cl_int status = acquire_va(
            queue.get(), 1, &image, 0, nullptr, nullptr);
        if (status != CL_SUCCESS) {
            throw std::runtime_error(
                "clEnqueueAcquireVA_APIMediaSurfacesINTEL failed: " +
                std::to_string(status));
        }
    }

    void release(cl_mem image) {
        const cl_int status = release_va(
            queue.get(), 1, &image, 0, nullptr, nullptr);
        if (status != CL_SUCCESS) {
            throw std::runtime_error(
                "clEnqueueReleaseVA_APIMediaSurfacesINTEL failed: " +
                std::to_string(status));
        }
        queue.finish();
    }
};

VADisplay decoder_va_display(std::uintptr_t decoder_pointer) {
    GstElement* decoder = reinterpret_cast<GstElement*>(decoder_pointer);
    if (!decoder) {
        throw std::runtime_error("null GStreamer decoder");
    }
    GstContext* gst_context =
        gst_element_get_context(decoder, "gst.vaapi.Display");
    if (!gst_context) {
        throw std::runtime_error("decoder did not publish gst.vaapi.Display");
    }
    const GstStructure* structure = gst_context_get_structure(gst_context);
    const GValue* value = gst_structure_get_value(
        structure, "gst.vaapi.Display.GObject");
    GObject* display_object = value ? G_OBJECT(g_value_get_object(value)) : nullptr;
    VADisplay display = nullptr;
    if (display_object) {
        g_object_get(display_object, "va-display", &display, nullptr);
    }
    gst_context_unref(gst_context);
    if (!display) {
        throw std::runtime_error("VA display property is null");
    }
    return display;
}

std::shared_ptr<ov::Model> build_yolo_graph(ov::Core& core,
                                             const std::string& model_path,
                                             std::size_t,
                                             std::size_t) {
    using namespace ov::opset13;
    auto base = core.read_model(model_path);
    auto raw_output = base->get_results().at(0)->input_value(0);
    if (raw_output.get_shape() != ov::Shape{1, 84, 8400}) {
        throw std::runtime_error("unexpected YOLO11n output shape");
    }
    auto scores = std::make_shared<Slice>(
        raw_output,
        Constant::create(ov::element::i64, ov::Shape{1}, {4}),
        Constant::create(ov::element::i64, ov::Shape{1}, {84}),
        Constant::create(ov::element::i64, ov::Shape{1}, {1}),
        Constant::create(ov::element::i64, ov::Shape{1}, {1}));
    auto best = std::make_shared<ReduceMax>(
        scores,
        Constant::create(ov::element::i64, ov::Shape{2}, {1, 2}), false);
    auto decision = std::make_shared<Convert>(
        std::make_shared<Greater>(
            best, Constant::create(ov::element::f32, ov::Shape{}, {0.25F})),
        ov::element::u8);
    auto graph = std::make_shared<ov::Model>(
        ov::OutputVector{decision}, base->get_parameters(),
        "yolo11n_int8_full_nv12_surface_any_detection");

    ov::preprocess::PrePostProcessor ppp(graph);
    ppp.input()
        .tensor()
        .set_element_type(ov::element::u8)
        .set_spatial_static_shape(360, 640)
        .set_color_format(ov::preprocess::ColorFormat::NV12_TWO_PLANES,
                          {"nv12_y_surface", "nv12_uv_surface"})
        .set_memory_type(ov::intel_gpu::memory_type::surface);
    ppp.input()
        .preprocess()
        .convert_color(ov::preprocess::ColorFormat::RGB)
        .convert_element_type(ov::element::f32)
        .scale(255.0F)
        .convert_layout("NCHW")
        .pad({0, 0, 140, 0}, {0, 0, 140, 0}, 114.0F / 255.0F,
             ov::preprocess::PaddingMode::CONSTANT);
    ppp.input().model().set_layout("NCHW");
    return ppp.build();
}

struct YoloRunner {
    ov::Core core;
    ov::intel_gpu::ocl::VAContext context;
    ov::CompiledModel compiled;
    ov::InferRequest request;
    std::size_t height;
    std::size_t width;
    VADisplay display;
    VAConfigID vpp_config = VA_INVALID_ID;
    VAContextID vpp_context = VA_INVALID_ID;
    VASurfaceID resized_surface = VA_INVALID_SURFACE;

    static void check_va(VAStatus status, const char* operation) {
        if (status != VA_STATUS_SUCCESS) {
            throw std::runtime_error(std::string(operation) + ": " +
                                     vaErrorStr(status));
        }
    }

    YoloRunner(VADisplay display, const std::string& model_path,
               std::size_t h, std::size_t w)
        : context(core, display), height(h), width(w), display(display) {
        auto graph = build_yolo_graph(core, model_path, height, width);
        compiled = core.compile_model(
            graph, context,
            ov::AnyMap{{ov::hint::performance_mode.name(),
                        ov::hint::PerformanceMode::LATENCY}});
        request = compiled.create_infer_request();

        check_va(vaCreateConfig(display, VAProfileNone,
                                VAEntrypointVideoProc, nullptr, 0,
                                &vpp_config),
                 "vaCreateConfig(VPP)");
        VASurfaceAttrib pixel_format{};
        pixel_format.type = VASurfaceAttribPixelFormat;
        pixel_format.flags = VA_SURFACE_ATTRIB_SETTABLE;
        pixel_format.value.type = VAGenericValueTypeInteger;
        pixel_format.value.value.i = VA_FOURCC_NV12;
        check_va(vaCreateSurfaces(display, VA_RT_FORMAT_YUV420,
                                  640, 360, &resized_surface, 1,
                                  &pixel_format, 1),
                 "vaCreateSurfaces(VPP output)");
        check_va(vaCreateContext(display, vpp_config, 640, 360,
                                 VA_PROGRESSIVE, nullptr, 0, &vpp_context),
                 "vaCreateContext(VPP)");
    }

    ~YoloRunner() {
        if (vpp_context != VA_INVALID_ID) {
            vaDestroyContext(display, vpp_context);
        }
        if (resized_surface != VA_INVALID_SURFACE) {
            vaDestroySurfaces(display, &resized_surface, 1);
        }
        if (vpp_config != VA_INVALID_ID) {
            vaDestroyConfig(display, vpp_config);
        }
    }

    void resize(std::uint32_t source_surface) {
        VARectangle source_region{0, 0,
                                  static_cast<unsigned short>(width),
                                  static_cast<unsigned short>(height)};
        VARectangle output_region{0, 0, 640, 360};
        VAProcPipelineParameterBuffer parameter{};
        parameter.surface = source_surface;
        parameter.surface_region = &source_region;
        parameter.output_region = &output_region;
        parameter.filter_flags = VA_FILTER_SCALING_DEFAULT;
        VABufferID buffer = VA_INVALID_ID;
        check_va(vaCreateBuffer(display, vpp_context,
                                VAProcPipelineParameterBufferType,
                                sizeof(parameter), 1, &parameter, &buffer),
                 "vaCreateBuffer(VPP)");
        try {
            check_va(vaBeginPicture(display, vpp_context, resized_surface),
                     "vaBeginPicture(VPP)");
            check_va(vaRenderPicture(display, vpp_context, &buffer, 1),
                     "vaRenderPicture(VPP)");
            check_va(vaEndPicture(display, vpp_context), "vaEndPicture(VPP)");
            check_va(vaSyncSurface(display, resized_surface),
                     "vaSyncSurface(VPP)");
        } catch (...) {
            vaDestroyBuffer(display, buffer);
            throw;
        }
        vaDestroyBuffer(display, buffer);
    }

    bool infer(std::uint32_t surface_id) {
        resize(surface_id);
        auto planes = context.create_tensor_nv12(360, 640, resized_surface);
        request.set_input_tensor(0, planes.first);
        request.set_input_tensor(1, planes.second);
        request.infer();
        return request.get_output_tensor().data<const std::uint8_t>()[0] != 0;
    }
};

template <typename Function>
int guarded(Function&& function) {
    try {
        function();
        last_error.clear();
        return 0;
    } catch (const std::exception& error) {
        last_error = error.what();
        return -1;
    } catch (...) {
        last_error = "unknown native exception";
        return -1;
    }
}

}  // namespace

extern "C" {

void* va_y_copy_create(std::uintptr_t va_display,
                       std::size_t height,
                       std::size_t width) {
    try {
        auto bridge = std::make_unique<YCopyBridge>(
            reinterpret_cast<VADisplay>(va_display), height, width);
        last_error.clear();
        return bridge.release();
    } catch (const std::exception& error) {
        last_error = error.what();
        return nullptr;
    } catch (...) {
        last_error = "unknown native exception";
        return nullptr;
    }
}

void* va_y_copy_create_from_decoder(std::uintptr_t decoder_pointer,
                                    std::size_t height,
                                    std::size_t width) {
    try {
        return va_y_copy_create(
            reinterpret_cast<std::uintptr_t>(decoder_va_display(decoder_pointer)),
            height, width);
    } catch (const std::exception& error) {
        last_error = error.what();
        return nullptr;
    }
}

void* va_yolo_create_from_decoder(std::uintptr_t decoder_pointer,
                                  const char* model_path,
                                  std::size_t height,
                                  std::size_t width) {
    try {
        if (!model_path) throw std::runtime_error("null YOLO model path");
        auto runner = std::make_unique<YoloRunner>(
            decoder_va_display(decoder_pointer), model_path, height, width);
        last_error.clear();
        return runner.release();
    } catch (const std::exception& error) {
        last_error = error.what();
        return nullptr;
    }
}

int va_yolo_infer(void* opaque, std::uint32_t surface_id, int* decision) {
    return guarded([&] {
        if (!opaque || !decision) {
            throw std::runtime_error("null YOLO runner or decision");
        }
        *decision = static_cast<YoloRunner*>(opaque)->infer(surface_id) ? 1 : 0;
    });
}

void va_yolo_destroy(void* opaque) {
    delete static_cast<YoloRunner*>(opaque);
}

int va_y_copy_surface(void* opaque,
                      std::uint32_t surface_id,
                      std::uint8_t* destination,
                      std::size_t destination_size) {
    return guarded([&] {
        if (!opaque || !destination) {
            throw std::runtime_error("null bridge or destination");
        }
        auto& bridge = *static_cast<YCopyBridge*>(opaque);
        const std::size_t required = bridge.height * bridge.width;
        if (destination_size != required) {
            throw std::runtime_error("incorrect Y-plane destination size");
        }
        auto planes = bridge.context.create_tensor_nv12(
            bridge.height, bridge.width, surface_id);
        cl::Image2D y_image = planes.first;
        const cl::array<cl::size_type, 3> origin = {0, 0, 0};
        const cl::array<cl::size_type, 3> region = {
            bridge.width, bridge.height, 1};
        cl_mem shared_image = y_image.get();
        bridge.acquire(shared_image, surface_id);
        try {
            bridge.queue.enqueueReadImage(
                y_image, CL_TRUE, origin, region, bridge.width, 0, destination);
        } catch (...) {
            try { bridge.release(shared_image); } catch (...) {}
            throw;
        }
    });
}

int va_y_copy_surface_profiled(void* opaque,
                               std::uint32_t surface_id,
                               std::uint8_t* destination,
                               std::size_t destination_size,
                               double* stage_ms,
                               int stage_count,
                               int* status_before) {
    return guarded([&] {
        if (!opaque || !destination || !stage_ms || stage_count < 4 ||
            !status_before) {
            throw std::runtime_error("invalid profiled Y-copy argument");
        }
        auto& bridge = *static_cast<YCopyBridge*>(opaque);
        const std::size_t required = bridge.height * bridge.width;
        if (destination_size != required) {
            throw std::runtime_error("incorrect profiled Y destination size");
        }
        using ProfileClock = std::chrono::steady_clock;
        const auto total_start = ProfileClock::now();
        VASurfaceStatus surface_status = VASurfaceRendering;
        VAStatus va_status = vaQuerySurfaceStatus(
            bridge.display, surface_id, &surface_status);
        if (va_status != VA_STATUS_SUCCESS) {
            throw std::runtime_error(std::string("vaQuerySurfaceStatus: ") +
                                     vaErrorStr(va_status));
        }
        *status_before = static_cast<int>(surface_status);

        auto start = ProfileClock::now();
        va_status = vaSyncSurface(bridge.display, surface_id);
        if (va_status != VA_STATUS_SUCCESS) {
            throw std::runtime_error(std::string("vaSyncSurface: ") +
                                     vaErrorStr(va_status));
        }
        stage_ms[0] = std::chrono::duration<double, std::milli>(
                          ProfileClock::now() - start)
                          .count();

        start = ProfileClock::now();
        auto planes = bridge.context.create_tensor_nv12(
            bridge.height, bridge.width, surface_id);
        cl::Image2D y_image = planes.first;
        stage_ms[1] = std::chrono::duration<double, std::milli>(
                          ProfileClock::now() - start)
                          .count();

        const cl::array<cl::size_type, 3> origin = {0, 0, 0};
        const cl::array<cl::size_type, 3> region = {
            bridge.width, bridge.height, 1};
        start = ProfileClock::now();
        cl_mem shared_image = y_image.get();
        bridge.acquire(shared_image, surface_id);
        try {
            bridge.queue.enqueueReadImage(
                y_image, CL_TRUE, origin, region, bridge.width, 0, destination);
        } catch (...) {
            try { bridge.release(shared_image); } catch (...) {}
            throw;
        }
        bridge.release(shared_image);
        stage_ms[2] = std::chrono::duration<double, std::milli>(
                          ProfileClock::now() - start)
                          .count();
        stage_ms[3] = std::chrono::duration<double, std::milli>(
                          ProfileClock::now() - total_start)
                          .count();
    });
}

int va_y_copy_sync_surface_profiled(void* opaque,
                                    std::uint32_t surface_id,
                                    int* status_before,
                                    double* sync_ms) {
    return guarded([&] {
        if (!opaque || !status_before || !sync_ms) {
            throw std::runtime_error("invalid VA sync profiling argument");
        }
        auto& bridge = *static_cast<YCopyBridge*>(opaque);
        VASurfaceStatus surface_status = VASurfaceRendering;
        VAStatus status = vaQuerySurfaceStatus(
            bridge.display, surface_id, &surface_status);
        if (status != VA_STATUS_SUCCESS) {
            throw std::runtime_error(std::string("vaQuerySurfaceStatus: ") +
                                     vaErrorStr(status));
        }
        *status_before = static_cast<int>(surface_status);
        const auto start = std::chrono::steady_clock::now();
        status = vaSyncSurface(bridge.display, surface_id);
        *sync_ms = std::chrono::duration<double, std::milli>(
                       std::chrono::steady_clock::now() - start)
                       .count();
        if (status != VA_STATUS_SUCCESS) {
            throw std::runtime_error(std::string("vaSyncSurface: ") +
                                     vaErrorStr(status));
        }
    });
}

int va_y_map_surface(void* opaque,
                     std::uint32_t surface_id,
                     std::uint8_t* destination,
                     std::size_t destination_size) {
    return guarded([&] {
        if (!opaque || !destination) {
            throw std::runtime_error("null bridge or VA-map destination");
        }
        auto& bridge = *static_cast<YCopyBridge*>(opaque);
        const std::size_t required = bridge.height * bridge.width;
        if (destination_size != required) {
            throw std::runtime_error("incorrect VA-map Y destination size");
        }
        VAStatus status = vaSyncSurface(bridge.display, surface_id);
        if (status != VA_STATUS_SUCCESS) {
            throw std::runtime_error(std::string("vaSyncSurface: ") +
                                     vaErrorStr(status));
        }
        VAImage image{};
        status = vaDeriveImage(bridge.display, surface_id, &image);
        if (status != VA_STATUS_SUCCESS) {
            throw std::runtime_error(std::string("vaDeriveImage: ") +
                                     vaErrorStr(status));
        }
        void* mapped = nullptr;
        status = vaMapBuffer(bridge.display, image.buf, &mapped);
        if (status != VA_STATUS_SUCCESS) {
            vaDestroyImage(bridge.display, image.image_id);
            throw std::runtime_error(std::string("vaMapBuffer: ") +
                                     vaErrorStr(status));
        }
        const auto* y = static_cast<const std::uint8_t*>(mapped) +
                        image.offsets[0];
        for (std::size_t row = 0; row < bridge.height; ++row) {
            std::memcpy(destination + row * bridge.width,
                        y + row * image.pitches[0], bridge.width);
        }
        vaUnmapBuffer(bridge.display, image.buf);
        vaDestroyImage(bridge.display, image.image_id);
    });
}

int va_sample_surface_id(std::uintptr_t sample_pointer,
                         std::uint32_t* surface_id) {
    return guarded([&] {
        if (!sample_pointer || !surface_id) {
            throw std::runtime_error("null GstSample or surface output");
        }
        GstSample* sample = reinterpret_cast<GstSample*>(sample_pointer);
        GstBuffer* buffer = gst_sample_get_buffer(sample);
        if (!buffer) throw std::runtime_error("GstSample has no buffer");
        GstMapInfo map = GST_MAP_INFO_INIT;
        const GstMapFlags va_flag =
            static_cast<GstMapFlags>(GST_MAP_FLAG_LAST << 1);
        if (!gst_buffer_map(buffer, &map, va_flag)) {
            throw std::runtime_error("could not map VASurfaceID");
        }
        if (map.size < sizeof(std::uint32_t)) {
            gst_buffer_unmap(buffer, &map);
            throw std::runtime_error("VASurfaceID map was too small");
        }
        std::memcpy(surface_id, map.data, sizeof(*surface_id));
        gst_buffer_unmap(buffer, &map);
    });
}

std::uintptr_t va_y_copy_cl_context(void* opaque) {
    if (!opaque) {
        last_error = "null bridge";
        return 0;
    }
    auto& bridge = *static_cast<YCopyBridge*>(opaque);
    return reinterpret_cast<std::uintptr_t>(bridge.context.get());
}

std::uintptr_t va_y_copy_acquire_image(void* opaque,
                                       std::uint32_t surface_id) {
    if (!opaque) {
        last_error = "null bridge";
        return 0;
    }
    try {
        auto& bridge = *static_cast<YCopyBridge*>(opaque);
        auto planes = bridge.context.create_tensor_nv12(
            bridge.height, bridge.width, surface_id);
        cl_mem image = planes.first.get();
        const cl_int status = clRetainMemObject(image);
        if (status != CL_SUCCESS) {
            throw std::runtime_error("clRetainMemObject failed");
        }
        last_error.clear();
        return reinterpret_cast<std::uintptr_t>(image);
    } catch (const std::exception& error) {
        last_error = error.what();
        return 0;
    }
}

void va_y_copy_release_image(std::uintptr_t image_pointer) {
    if (image_pointer) {
        clReleaseMemObject(reinterpret_cast<cl_mem>(image_pointer));
    }
}

void va_y_copy_destroy(void* opaque) {
    delete static_cast<YCopyBridge*>(opaque);
}

const char* va_y_copy_last_error() {
    return last_error.c_str();
}

}  // extern "C"
