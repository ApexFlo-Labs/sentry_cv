// VA-surface vehicle detector: OpenVINO reads the VA surface directly
// (remote tensor), VPP scales 1920x1080 -> 640x360 on-chip, PPP pads to
// 640x640 letterbox (140px top/bottom, gray 114) - the exact geometry of
// OpenVINOYOLODetector.letterbox, so boxes compare 1:1 with the BGR path.
// Output rows are the baked NMS [x1,y1,x2,y2,score,class] in letterbox px.
// Only ~29KB (300x6 f32) crosses to host per infer. No full-frame download.
#include <CL/cl.h>
#include <CL/cl_ext.h>
#include <CL/cl_va_api_media_sharing_intel.h>
#include <gst/gst.h>
#include <va/va.h>

#include <openvino/openvino.hpp>
#include <openvino/runtime/intel_gpu/ocl/va.hpp>

#include <cstddef>
#include <cstdint>
#include <cstring>
#include <exception>
#include <memory>
#include <stdexcept>
#include <string>

namespace {

thread_local std::string last_error;

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

struct SurfaceVehicle {
    static constexpr int kFateW = 640;
    static constexpr int kFateH = 360;
    static constexpr int kOut = 640;
    static constexpr int kMax = 300;

    ov::Core core;
    ov::intel_gpu::ocl::VAContext context;
    ov::CompiledModel compiled;
    ov::InferRequest request;
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

    SurfaceVehicle(VADisplay display, const std::string& model_path)
        : context(core, display), display(display) {
        auto model = core.read_model(model_path);
        ov::preprocess::PrePostProcessor ppp(model);
        ppp.input()
            .tensor()
            .set_element_type(ov::element::u8)
            .set_spatial_static_shape(kFateH, kFateW)
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
        auto graph = ppp.build();
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
                                  kFateW, kFateH, &resized_surface, 1,
                                  &pixel_format, 1),
                 "vaCreateSurfaces(VPP output)");
        check_va(vaCreateContext(display, vpp_config, kFateW, kFateH,
                                 VA_PROGRESSIVE, nullptr, 0, &vpp_context),
                 "vaCreateContext(VPP)");
    }

    ~SurfaceVehicle() {
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

    // Returns kept-row count; rows are [x1,y1,x2,y2,score,class] f32.
    int infer(std::uint32_t source_surface, float* rows, int capacity) {
        VARectangle source_region{0, 0, 1920, 1080};
        VARectangle output_region{0, 0, kFateW, kFateH};
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
            check_va(vaEndPicture(display, vpp_context),
                     "vaEndPicture(VPP)");
            check_va(vaSyncSurface(display, resized_surface),
                     "vaSyncSurface(VPP)");
        } catch (...) {
            vaDestroyBuffer(display, buffer);
            throw;
        }
        vaDestroyBuffer(display, buffer);
        auto planes = context.create_tensor_nv12(kFateH, kFateW,
                                                 resized_surface);
        request.set_input_tensor(0, planes.first);
        request.set_input_tensor(1, planes.second);
        request.infer();
        auto out = request.get_output_tensor();
        const float* data = out.data<const float>();
        // Rows are score-sorted; stop at first below-threshold or empty row.
        int kept = 0;
        for (int i = 0; i < kMax && kept < capacity; ++i) {
            const float* r = data + i * 6;
            const float score = r[4];
            if (!(score > 0.0F)) break;
            std::memcpy(rows + kept * 6, r, 6 * sizeof(float));
            ++kept;
            if (score < 0.05F) break;
        }
        return kept;
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

void* va_surface_vehicle_create(std::uintptr_t decoder_pointer,
                                const char* model_path) {
    try {
        if (!decoder_pointer || !model_path) {
            throw std::runtime_error("null vehicle bridge argument");
        }
        last_error.clear();
        return new SurfaceVehicle(decoder_va_display(decoder_pointer),
                                  model_path);
    } catch (const std::exception& error) {
        last_error = error.what();
        return nullptr;
    }
}

int va_surface_vehicle_infer(void* opaque, std::uint32_t surface_id,
                             float* rows, int capacity, int* kept) {
    return guarded([&] {
        if (!opaque || !rows || capacity <= 0 || !kept) {
            throw std::runtime_error("null vehicle infer argument");
        }
        *kept = static_cast<SurfaceVehicle*>(opaque)->infer(surface_id, rows,
                                                            capacity);
    });
}

const char* va_surface_vehicle_last_error() {
    return last_error.c_str();
}

void va_surface_vehicle_destroy(void* opaque) {
    delete static_cast<SurfaceVehicle*>(opaque);
}

}  // extern "C"
