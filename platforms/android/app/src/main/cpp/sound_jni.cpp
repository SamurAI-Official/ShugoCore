// sound_jni.cpp — JNI bindings for audio perception on Android.
//
// Data flow:  mic frames (16 kHz mono)
//               -> Silero VAD (ONNX, 512-sample chunks)     -> speech probability
//               -> YAMNet      (ONNX, 15600-sample windows) -> 521 scores + embedding
//               -> SoundBridge.kt -> PerceptionState -> the sound/ contract in Python.
//
// Design notes (mirrors nrr_jni.cpp):
//  * nativeCreate() returns a pointer to a ShugoSoundSession; every other call takes
//    that handle. No mutable global state beyond a registry lock.
//  * No audio ever leaves the device: the caller hands over frames it already holds, and
//    only scores/labels/embeddings come back. Same rule as NRR's "no pixel bytes leave
//    the device" -- and for a microphone it is also the privacy rule.
//  * Models live as assets and are extracted to filesDir by SoundBridge.kt (ONNX Runtime
//    needs a real path), exactly as the NRR model is.
//  * Partial support is reported, not hidden: a session with only the VAD loads, and
//    nativeCapabilitiesJson() says which models are live. A node that cannot classify
//    must not look like a node that heard nothing.
//  * Silero v5 keeps TWO pieces of state across chunks: an RNN state [2,1,128] and a
//    64-sample context prepended to each 512-sample chunk (576 in, verified). Both are
//    owned here, so callers pass exactly 512 new samples -- the VAD_CHUNK the Python
//    contract also pins.
//  * NNAPI is not appended (the ORT build in the APK has no NNAPI provider wired), so
//    capabilities report execution_provider "CPU" and nnapi false, honestly.
//  * __ANDROID__ guards keep this file host-compilable for CI header checks.

#include <jni.h>

#include <algorithm>
#include <chrono>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <mutex>
#include <string>
#include <vector>

#include "onnxruntime_c_api.h"

#ifdef __ANDROID__
#include <android/log.h>
#define LOGI(...) __android_log_print(ANDROID_LOG_INFO,  TAG, __VA_ARGS__)
#define LOGW(...) __android_log_print(ANDROID_LOG_WARN,  TAG, __VA_ARGS__)
#define LOGE(...) __android_log_print(ANDROID_LOG_ERROR, TAG, __VA_ARGS__)
#else
#define LOGI(...) do { fprintf(stdout, "[sound_jni] " __VA_ARGS__); fprintf(stdout, "\n"); } while (0)
#define LOGW(...) do { fprintf(stderr, "[sound_jni] W " __VA_ARGS__); fprintf(stderr, "\n"); } while (0)
#define LOGE(...) do { fprintf(stderr, "[sound_jni] E " __VA_ARGS__); fprintf(stderr, "\n"); } while (0)
#endif

#define TAG "sound_jni"

namespace {

constexpr int kSampleRate = 16000;
constexpr int kVadChunk = 512;        // new samples per call (32 ms at 16 kHz)
constexpr int kVadContext = 64;       // prepended from the previous chunk
constexpr int kVadInput = kVadChunk + kVadContext;
constexpr int kVadStateFloats = 2 * 1 * 128;
constexpr int kWindow = 15600;        // YAMNet: 0.975 s at 16 kHz
constexpr int kClasses = 521;
constexpr int kEmbedding = 1024;
constexpr int kTopK = 5;

std::mutex g_session_mutex;

struct ShugoSoundSession {
    const OrtApi* api = nullptr;
    OrtEnv* env = nullptr;
    OrtSession* vad = nullptr;
    OrtSession* classifier = nullptr;
    OrtMemoryInfo* memory = nullptr;
    std::vector<float> state;         // RNN state, carried across chunks
    std::vector<float> context;       // last 64 samples of the previous chunk
    std::mutex mutex;                 // one frame at a time per session
    std::string vad_path;
    std::string model_path;
    double last_inference_ms = 0.0;
};

ShugoSoundSession* as_session(jlong handle) {
    return reinterpret_cast<ShugoSoundSession*>(static_cast<intptr_t>(handle));
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

std::string ort_error(const OrtApi* api, OrtStatus* status) {
    if (!status) return std::string();
    std::string message = api->GetErrorMessage(status);
    api->ReleaseStatus(status);
    return message;
}

// One inference session. Returns nullptr with the reason logged on failure: a missing or
// broken model is not fatal, because the other one may still be usable.
OrtSession* create_session(ShugoSoundSession* s, const std::string& path,
                           const char* label, int threads) {
    if (path.empty()) {
        LOGW("create_session: no %s model path given", label);
        return nullptr;
    }
    OrtSessionOptions* options = nullptr;
    if (OrtStatus* st = s->api->CreateSessionOptions(&options)) {
        LOGE("create_session: CreateSessionOptions failed: %s",
             ort_error(s->api, st).c_str());
        return nullptr;
    }
    // Battery matters more than throughput here: the VAD runs per 32 ms frame, and the
    // classifier once per window.
    s->api->SetIntraOpNumThreads(options, threads);
    s->api->SetSessionGraphOptimizationLevel(options, ORT_ENABLE_ALL);
    OrtSession* session = nullptr;
    if (OrtStatus* st = s->api->CreateSession(s->env, path.c_str(), options, &session)) {
        LOGW("create_session: %s ('%s') did not load: %s", label, path.c_str(),
             ort_error(s->api, st).c_str());
        session = nullptr;
    } else {
        LOGI("create_session: %s loaded from '%s'", label, path.c_str());
    }
    s->api->ReleaseSessionOptions(options);
    return session;
}

// A float tensor over caller-owned memory: the C API does not copy, so the buffer must
// outlive the Run() call.
OrtValue* float_tensor(ShugoSoundSession* s, float* data, const int64_t* dims, int rank) {
    size_t count = 1;
    for (int i = 0; i < rank; ++i) count *= static_cast<size_t>(dims[i]);
    OrtValue* value = nullptr;
    if (OrtStatus* st = s->api->CreateTensorWithDataAsOrtValue(
            s->memory, data, count * sizeof(float), dims, static_cast<size_t>(rank),
            ONNX_TENSOR_ELEMENT_DATA_TYPE_FLOAT, &value)) {
        LOGE("CreateTensorWithDataAsOrtValue failed: %s", ort_error(s->api, st).c_str());
        return nullptr;
    }
    return value;
}

// Silero takes the sample rate as a scalar tensor.
OrtValue* int64_scalar(ShugoSoundSession* s, int64_t* data) {
    const int64_t dims[1] = {1};
    OrtValue* value = nullptr;
    if (OrtStatus* st = s->api->CreateTensorWithDataAsOrtValue(
            s->memory, data, sizeof(int64_t), dims, 1,
            ONNX_TENSOR_ELEMENT_DATA_TYPE_INT64, &value)) {
        LOGE("CreateTensorWithDataAsOrtValue(int64) failed: %s",
             ort_error(s->api, st).c_str());
        return nullptr;
    }
    return value;
}

const float* value_floats(ShugoSoundSession* s, OrtValue* value) {
    if (!value) return nullptr;
    float* data = nullptr;
    if (OrtStatus* st = s->api->GetTensorMutableData(value, reinterpret_cast<void**>(&data))) {
        LOGE("GetTensorMutableData failed: %s", ort_error(s->api, st).c_str());
        return nullptr;
    }
    return data;
}

// Silero v5: [1,576] in (64 context + 512 new), state [2,1,128], sr scalar;
// out: probability [1,1] and the next state.
float run_vad(ShugoSoundSession* s, const float* chunk) {
    std::vector<float> input(kVadInput);
    std::copy(s->context.begin(), s->context.end(), input.begin());
    std::copy(chunk, chunk + kVadChunk, input.begin() + kVadContext);

    const int64_t input_dims[2] = {1, kVadInput};
    const int64_t state_dims[3] = {2, 1, 128};
    int64_t rate = kSampleRate;

    OrtValue* in_input = float_tensor(s, input.data(), input_dims, 2);
    OrtValue* in_state = float_tensor(s, s->state.data(), state_dims, 3);
    OrtValue* in_rate = int64_scalar(s, &rate);
    if (!in_input || !in_state || !in_rate) {
        if (in_input) s->api->ReleaseValue(in_input);
        if (in_state) s->api->ReleaseValue(in_state);
        if (in_rate) s->api->ReleaseValue(in_rate);
        return -1.0f;
    }

    const char* names[] = {"input", "state", "sr"};
    const OrtValue* values[] = {in_input, in_state, in_rate};
    const char* out_names[] = {"output", "stateN"};
    OrtValue* outputs[2] = {nullptr, nullptr};
    float probability = -1.0f;

    if (OrtStatus* st = s->api->Run(s->vad, nullptr, names, values, 3, out_names, 2, outputs)) {
        LOGE("vad Run failed: %s", ort_error(s->api, st).c_str());
    } else {
        const float* prob = value_floats(s, outputs[0]);
        const float* next = value_floats(s, outputs[1]);
        if (prob && next) {
            probability = prob[0];
            std::copy(next, next + kVadStateFloats, s->state.begin());
        } else {
            probability = -1.0f;
        }
    }

    s->api->ReleaseValue(in_input);
    s->api->ReleaseValue(in_state);
    s->api->ReleaseValue(in_rate);
    for (OrtValue* value : outputs) {
        if (value) s->api->ReleaseValue(value);
    }
    // Context for the next chunk is the tail of THIS chunk, not of the assembled input.
    std::copy(chunk + kVadChunk - kVadContext, chunk + kVadChunk, s->context.begin());
    return probability;
}

}  // namespace

// ---------------------------------------------------------------------------
// The JNI surface. Names must match SoundBridge.kt exactly; the cross-language
// contract test fails the suite if either side renames one.
// ---------------------------------------------------------------------------

// create(vadPath, modelPath) -> session handle, or 0 when neither model loads.
extern "C" JNIEXPORT jlong JNICALL
Java_com_samurai_shugocore_inference_SoundBridge_nativeCreate(
    JNIEnv* env, jclass, jstring jvad, jstring jmodel) {
    std::lock_guard<std::mutex> lock(g_session_mutex);

    auto* s = new ShugoSoundSession();
    s->vad_path = jstr(env, jvad);
    s->model_path = jstr(env, jmodel);
    s->state.assign(kVadStateFloats, 0.0f);
    s->context.assign(kVadContext, 0.0f);

    s->api = OrtGetApiBase()->GetApi(ORT_API_VERSION);
    if (!s->api) {
        LOGE("nativeCreate: OrtGetApiBase returned no API");
        delete s;
        return 0;
    }
    if (OrtStatus* st = s->api->CreateEnv(ORT_LOGGING_LEVEL_WARNING, "shugocore_sound",
                                          &s->env)) {
        LOGE("nativeCreate: CreateEnv failed: %s", ort_error(s->api, st).c_str());
        delete s;
        return 0;
    }
    if (OrtStatus* st = s->api->CreateCpuMemoryInfo(OrtArenaAllocator, OrtMemTypeDefault,
                                                    &s->memory)) {
        LOGE("nativeCreate: CreateCpuMemoryInfo failed: %s",
             ort_error(s->api, st).c_str());
        s->api->ReleaseEnv(s->env);
        delete s;
        return 0;
    }

    // One thread for the per-frame VAD; two for the per-window classifier.
    s->vad = create_session(s, s->vad_path, "vad", 1);
    s->classifier = create_session(s, s->model_path, "yamnet", 2);
    if (!s->vad && !s->classifier) {
        LOGW("nativeCreate: neither model loaded; audio perception unavailable");
        s->api->ReleaseMemoryInfo(s->memory);
        s->api->ReleaseEnv(s->env);
        delete s;
        return 0;
    }
    LOGI("nativeCreate: vad=%s classifier=%s", s->vad ? "yes" : "no",
         s->classifier ? "yes" : "no");
    return static_cast<jlong>(reinterpret_cast<intptr_t>(s));
}

extern "C" JNIEXPORT void JNICALL
Java_com_samurai_shugocore_inference_SoundBridge_nativeDestroy(
    JNIEnv*, jclass, jlong handle) {
    ShugoSoundSession* s = as_session(handle);
    if (!s) return;
    std::lock_guard<std::mutex> guard(s->mutex);
    if (s->vad) s->api->ReleaseSession(s->vad);
    if (s->classifier) s->api->ReleaseSession(s->classifier);
    if (s->memory) s->api->ReleaseMemoryInfo(s->memory);
    if (s->env) s->api->ReleaseEnv(s->env);
    s->vad = nullptr;
    s->classifier = nullptr;
    s->memory = nullptr;
    s->env = nullptr;
    delete s;
}

// Clear the VAD's carried state: a new utterance, or a gap in the audio, must not be
// judged against the previous context.
extern "C" JNIEXPORT void JNICALL
Java_com_samurai_shugocore_inference_SoundBridge_nativeResetVad(
    JNIEnv*, jclass, jlong handle) {
    ShugoSoundSession* s = as_session(handle);
    if (!s) return;
    std::lock_guard<std::mutex> guard(s->mutex);
    std::fill(s->state.begin(), s->state.end(), 0.0f);
    std::fill(s->context.begin(), s->context.end(), 0.0f);
}

// speechProbability(chunk) -> 0..1, or -1 when there is no VAD (never a silent 0.0,
// which would mean "definitely not speech").
extern "C" JNIEXPORT jfloat JNICALL
Java_com_samurai_shugocore_inference_SoundBridge_nativeSpeechProbability(
    JNIEnv* env, jclass, jlong handle, jfloatArray jchunk) {
    ShugoSoundSession* s = as_session(handle);
    if (!s || !s->vad || !jchunk) return -1.0f;
    if (env->GetArrayLength(jchunk) != kVadChunk) {
        LOGE("speechProbability: chunk is %d samples, expected %d",
             env->GetArrayLength(jchunk), kVadChunk);
        return -1.0f;
    }
    std::vector<float> chunk(kVadChunk);
    env->GetFloatArrayRegion(jchunk, 0, kVadChunk, chunk.data());
    std::lock_guard<std::mutex> guard(s->mutex);
    return static_cast<jfloat>(run_vad(s, chunk.data()));
}

// analyze(window, wantEmbedding) -> JSON:
//   {"status":"ok","top":[{"index":0,"score":0.993},...],"embedding":[...]}
//
// Indices are model indices, not names: the label table lives in Python
// (sound.models.load_class_map), so this layer never ships one and a class reorder cannot
// be made here by accident. wantEmbedding is the caller's choice because the embedding is
// the one part of this payload that we do not publish by default.
extern "C" JNIEXPORT jstring JNICALL
Java_com_samurai_shugocore_inference_SoundBridge_nativeAnalyze(
    JNIEnv* env, jclass, jlong handle, jfloatArray jwindow, jboolean jembedding) {
    ShugoSoundSession* s = as_session(handle);
    if (!s) return to_jstr(env, "{\"status\":\"no_session\"}");
    if (!s->classifier) return to_jstr(env, "{\"status\":\"no_model\"}");
    if (!jwindow || env->GetArrayLength(jwindow) != kWindow) {
        return to_jstr(env, "{\"status\":\"bad_window\"}");
    }
    std::vector<float> window(kWindow);
    env->GetFloatArrayRegion(jwindow, 0, kWindow, window.data());

    std::lock_guard<std::mutex> guard(s->mutex);
    const int64_t dims[1] = {kWindow};
    OrtValue* in_window = float_tensor(s, window.data(), dims, 1);
    if (!in_window) return to_jstr(env, "{\"status\":\"tensor_failed\"}");

    const bool want_embedding = (jembedding == JNI_TRUE);
    const char* names[] = {"waveform"};
    const OrtValue* values[] = {in_window};
    const char* out_names[2] = {"activation", "global_average_pooling2d"};
    OrtValue* outputs[2] = {nullptr, nullptr};
    const int out_count = want_embedding ? 2 : 1;

    std::string json;
    const auto started = std::chrono::steady_clock::now();
    if (OrtStatus* st = s->api->Run(s->classifier, nullptr, names, values, 1, out_names,
                                    out_count, outputs)) {
        LOGE("analyze: Run failed: %s", ort_error(s->api, st).c_str());
        json = "{\"status\":\"run_failed\"}";
    } else {
        const float* scores = value_floats(s, outputs[0]);
        if (!scores) {
            json = "{\"status\":\"read_failed\"}";
        } else {
            std::vector<int> order(kClasses);
            for (int i = 0; i < kClasses; ++i) order[i] = i;
            std::partial_sort(order.begin(), order.begin() + kTopK, order.end(),
                              [scores](int a, int b) { return scores[a] > scores[b]; });
            char buffer[64];
            json = "{\"status\":\"ok\",\"top\":[";
            for (int i = 0; i < kTopK; ++i) {
                std::snprintf(buffer, sizeof(buffer), "%s{\"index\":%d,\"score\":%.4f}",
                              i ? "," : "", order[i], static_cast<double>(scores[order[i]]));
                json += buffer;
            }
            json += "]";
            const float* embedding = want_embedding ? value_floats(s, outputs[1]) : nullptr;
            if (embedding) {
                json += ",\"embedding\":[";
                for (int i = 0; i < kEmbedding; ++i) {
                    std::snprintf(buffer, sizeof(buffer), "%s%.4f", i ? "," : "",
                                  static_cast<double>(embedding[i]));
                    json += buffer;
                }
                json += "]";
            }
            json += "}";
        }
    }
    s->last_inference_ms = std::chrono::duration<double, std::milli>(
        std::chrono::steady_clock::now() - started).count();

    s->api->ReleaseValue(in_window);
    for (OrtValue* value : outputs) {
        if (value) s->api->ReleaseValue(value);
    }
    return to_jstr(env, json);
}

// What this session can actually do, for the node's capability report. Reported, never
// assumed: the execution provider is the one ORT really uses, and NNAPI is false because
// nothing appends it (the same honesty NRR applies to its own provider claims).
extern "C" JNIEXPORT jstring JNICALL
Java_com_samurai_shugocore_inference_SoundBridge_nativeCapabilitiesJson(
    JNIEnv* env, jclass, jlong handle) {
    ShugoSoundSession* s = as_session(handle);
    if (!s) return to_jstr(env, "{}");
    char buffer[512];
    std::snprintf(buffer, sizeof(buffer),
                  "{\"execution_provider\":\"CPU\",\"nnapi\":false,\"vad\":%s,"
                  "\"classifier\":%s,\"sample_rate\":%d,\"chunk_samples\":%d,"
                  "\"window_samples\":%d,\"classes\":%d,\"embedding\":%s,"
                  "\"last_ms\":%.2f}",
                  s->vad ? "true" : "false", s->classifier ? "true" : "false",
                  kSampleRate, kVadChunk, kWindow, kClasses,
                  s->classifier ? "true" : "false", s->last_inference_ms);
    return to_jstr(env, buffer);
}
