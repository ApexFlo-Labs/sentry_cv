// Generic VA-surface raw-output detector: VPP scales source NV12 to fate
// size on-chip, PPP pads to the square letterbox (same geometry as
// OpenVINOYOLODetector.letterbox), model runs on the surface, raw f32 output
// crosses to host (boxes only). No full-frame download.
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

struct VaResources {
    VADisplay display;
    VAConfigID config = VA_INVALID_ID;
    VAContextID context = VA_INVALID_ID;
    VASurfaceID surface = VA_INVALID_SURFACE;

    ~VaResources() {
        if (context != VA_INVALID_ID) vaDestroyContext(display, context);
        if (surface != VA_INVALID_SURFACE) vaDestroySurfaces(display, &surface, 1);
        if (config != VA_INVALID_ID) vaDestroyConfig(display, config);
    }
};

struct SurfaceRaw {
    // Declared first, destroyed last: imported OpenCL/OV tensors must be
    // released before their backing VA surface and VPP context are destroyed.
    VaResources va;
    ov::Core core;
    ov::intel_gpu::ocl::VAContext context;
    ov::CompiledModel compiled;
    ov::InferRequest request;
    VADisplay& display = va.display;
    VAConfigID& vpp_config = va.config;
    VAContextID& vpp_context = va.context;
    VASurfaceID& resized_surface = va.surface;
    int fate_w, fate_h, pad_tb;
    std::size_t out_elems;

    static void check_va(VAStatus status, const char* operation) {
        if (status != VA_STATUS_SUCCESS) {
            throw std::runtime_error(std::string(operation) + ": " +
                                     vaErrorStr(status));
        }
    }

    SurfaceRaw(VADisplay display, const std::string& model_path, int fw,
               int fh, int pad, std::size_t out_e)
        : va{display}, context(core, display), fate_w(fw), fate_h(fh),
          pad_tb(pad), out_elems(out_e) {
        auto model = core.read_model(model_path);
        ov::preprocess::PrePostProcessor ppp(model);
        ppp.input()
            .tensor()
            .set_element_type(ov::element::u8)
            .set_spatial_static_shape(fate_h, fate_w)
            .set_color_format(ov::preprocess::ColorFormat::NV12_TWO_PLANES,
                              {"nv12_y_surface", "nv12_uv_surface"})
            .set_memory_type(ov::intel_gpu::memory_type::surface);
        ppp.input()
            .preprocess()
            .convert_color(ov::preprocess::ColorFormat::RGB)
            .convert_element_type(ov::element::f32)
            .scale(255.0F)
            .convert_layout("NCHW")
            .pad({0, 0, pad_tb, 0}, {0, 0, pad_tb, 0}, 114.0F / 255.0F,
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
        check_va(vaCreateSurfaces(display, VA_RT_FORMAT_YUV420, fate_w, fate_h,
                                  &resized_surface, 1, &pixel_format, 1),
                 "vaCreateSurfaces(VPP output)");
        check_va(vaCreateContext(display, vpp_config, fate_w, fate_h,
                                 VA_PROGRESSIVE, nullptr, 0, &vpp_context),
                 "vaCreateContext(VPP)");
    }

    ~SurfaceRaw() = default;

    std::size_t infer(std::uint32_t source_surface, float* out,
                      std::size_t cap_floats) {
        VARectangle source_region{0, 0, 1920, 1080};
        VARectangle output_region{0, 0, fate_w, fate_h};
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
        auto planes = context.create_tensor_nv12(fate_h, fate_w,
                                                 resized_surface);
        request.set_input_tensor(0, planes.first);
        request.set_input_tensor(1, planes.second);
        request.infer();
        auto tensor = request.get_output_tensor();
        const float* data = tensor.data<const float>();
        const std::size_t n = std::min<std::size_t>(out_elems, cap_floats);
        std::memcpy(out, data, n * sizeof(float));
        return n;
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

void* va_surface_raw_create(std::uintptr_t decoder_pointer,
                            const char* model_path, int fate_w, int fate_h,
                            int pad_tb, unsigned long out_elems) {
    try {
        if (!decoder_pointer || !model_path) {
            throw std::runtime_error("null surface-raw bridge argument");
        }
        last_error.clear();
        return new SurfaceRaw(decoder_va_display(decoder_pointer), model_path,
                              fate_w, fate_h, pad_tb, out_elems);
    } catch (const std::exception& error) {
        last_error = error.what();
        return nullptr;
    }
}

int va_surface_raw_infer(void* opaque, std::uint32_t surface_id, float* out,
                         unsigned long cap_floats, unsigned long* got) {
    return guarded([&] {
        if (!opaque || !out || !got) {
            throw std::runtime_error("null surface-raw infer argument");
        }
        *got = static_cast<SurfaceRaw*>(opaque)->infer(surface_id, out,
                                                       cap_floats);
    });
}

const char* va_surface_raw_last_error() {
    return last_error.c_str();
}

void va_surface_raw_destroy(void* opaque) {
    delete static_cast<SurfaceRaw*>(opaque);
}

}  // extern "C"
