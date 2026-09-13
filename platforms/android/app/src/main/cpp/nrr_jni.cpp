// nrr_jni.cpp — JNI bindings for the native NRR runtime on Android.
//
// Data flow:  model.onnx -> NRR device+model -> mobile execution kernel
//             (ONNX Runtime, CPU EP) -> RGB8 bytes
//             -> NRRBridge.kt -> AndroidBackend/LocalApiServer -> ShugoCore.
//
// Design notes (mirrors llama_jni.cpp):
//  * nativeCreate() returns a pointer to a ShugoNrrSession; every other call
//    takes that handle. No mutable global state beyond a registry lock.
//  * No pixel bytes ever leave the device: the caller uploads a local frame
//    and gets the rendered RGB8 back in-process.
//  * The device is created with an EXPLICIT preferred_backend. Upstream's
//    vendor backends report is_supported() == true without probing for their
//    GPU, and Adreno (priority 60) outranks CPU (10), so upstream's
//    auto-selection resolves to Adreno on every SoC and then fails GPU
//    detection on non-Qualcomm silicon (see docs/nrr_android_port.md).
//  * __ANDROID__ guards keep this file host-compilable for CI header checks.

#include <jni.h>

#include <cstdint>
#include <cstring>
#include <mutex>
#include <string>
#include <vector>

#ifdef __ANDROID__
#include <android/log.h>
#define LOGI(...) __android_log_print(ANDROID_LOG_INFO,  TAG, __VA_ARGS__)
#define LOGW(...) __android_log_print(ANDROID_LOG_WARN,  TAG, __VA_ARGS__)
#define LOGE(...) __android_log_print(ANDROID_LOG_ERROR, TAG, __VA_ARGS__)
#else
#define LOGI(...) do { fprintf(stdout, "[nrr_jni] " __VA_ARGS__); fprintf(stdout, "\n"); } while (0)
#define LOGW(...) do { fprintf(stderr, "[nrr_jni] W " __VA_ARGS__); fprintf(stderr, "\n"); } while (0)
#define LOGE(...) do { fprintf(stderr, "[nrr_jni] E " __VA_ARGS__); fprintf(stderr, "\n"); } while (0)
#endif

#include "nrr.h"
#include "nrr_device.h"
#include "nrr_model.h"
#include "nrr_runtime.h"
#include "mobile/mobile_kernel.h"
#include "mobile/nrr_power_manager.h"

#define TAG "nrr_jni"

namespace {

// Single active session per process: the mobile execution kernel is itself a
// process-wide singleton owning one ONNX Runtime session, so allowing two
// devices would silently share one session.
std::mutex g_session_mutex;
int g_live_sessions = 0;

struct ShugoNrrSession {
    NRRDevice* device = nullptr;
    NRRModel*  model  = nullptr;
    NRRTexture* input = nullptr;   // cached RGBA8 input texture
    uint32_t   input_w = 0;
    uint32_t   input_h = 0;
    std::string backend_name;
    std::string model_path;
    std::mutex  render_mutex;      // serializes frames on this session
};

ShugoNrrSession* as_session(jlong ptr) {
    return reinterpret_cast<ShugoNrrSession*>(static_cast<intptr_t>(ptr));
}

std::string jstr(JNIEnv* env, jstring s) {
    if (!s) return std::string();
    const char* c = env->GetStringUTFChars(s, nullptr);
    std::string out = c ? c : "";
    if (c) env->ReleaseStringUTFChars(s, c);
    return out;
}

jstring to_jstr(JNIEnv* env, const std::string& s) {
    return env->NewStringUTF(s.c_str());
}

std::string json_escape(const std::string& in) {
    std::string out;
    out.reserve(in.size());
    for (char c : in) {
        if (c == '"' || c == '\\') { out.push_back('\\'); out.push_back(c); }
        else if (static_cast<unsigned char>(c) >= 0x20) out.push_back(c);
    }
    return out;
}

const char* cap_state(NRRCapabilityState s) {
    switch (s) {
        case NRR_CAPABILITY_ABSENT:       return "absent";
        case NRR_CAPABILITY_BASIC:        return "basic";
        case NRR_CAPABILITY_OPTIMIZED:    return "optimized";
        case NRR_CAPABILITY_FULL:         return "full";
        case NRR_CAPABILITY_EXPERIMENTAL: return "experimental";
        default:                          return "unknown";
    }
}

}  // namespace
extern "C" JNIEXPORT jint JNICALL JNI_OnLoad(JavaVM*, void*) {
    LOGI("JNI_OnLoad");
    return JNI_VERSION_1_6;
}

// create(preferredBackend, modelPath) -> session handle, or 0.
extern "C" JNIEXPORT jlong JNICALL
Java_com_samurai_shugocore_inference_NRRBridge_nativeCreate(
    JNIEnv* env, jclass, jstring jbackend, jstring jmodel) {
    std::lock_guard<std::mutex> lock(g_session_mutex);
    if (g_live_sessions > 0) {
        LOGE("nativeCreate: a session already exists (kernel is a singleton)");
        return 0;
    }

    std::string backend = jstr(env, jbackend);
    if (backend.empty()) backend = "CPU";
    const std::string model_path = jstr(env, jmodel);

    auto* s = new ShugoNrrSession();
    s->model_path = model_path;

    NRRDeviceOptions opts = {};
    opts.preferred_backend = backend.c_str();  // never rely on auto-selection
    opts.frames_in_flight = 1;
    if (nrr_device_create(&opts, &s->device) != NRR_SUCCESS || !s->device) {
        LOGE("nativeCreate: nrr_device_create failed for '%s'", backend.c_str());
        delete s;
        return 0;
    }

    char name[64] = {};
    nrr_get_backend_name(s->device, name, sizeof(name));
    s->backend_name = name;

    if (!model_path.empty()) {
        if (nrr_model_load(s->device, model_path.c_str(), &s->model)
                != NRR_SUCCESS || !s->model) {
            LOGW("nativeCreate: model '%s' failed to load; render unavailable",
                 model_path.c_str());
            s->model = nullptr;
        }
    }

    nrr::MobileExecutionKernel* kernel = nrr::get_mobile_kernel();
    if (kernel && !kernel->is_initialized()) {
        if (!kernel->initialize(nrr::MobileEP::CPU, 256u * 1024u * 1024u,
                                true, false, true)) {
            LOGW("nativeCreate: mobile kernel init failed");
        }
    }

    ++g_live_sessions;
    LOGI("nativeCreate: backend=%s model=%s", s->backend_name.c_str(),
         s->model ? "loaded" : "none");
    return static_cast<jlong>(reinterpret_cast<intptr_t>(s));
}

extern "C" JNIEXPORT void JNICALL
Java_com_samurai_shugocore_inference_NRRBridge_nativeDestroy(
    JNIEnv*, jclass, jlong handle) {
    ShugoNrrSession* s = as_session(handle);
    if (!s) return;
    {
        std::lock_guard<std::mutex> lock(s->render_mutex);
        if (s->input && s->device) nrr_texture_destroy(s->device, s->input);
        s->input = nullptr;
        if (s->model) nrr_model_unload(s->model);
        s->model = nullptr;
        if (s->device) nrr_device_destroy(s->device);
        s->device = nullptr;
    }
    nrr::destroy_mobile_kernel();
    delete s;
    std::lock_guard<std::mutex> lock(g_session_mutex);
    if (g_live_sessions > 0) --g_live_sessions;
}

// renderFrame(w, h, rgba) -> RGB8 bytes, or null on failure.
extern "C" JNIEXPORT jbyteArray JNICALL
Java_com_samurai_shugocore_inference_NRRBridge_nativeRenderFrame(
    JNIEnv* env, jclass, jlong handle, jint width, jint height,
    jbyteArray jrgba) {
    ShugoNrrSession* s = as_session(handle);
    if (!s || !s->device || !s->model || !jrgba) return nullptr;
    if (width <= 0 || height <= 0) return nullptr;

    const jsize rgba_len = env->GetArrayLength(jrgba);
    const size_t expect = static_cast<size_t>(width) * height * 4;
    if (static_cast<size_t>(rgba_len) != expect) {
        LOGE("renderFrame: rgba is %d bytes, expected %zu", rgba_len, expect);
        return nullptr;
    }

    std::lock_guard<std::mutex> lock(s->render_mutex);

    // (Re)create the cached input texture when the frame geometry changes.
    if (!s->input || s->input_w != static_cast<uint32_t>(width) ||
        s->input_h != static_cast<uint32_t>(height)) {
        if (s->input) nrr_texture_destroy(s->device, s->input);
        NRRTextureDesc td = {};
        td.width = static_cast<uint32_t>(width);
        td.height = static_cast<uint32_t>(height);
        td.format = NRR_TEXTURE_FORMAT_RGBA8;
        td.usage = NRR_TEXTURE_USAGE_COLOR;
        if (nrr_texture_create(s->device, &td, &s->input) != NRR_SUCCESS) {
            s->input = nullptr;
            LOGE("renderFrame: input texture create failed");
            return nullptr;
        }
        s->input_w = td.width;
        s->input_h = td.height;
    }

    std::vector<uint8_t> rgba(expect);
    env->GetByteArrayRegion(jrgba, 0, rgba_len,
                            reinterpret_cast<jbyte*>(rgba.data()));
    if (nrr_texture_upload(s->device, s->input, rgba.data(), rgba.size())
            != NRR_SUCCESS) {
        LOGE("renderFrame: texture upload failed");
        return nullptr;
    }

    // Backend-agnostic plumbing: `backend_texture` is an opaque handle, so
    // wrap it in a scratch TextureImpl and route through the owning device.
    nrr::DeviceImpl* dev = reinterpret_cast<nrr::DeviceImpl*>(s->device);
    auto download = [dev](void* backend_tex, void* dst, std::size_t n) -> NRRResult {
        nrr::TextureImpl scratch;
        scratch.backend_texture = backend_tex;
        return dev->download_texture(&scratch, dst, n);
    };
    auto upload = [dev](void* backend_tex, const void* src, std::size_t n) -> NRRResult {
        nrr::TextureImpl scratch;
        scratch.backend_texture = backend_tex;
        return dev->upload_texture(&scratch, src, n);
    };

    NRRFrameInput input = {};
    input.color = s->input;
    input.camera.viewport_width = static_cast<uint32_t>(width);
    input.camera.viewport_height = static_cast<uint32_t>(height);
    NRRFrameOutput output = {};

    nrr::MobileExecutionKernel* kernel = nrr::get_mobile_kernel();
    if (!kernel || !kernel->is_initialized()) {
        LOGE("renderFrame: mobile kernel not initialized");
        return nullptr;
    }
    if (kernel->execute_frame(reinterpret_cast<nrr::ModelImpl*>(s->model),
                              input, output, download, upload) != NRR_SUCCESS ||
        !output.color) {
        LOGE("renderFrame: execute_frame failed");
        return nullptr;
    }

    nrr::TextureImpl* out = reinterpret_cast<nrr::TextureImpl*>(output.color);
    const size_t out_bytes =
        static_cast<size_t>(out->width) * out->height * 3;  // RGB8
    std::vector<uint8_t> rgb(out_bytes, 0);
    if (nrr_texture_download(s->device, output.color, rgb.data(), rgb.size())
            != NRR_SUCCESS) {
        LOGE("renderFrame: output download failed");
        return nullptr;
    }

    jbyteArray result = env->NewByteArray(static_cast<jsize>(out_bytes));
    if (!result) return nullptr;
    env->SetByteArrayRegion(result, 0, static_cast<jsize>(out_bytes),
                            reinterpret_cast<const jbyte*>(rgb.data()));
    return result;
}

// capabilitiesJson() -> NRRCapabilities as JSON (honest EP reporting).
extern "C" JNIEXPORT jstring JNICALL
Java_com_samurai_shugocore_inference_NRRBridge_nativeCapabilitiesJson(
    JNIEnv* env, jclass, jlong handle) {
    ShugoNrrSession* s = as_session(handle);
    if (!s || !s->device) return to_jstr(env, "{}");

    NRRCapabilities caps = {};
    nrr_get_capabilities(s->device, &caps);
    const nrr::MobileExecutionKernel* kernel = nrr::get_mobile_kernel();
    const bool nnapi = kernel && kernel->get_capabilities().supports_nnapi;

    char buf[2048];
    snprintf(buf, sizeof(buf),
             "{\"backend\":\"%s\",\"device_name\":\"%s\","
             "\"device_vendor\":\"%s\",\"neural_acceleration\":\"%s\","
             "\"compute_shader\":\"%s\",\"fp32\":\"%s\",\"fp16\":\"%s\","
             "\"int8\":\"%s\",\"tensor_cores\":\"%s\","
             "\"max_texture_size\":%u,\"vram_mb\":%u,"
             "\"execution_provider\":\"%s\",\"supports_nnapi\":%s}",
             json_escape(s->backend_name).c_str(),
             json_escape(caps.device_name).c_str(),
             json_escape(caps.device_vendor).c_str(),
             cap_state(caps.neural_acceleration),
             cap_state(caps.compute_shader),
             cap_state(caps.fp32), cap_state(caps.fp16),
             cap_state(caps.int8), cap_state(caps.tensor_cores),
             caps.max_texture_size, caps.vram_mb,
             nnapi ? "NNAPI" : "CPU",
             nnapi ? "true" : "false");
    return to_jstr(env, buf);
}

extern "C" JNIEXPORT jstring JNICALL
Java_com_samurai_shugocore_inference_NRRBridge_nativeBackendName(
    JNIEnv* env, jclass, jlong handle) {
    ShugoNrrSession* s = as_session(handle);
    return to_jstr(env, s ? s->backend_name : std::string());
}

extern "C" JNIEXPORT jboolean JNICALL
Java_com_samurai_shugocore_inference_NRRBridge_nativeIsModelLoaded(
    JNIEnv*, jclass, jlong handle) {
    ShugoNrrSession* s = as_session(handle);
    return (s && s->model) ? JNI_TRUE : JNI_FALSE;
}

// powerStatusJson() -> battery/thermal snapshot from the NRR power manager.
extern "C" JNIEXPORT jstring JNICALL
Java_com_samurai_shugocore_inference_NRRBridge_nativePowerStatusJson(
    JNIEnv* env, jclass, jlong handle) {
    ShugoNrrSession* s = as_session(handle);
    if (!s || !s->device) return to_jstr(env, "{}");

    NRRPowerSettings settings = {};
    settings.low_battery_threshold = 0.20f;
    settings.critical_battery_threshold = 0.10f;
    settings.thermal_throttle_threshold = 0.50f;
    settings.enable_dynamic_resolution = 1;
    settings.enable_thermal_throttle = 1;
    settings.min_resolution_scale = 0.50f;
    settings.max_resolution_scale = 1.00f;
    if (nrr_power_manager_init(s->device, &settings) != NRR_SUCCESS) {
        return to_jstr(env, "{}");
    }

    NRRPowerStatus status = {};
    if (nrr_power_manager_get_status(s->device, &status) != NRR_SUCCESS) {
        nrr_power_manager_shutdown(s->device);
        return to_jstr(env, "{}");
    }
    const float scale = nrr_power_manager_get_resolution_scale(s->device);

    char buf[512];
    snprintf(buf, sizeof(buf),
             "{\"battery_level\":%.3f,\"charging\":%d,"
             "\"thermal_headroom\":%.3f,\"low_power_mode\":%d,"
             "\"profile\":%d,\"resolution_scale\":%.3f}",
             static_cast<double>(status.battery_level), status.charging,
             static_cast<double>(status.thermal_headroom),
             status.low_power_mode,
             static_cast<int>(status.current_profile),
             static_cast<double>(scale));
    nrr_power_manager_shutdown(s->device);
    return to_jstr(env, buf);
}

