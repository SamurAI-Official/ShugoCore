// nrr_probe.cpp — end-to-end NRR diagnostic (host CI gate + on-device smoke).
//
// Mirrors upstream tests/unit/test_mobile.cpp:
//   create device -> load model -> create RGBA8 texture -> upload ->
//   mobile kernel execute_frame -> download output.
//
// Two deliberate differences from upstream's own test:
//
//  1. The device is created with an EXPLICIT preferred_backend.  Upstream's
//     vendor backends report is_supported() == true without probing for
//     their GPU, and Adreno (priority 60) outranks CPU (10), so
//     auto-selection resolves to Adreno on every SoC and then fails GPU
//     detection on non-Qualcomm silicon.
//
//  2. Download/upload are wired to DeviceImpl instead of being left null, so
//     real pixels reach the model.  (Upstream's test passes null and the
//     kernel falls back to a flat gray frame.)  The lambdas are
//     backend-agnostic: `backend_texture` is an opaque handle, so it is
//     wrapped in a scratch TextureImpl and routed through the device.
//
// Prints one JSON object on stdout so a harness can assert on it.

#include <cstdio>
#include <cstring>
#include <string>
#include <vector>

#include "nrr.h"
#include "nrr_device.h"
#include "nrr_model.h"
#include "nrr_runtime.h"
#include "mobile/mobile_kernel.h"

namespace {

int g_failures = 0;

void check(bool cond, const char* what) {
    if (!cond) {
        ++g_failures;
        fprintf(stderr, "[nrr_probe] FAIL: %s\n", what);
    }
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

std::string json_escape(const std::string& in) {
    std::string out;
    out.reserve(in.size());
    for (char c : in) {
        if (c == '"' || c == '\\') { out.push_back('\\'); out.push_back(c); }
        else if (c == '\n') { out += "\\n"; }
        else if (static_cast<unsigned char>(c) >= 0x20) { out.push_back(c); }
    }
    return out;
}

}  // namespace
int main(int argc, char** argv) {
    std::string backend = "CPU";
    std::string model_path;
    for (int i = 1; i < argc; ++i) {
        const std::string a = argv[i];
        if (a == "--backend" && i + 1 < argc)      backend = argv[++i];
        else if (a == "--model" && i + 1 < argc)   model_path = argv[++i];
    }

    // Resolve a default sample model when one was not supplied.
    if (model_path.empty()) {
        const char* candidates[] = {
            "models/nrr_upscaler_v0.1.onnx",
            "nrr/models/nrr_upscaler_v0.1.onnx",
            "/data/local/tmp/nrr_upscaler_v0.1.onnx",
        };
        for (const char* c : candidates) {
            FILE* f = fopen(c, "rb");
            if (f) { fclose(f); model_path = c; break; }
        }
    }

    // --- device (explicit backend: see header note) ------------------------
    NRRDeviceOptions opts = {};
    opts.preferred_backend = backend.c_str();
    opts.frames_in_flight = 1;

    NRRDevice* device = nullptr;
    const NRRResult dc = nrr_device_create(&opts, &device);
    check(dc == NRR_SUCCESS && device != nullptr, "device create");

    char backend_name[64] = {};
    NRRCapabilities caps = {};
    if (device) {
        nrr_get_backend_name(device, backend_name, sizeof(backend_name));
        nrr_get_capabilities(device, &caps);
    }

    // --- model -------------------------------------------------------------
    NRRModel* model = nullptr;
    bool model_loaded = false;
    if (device && !model_path.empty()) {
        model_loaded =
            (nrr_model_load(device, model_path.c_str(), &model) == NRR_SUCCESS)
            && model != nullptr;
        check(model_loaded, "model load");
    }

    // --- mobile execution kernel ------------------------------------------
    nrr::MobileExecutionKernel* kernel = nrr::get_mobile_kernel();
    bool kernel_ready = false;
    if (kernel && !kernel->is_initialized()) {
        kernel_ready = kernel->initialize(nrr::MobileEP::CPU,
                                         256u * 1024u * 1024u,
                                         true /* fp16 */, false /* quantized */,
                                         true /* cpu fallback */);
    } else if (kernel) {
        kernel_ready = true;
    }
    check(kernel_ready, "mobile kernel initialize");

    // --- frame -------------------------------------------------------------
    const uint32_t W = 16, H = 16;
    NRRTexture* color = nullptr;
    bool frame_ok = false;
    bool output_ok = false;
    size_t distinct = 0;

    if (device) {
        NRRTextureDesc td = {};
        td.width = W;
        td.height = H;
        td.format = NRR_TEXTURE_FORMAT_RGBA8;
        td.usage = NRR_TEXTURE_USAGE_COLOR;
        check(nrr_texture_create(device, &td, &color) == NRR_SUCCESS && color,
              "texture create");

        std::vector<uint8_t> rgba(static_cast<size_t>(W) * H * 4, 0);
        for (uint32_t y = 0; y < H; ++y) {
            for (uint32_t x = 0; x < W; ++x) {
                const size_t i = (static_cast<size_t>(y) * W + x) * 4;
                rgba[i]     = static_cast<uint8_t>((x * 255u) / (W - 1));
                rgba[i + 1] = static_cast<uint8_t>((y * 255u) / (H - 1));
                rgba[i + 2] = 128;
                rgba[i + 3] = 255;
            }
        }
        if (color) {
            check(nrr_texture_upload(device, color, rgba.data(), rgba.size())
                      == NRR_SUCCESS,
                  "texture upload");
        }

        if (kernel_ready && model) {
            // Backend-agnostic texture plumbing: `backend_texture` is an
            // opaque handle, so wrap it in a scratch TextureImpl and route
            // through the owning device.
            nrr::DeviceImpl* dev = reinterpret_cast<nrr::DeviceImpl*>(device);
            auto download = [dev](void* backend_tex, void* dst,
                                  std::size_t n) -> NRRResult {
                nrr::TextureImpl scratch;
                scratch.backend_texture = backend_tex;
                return dev->download_texture(&scratch, dst, n);
            };
            auto upload = [dev](void* backend_tex, const void* src,
                                std::size_t n) -> NRRResult {
                nrr::TextureImpl scratch;
                scratch.backend_texture = backend_tex;
                return dev->upload_texture(&scratch, src, n);
            };

            NRRFrameInput input = {};
            NRRFrameOutput output = {};
            input.color = color;
            input.camera.viewport_width = W;
            input.camera.viewport_height = H;

            frame_ok = kernel->execute_frame(
                           reinterpret_cast<nrr::ModelImpl*>(model),
                           input, output, download, upload) == NRR_SUCCESS;

            if (frame_ok && output.color) {
                std::vector<uint8_t> rgb(static_cast<size_t>(W) * H * 3, 0);
                if (nrr_texture_download(device, output.color, rgb.data(),
                                         rgb.size()) == NRR_SUCCESS) {
                    output_ok = true;
                    bool seen[256] = {false};
                    for (uint8_t b : rgb) seen[b] = true;
                    for (bool s : seen) if (s) ++distinct;
                }
            }
        }
    }

    // --- report ------------------------------------------------------------
    std::string caps_json = "{}";
    if (device) {
        char buf[2048];
        snprintf(buf, sizeof(buf),
                 "{\"device_name\":\"%s\",\"device_vendor\":\"%s\","
                 "\"neural_acceleration\":\"%s\",\"compute_shader\":\"%s\","
                 "\"fp32\":\"%s\",\"fp16\":\"%s\",\"int8\":\"%s\","
                 "\"tensor_cores\":\"%s\",\"async_compute\":\"%s\","
                 "\"vram_mb\":%u,\"max_texture_size\":%u,"
                 "\"model_execution_score\":%.3f}",
                 json_escape(caps.device_name).c_str(),
                 json_escape(caps.device_vendor).c_str(),
                 cap_state(caps.neural_acceleration),
                 cap_state(caps.compute_shader),
                 cap_state(caps.fp32), cap_state(caps.fp16),
                 cap_state(caps.int8), cap_state(caps.tensor_cores),
                 cap_state(caps.async_compute),
                 caps.vram_mb, caps.max_texture_size,
                 static_cast<double>(caps.model_execution_score));
        caps_json = buf;
    }

    std::string kernel_json = "{}";
    if (kernel) {
        const nrr::MobileCapabilities& mc = kernel->get_capabilities();
        char buf[1024];
        snprintf(buf, sizeof(buf),
                 "{\"initialized\":%s,\"loaded\":%s,\"active_ep\":\"%s\","
                 "\"supports_nnapi\":%s,\"supports_core_ml\":%s,"
                 "\"supports_fp16\":%s,\"supports_int8\":%s,"
                 "\"max_texture_size\":%u,\"gpu_name\":\"%s\"}",
                 kernel->is_initialized() ? "true" : "false",
                 kernel->is_loaded() ? "true" : "false",
                 json_escape(kernel->get_active_ep_name()).c_str(),
                 mc.supports_nnapi ? "true" : "false",
                 mc.supports_core_ml ? "true" : "false",
                 mc.supports_fp16 ? "true" : "false",
                 mc.supports_int8 ? "true" : "false",
                 mc.max_texture_size,
                 json_escape(mc.gpu_name).c_str());
        kernel_json = buf;
    }

    printf("{\n");
#ifdef NRR_HAVE_ONNXRUNTIME
    printf("  \"onnxruntime\": true,\n");
#else
    printf("  \"onnxruntime\": false,\n");
#endif
    printf("  \"preferred_backend\": \"%s\",\n", json_escape(backend).c_str());
    printf("  \"active_backend\": \"%s\",\n", json_escape(backend_name).c_str());
    printf("  \"device_created\": %s,\n",
           (dc == NRR_SUCCESS && device) ? "true" : "false");
    printf("  \"capabilities\": %s,\n", caps_json.c_str());
    printf("  \"model_path\": \"%s\",\n", json_escape(model_path).c_str());
    printf("  \"model_loaded\": %s,\n", model_loaded ? "true" : "false");
    printf("  \"kernel\": %s,\n", kernel_json.c_str());
    printf("  \"execute_frame\": %s,\n", frame_ok ? "true" : "false");
    printf("  \"output_texture\": %s,\n", output_ok ? "true" : "false");
    printf("  \"output_distinct_bytes\": %zu,\n", distinct);
    printf("  \"failures\": %d,\n", g_failures);
    printf("  \"ok\": %s\n", g_failures == 0 ? "true" : "false");
    printf("}\n");

    // --- teardown ----------------------------------------------------------
    if (color && device) nrr_texture_destroy(device, color);
    if (model) nrr_model_unload(model);
    if (device) nrr_device_destroy(device);
    nrr::destroy_mobile_kernel();

    return g_failures == 0 ? 0 : 1;
}

