// Diagnostic only. Observe a bounded number of actual fire-model input tensors
// at OpenVINO's public start_async boundary. Never modify tensors or callbacks.
// LD_PRELOAD affects timing: this cannot be used as capacity evidence.
#include <openvino/openvino.hpp>
#include <dlfcn.h>
#include <atomic>
#include <chrono>
#include <cstdlib>
#include <filesystem>
#include <fstream>
#include <mutex>
#include <stdexcept>
#include <thread>
#include <unistd.h>

namespace {
std::mutex capture_mutex;
std::atomic<unsigned> next_capture{0};
std::atomic<unsigned> next_model{0};
std::atomic<unsigned> next_compiled{0};
std::atomic<unsigned> next_completed{0};
thread_local bool observing = false;
std::recursive_mutex gpu_compile_mutex;

bool trace_only() {
    const char* value=std::getenv("SENTINEL_TEST_COMPILE_TRACE_ONLY");
    return value && std::string(value)=="1";
}

struct CompileTrace {
    std::unique_lock<std::recursive_mutex> lock;
    const void* model;
    std::string device;
    std::chrono::steady_clock::time_point began;
    CompileTrace(const void* m, std::string d): model(m),device(std::move(d)),
        began(std::chrono::steady_clock::now()) {
        record("requested");
        const char* serialized=std::getenv("SENTINEL_TEST_SERIALIZE_GPU_COMPILE");
        if (serialized && std::string(serialized)=="1" && device.find("GPU")!=std::string::npos)
            lock=std::unique_lock<std::recursive_mutex>(gpu_compile_mutex);
        record("entered");
    }
    void record(const char* stage) noexcept {
        const char* path=std::getenv("SENTINEL_NATIVE_COMPILE_TRACE");
        if (!path || !*path) return;
        try {
            const auto now=std::chrono::steady_clock::now();
            std::lock_guard<std::mutex> guard(capture_mutex);
            std::ofstream out(path,std::ios::app);
            out << "{\"stage\":\"" << stage << "\",\"device\":\"" << device
                << "\",\"model\":\"" << model << "\",\"thread\":\""
                << std::this_thread::get_id() << "\",\"monotonic_ns\":"
                << std::chrono::duration_cast<std::chrono::nanoseconds>(now.time_since_epoch()).count()
                << ",\"duration_ns\":" << std::chrono::duration_cast<std::chrono::nanoseconds>(now-began).count()
                << "}\n";
        } catch (...) {}
    }
    ~CompileTrace() { record("finished"); }
};

void capture_model(const std::shared_ptr<const ov::Model>& model,
                   const std::string& device, const ov::AnyMap& config) noexcept {
    const char* directory = std::getenv("SENTINEL_FIRE_INPUT_CAPTURE");
    if (!directory || !*directory || trace_only()) return;
    try {
        if (model->outputs().size() != 1 || model->output().get_shape() != ov::Shape{1, 6, 2100}) return;
        const auto index = next_model.fetch_add(1);
        if (index >= 4) return;
        std::lock_guard<std::mutex> lock(capture_mutex);
        const auto root = std::filesystem::path(directory);
        std::filesystem::create_directories(root);
        const auto stem = std::to_string(getpid()) + "-model-" + std::to_string(index);
        // serialize preserves precision/weights, unlike save_model's optional
        // compression. This is the exact pre-compilation graph, including PPP.
        ov::serialize(model, (root/(stem+".xml")).string(), (root/(stem+".bin")).string());
        std::ofstream metadata(root/(stem+".txt"));
        metadata << "device=" << device << '\n';
        for (const auto& entry : config) {
            metadata << entry.first << '=';
            entry.second.print(metadata);
            metadata << '\n';
        }
    } catch (...) { /* A failed observation never replaces compilation. */ }
}

void capture_compiled(const std::shared_ptr<const ov::Model>& model,
                      const ov::CompiledModel& compiled) noexcept {
    const char* directory = std::getenv("SENTINEL_FIRE_INPUT_CAPTURE");
    if (!directory || !*directory || trace_only()) return;
    try {
        if (model->outputs().size()!=1 || model->output().get_shape()!=ov::Shape{1,6,2100}) return;
        const auto index=next_compiled.fetch_add(1);
        if (index>=4) return;
        std::lock_guard<std::mutex> lock(capture_mutex);
        const auto root=std::filesystem::path(directory);
        const auto stem=std::to_string(getpid())+"-compiled-"+std::to_string(index);
        std::ofstream properties(root/(stem+".txt"));
        for (const auto* key : {"EXECUTION_DEVICES", "INFERENCE_PRECISION_HINT", "LP_TRANSFORMS_MODE",
                "NUM_STREAMS", "PERFORMANCE_HINT", "OPTIMAL_NUMBER_OF_INFER_REQUESTS"}) {
            properties << key << '=';
            try { compiled.get_property(key).print(properties); }
            catch (...) { properties << "unavailable"; }
            properties << '\n';
        }
        ov::serialize(compiled.get_runtime_model(), (root/(stem+".xml")).string(),
                       (root/(stem+".bin")).string());
    } catch (...) { /* Diagnostic failures cannot replace execution. */ }
}
}

ov::CompiledModel ov::Core::compile_model(const std::shared_ptr<const ov::Model>& model,
                                         const std::string& device,
                                         const ov::AnyMap& config) {
    using Function = ov::CompiledModel (*)(ov::Core*, const std::shared_ptr<const ov::Model>&,
                                           const std::string&, const ov::AnyMap&);
    static auto original = reinterpret_cast<Function>(dlsym(RTLD_NEXT,
        "_ZN2ov4Core13compile_modelERKSt10shared_ptrIKNS_5ModelEERKNSt7__cxx1112basic_stringIcSt11char_traitsIcESaIcEEERKSt3mapISC_NS_3AnyESt4lessISC_ESaISt4pairISD_SG_EEE"));
    if (!original) std::abort();
    CompileTrace trace(model.get(),device);
    capture_model(model, device, config);
    auto compiled=original(this, model, device, config);
    capture_compiled(model, compiled);
    return compiled;
}

ov::CompiledModel ov::Core::compile_model(const std::shared_ptr<const ov::Model>& model,
                                         const ov::RemoteContext& context,
                                         const ov::AnyMap& config) {
    using Function = ov::CompiledModel (*)(ov::Core*, const std::shared_ptr<const ov::Model>&,
                                           const ov::RemoteContext&, const ov::AnyMap&);
    static auto original = reinterpret_cast<Function>(dlsym(RTLD_NEXT,
        "_ZN2ov4Core13compile_modelERKSt10shared_ptrIKNS_5ModelEERKNS_13RemoteContextERKSt3mapINSt7__cxx1112basic_stringIcSt11char_traitsIcESaIcEEENS_3AnyESt4lessISG_ESaISt4pairIKSG_SH_EEE"));
    if (!original) std::abort();
    CompileTrace trace(model.get(),context.get_device_name());
    capture_model(model, context.get_device_name(), config);
    auto compiled=original(this, model, context, config);
    capture_compiled(model, compiled);
    return compiled;
}

ov::Tensor ov::InferRequest::get_output_tensor(const size_t index) {
    using Function=ov::Tensor (*)(ov::InferRequest*,size_t);
    static auto original=reinterpret_cast<Function>(dlsym(RTLD_NEXT,
        "_ZN2ov12InferRequest17get_output_tensorEm"));
    if (!original) std::abort();
    auto output=original(this,index);
    const char* directory=std::getenv("SENTINEL_FIRE_INPUT_CAPTURE");
    if (directory && *directory && !trace_only() && !observing && next_completed.load()<32) {
        observing=true;
        try {
            if (output.get_shape()==ov::Shape{1,6,2100} && output.get_element_type()==ov::element::f32) {
                auto input=get_input_tensor();
                const auto sequence=next_completed.fetch_add(1);
                if (sequence<32 && input.get_byte_size()<=8*1024*1024) {
                    std::lock_guard<std::mutex> lock(capture_mutex);
                    const auto root=std::filesystem::path(directory);
                    std::filesystem::create_directories(root);
                    const auto stem=std::to_string(getpid())+"-completed-"+std::to_string(sequence);
                    std::ofstream data(root/(stem+".output"),std::ios::binary);
                    data.write(static_cast<const char*>(output.data()),output.get_byte_size());data.close();
                    std::ofstream before(root/(stem+".input"),std::ios::binary);
                    before.write(static_cast<const char*>(input.data()),input.get_byte_size());before.close();
                    std::ofstream metadata(root/(stem+".json"));
                    metadata << "{\"request\":\"" << this << "\",\"input_bytes\":"
                        << input.get_byte_size() << ",\"output_bytes\":" << output.get_byte_size()
                        << ",\"scope\":\"Observed getter before GVA postprocessing; not capacity evidence\"}\n";
                }
            }
        } catch (...) { /* Preserve the application's original getter result. */ }
        observing=false;
    }
    return output;
}

extern "C" void capture_start_async(void* pointer)
    asm("_ZN2ov12InferRequest11start_asyncEv");

extern "C" void capture_start_async(void* pointer) {
    using Function = void (*)(void*);
    static auto original = reinterpret_cast<Function>(
        dlsym(RTLD_NEXT, "_ZN2ov12InferRequest11start_asyncEv"));
    if (!original) std::abort();
    const char* directory = std::getenv("SENTINEL_FIRE_INPUT_CAPTURE");
    if (directory && *directory && !trace_only() && !observing && next_capture.load() < 32) {
        observing = true;
        try {
            auto& request = *static_cast<ov::InferRequest*>(pointer);
            // Identify the fire model by its full output shape, never by a
            // guessed request address or a shared 320-pixel input size.
            auto output = request.get_output_tensor();
            if (output.get_shape() == ov::Shape{1, 6, 2100}) {
                auto input = request.get_input_tensor();
                const auto bytes = input.get_byte_size();
                if (!bytes || bytes > 8 * 1024 * 1024)
                    throw std::runtime_error("Input size outside bounded diagnostic");
                const auto* data = static_cast<const char*>(input.data());
                const auto index = next_capture.fetch_add(1);
                if (index < 32) {
                    std::lock_guard<std::mutex> lock(capture_mutex);
                    auto root = std::filesystem::path(directory);
                    std::filesystem::create_directories(root);
                    const auto stem = std::to_string(getpid()) + "-" + std::to_string(index);
                    std::ofstream binary(root / (stem + ".tensor"), std::ios::binary);
                    binary.write(data, bytes);
                    binary.close();
                    std::ofstream metadata(root / (stem + ".json"));
                    metadata << "{\"pid\":" << getpid() << ",\"index\":" << index
                             << ",\"bytes\":" << bytes << ",\"element_type\":\""
                             << input.get_element_type().get_type_name() << "\",\"shape\":[";
                    const auto shape = input.get_shape();
                    for (size_t i = 0; i < shape.size(); ++i) {
                        if (i) metadata << ',';
                        metadata << shape[i];
                    }
                    metadata << "],\"strides\":[";
                    const auto strides = input.get_strides();
                    for (size_t i = 0; i < strides.size(); ++i) {
                        if (i) metadata << ',';
                        metadata << strides[i];
                    }
                    metadata << "],\"request\":\"" << pointer
                             << "\",\"scope\":\"Read-only diagnostic; not capacity evidence\"}\n";
                }
            }
        } catch (const std::exception& error) {
            // Observation failure must never replace the model operation.
            std::lock_guard<std::mutex> lock(capture_mutex);
            try {
                std::filesystem::create_directories(directory);
                std::ofstream log(std::filesystem::path(directory) / "errors.log", std::ios::app);
                log << error.what() << '\n';
            } catch (...) {}
        }
        observing = false;
    }
    original(pointer);
}
