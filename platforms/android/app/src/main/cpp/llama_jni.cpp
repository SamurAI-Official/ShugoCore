// llama_jni.cpp — JNI bindings for real llama.cpp inference on Android.
//
// Pinned against llama.cpp b10795 (submodule at cpp/llama.cpp).
// Data flow:  GGUF -> llama.cpp -> JNI (this file) -> LlamaCppBridge.kt
//             -> LocalApiServer.kt -> ShugoCore (Ollama-compatible HTTP).
//
// Design notes:
//  * nativeInit() returns a pointer to a ShugoSession holding the model,
//    context, vocab, KV position and a pending-UTF-8 buffer. All other
//    calls take the session pointer: no mutable global state.
//  * nativeDetokenize() only returns complete UTF-8 sequences (GGUF vocabs
//    split multi-byte characters across tokens); nativeDrain() flushes
//    the tail at end-of-generation.
//  * __ANDROID__ guards keep this file host-compilable for CI header checks.

#include <jni.h>

#include <string>
#include <vector>
#include <thread>
#include <mutex>
#include <cstdint>
#include <cstring>

#ifdef __ANDROID__
#include <android/log.h>
#define LOGI(...) __android_log_print(ANDROID_LOG_INFO,  TAG, __VA_ARGS__)
#define LOGW(...) __android_log_print(ANDROID_LOG_WARN,  TAG, __VA_ARGS__)
#define LOGE(...) __android_log_print(ANDROID_LOG_ERROR, TAG, __VA_ARGS__)
#else
#include <cstdio>
#define LOGI(...) do { fprintf(stdout, "[llama_jni] " __VA_ARGS__); fprintf(stdout, "\n"); } while (0)
#define LOGW(...) do { fprintf(stderr, "[llama_jni] W " __VA_ARGS__); fprintf(stderr, "\n"); } while (0)
#define LOGE(...) do { fprintf(stderr, "[llama_jni] E " __VA_ARGS__); fprintf(stderr, "\n"); } while (0)
#endif

#include "llama.h"

#define TAG "llama_jni"

namespace {

struct ShugoSession {
    llama_model*       model  = nullptr;
    llama_context*     ctx    = nullptr;
    const llama_vocab* vocab  = nullptr;
    uint32_t           n_ctx  = 0;
    int32_t            n_past = 0;
    std::string        pending; // partial UTF-8 bytes awaiting completion
};

std::mutex g_session_mutex; // serializes inference on the single session

ShugoSession* as_session(jlong ptr) {
    return reinterpret_cast<ShugoSession*>(static_cast<intptr_t>(ptr));
}

// Extract only complete UTF-8 sequences from `pending`, leaving any trailing
// partial sequence in place for the next call.
std::string take_complete_utf8(std::string& pending) {
    std::string out;
    size_t i = 0;
    while (i < pending.size()) {
        const unsigned char c = static_cast<unsigned char>(pending[i]);
        size_t len = 1;
        if      ((c & 0x80u) == 0x00u) len = 1;
        else if ((c & 0xE0u) == 0xC0u) len = 2;
        else if ((c & 0xF0u) == 0xE0u) len = 3;
        else if ((c & 0xF8u) == 0xF0u) len = 4;
        else { ++i; continue; } // stray continuation byte: drop
        if (i + len > pending.size()) break; // incomplete: wait for more bytes
        out.append(pending, i, len);
        i += len;
    }
    pending.erase(0, i);
    return out;
}

bool decode_tokens(ShugoSession* s, const llama_token* tokens, int32_t n_tokens) {
    llama_batch batch = llama_batch_get_one(const_cast<llama_token*>(tokens), n_tokens);
    const int rc = llama_decode(s->ctx, batch); // 0 = success, <0 = error, >0 = warning
    if (rc < 0) {
        LOGE("llama_decode failed rc=%d", rc);
        return false;
    }
    s->n_past += n_tokens;
    return true;
}

} // namespace


extern "C" JNIEXPORT jint JNICALL JNI_OnLoad(JavaVM*, void*) {
    LOGI("JNI_OnLoad");
    return JNI_VERSION_1_6;
}

// ---------------------------------------------------------------------------
// Session lifecycle
// ---------------------------------------------------------------------------

extern "C" JNIEXPORT jlong JNICALL
Java_com_samurai_shugocore_inference_LlamaCppBridge_nativeInit(
        JNIEnv* env, jobject, jstring model_path, jint n_ctx, jint n_threads,
        jint n_gpu_layers) {
    if (!model_path) return 0;

    const char* path = env->GetStringUTFChars(model_path, nullptr);
    if (!path) return 0;
    const std::string path_str(path);
    env->ReleaseStringUTFChars(model_path, path);

    if (n_threads < 1) {
        n_threads = static_cast<jint>(std::thread::hardware_concurrency());
        if (n_threads < 1) n_threads = 2;
    }
    if (n_ctx < 64) n_ctx = 2048;

    std::lock_guard<std::mutex> lock(g_session_mutex);
    llama_backend_init();

    llama_model_params mparams = llama_model_default_params();
    mparams.n_gpu_layers = n_gpu_layers; // negative = all layers on GPU

    llama_model* model = llama_model_load_from_file(path_str.c_str(), mparams);
    if (!model) {
        LOGE("failed to load model from %s", path_str.c_str());
        llama_backend_free();
        return 0;
    }

    llama_context_params cparams = llama_context_default_params();
    cparams.n_ctx           = static_cast<uint32_t>(n_ctx);
    cparams.n_batch         = static_cast<uint32_t>(n_ctx < 512 ? n_ctx : 512);
    cparams.n_threads       = n_threads;
    cparams.n_threads_batch = n_threads;

    llama_context* ctx = llama_init_from_model(model, cparams);
    if (!ctx) {
        LOGE("failed to create context");
        llama_model_free(model);
        llama_backend_free();
        return 0;
    }

    auto* session = new ShugoSession();
    session->model = model;
    session->ctx   = ctx;
    session->vocab = llama_model_get_vocab(model);
    session->n_ctx = llama_n_ctx(ctx);
    LOGI("session ready: n_ctx=%u", session->n_ctx);
    return reinterpret_cast<jlong>(session);
}

extern "C" JNIEXPORT void JNICALL
Java_com_samurai_shugocore_inference_LlamaCppBridge_nativeFree(
        JNIEnv*, jobject, jlong session_ptr) {
    auto* s = as_session(session_ptr);
    if (!s) return;
    std::lock_guard<std::mutex> lock(g_session_mutex);
    if (s->ctx)   llama_free(s->ctx);
    if (s->model) llama_model_free(s->model);
    delete s;
    llama_backend_free();
    LOGI("session freed");
}


// ---------------------------------------------------------------------------
// Tokenization
// ---------------------------------------------------------------------------

extern "C" JNIEXPORT jint JNICALL
Java_com_samurai_shugocore_inference_LlamaCppBridge_nativeTokenize(
        JNIEnv* env, jobject, jlong session_ptr, jstring text,
        jintArray out_tokens, jint max_tokens, jboolean add_special) {
    auto* s = as_session(session_ptr);
    if (!s || !s->vocab || !text || !out_tokens || max_tokens < 1) return 0;

    const char* utf = env->GetStringUTFChars(text, nullptr);
    if (!utf) return 0;
    const std::string text_str(utf);
    env->ReleaseStringUTFChars(text, utf);

    std::vector<llama_token> tokens(static_cast<size_t>(max_tokens));
    const int32_t n = llama_tokenize(
            s->vocab, text_str.c_str(), static_cast<int32_t>(text_str.size()),
            tokens.data(), max_tokens, add_special == JNI_TRUE, /*parse_special=*/true);
    if (n < 0) {
        // Negative return: number of tokens that would have been produced.
        LOGW("tokenize overflow: needed %d tokens (max %d)", -n, max_tokens);
        return 0;
    }
    if (n > 0) {
        env->SetIntArrayRegion(out_tokens, 0, n,
                               reinterpret_cast<const jint*>(tokens.data()));
    }
    return n;
}

extern "C" JNIEXPORT jstring JNICALL
Java_com_samurai_shugocore_inference_LlamaCppBridge_nativeDetokenize(
        JNIEnv* env, jobject, jlong session_ptr, jint token_id) {
    auto* s = as_session(session_ptr);
    if (!s || !s->vocab) return env->NewStringUTF("");

    char buf[512];
    const int32_t n = llama_token_to_piece(
            s->vocab, static_cast<llama_token>(token_id),
            buf, static_cast<int32_t>(sizeof(buf)), /*lstrip=*/0, /*special=*/false);
    if (n > 0) s->pending.append(buf, static_cast<size_t>(n));
    return env->NewStringUTF(take_complete_utf8(s->pending).c_str());
}

// Flush any trailing partial UTF-8 sequence buffered by nativeDetokenize.
extern "C" JNIEXPORT jstring JNICALL
Java_com_samurai_shugocore_inference_LlamaCppBridge_nativeDrain(
        JNIEnv* env, jobject, jlong session_ptr) {
    auto* s = as_session(session_ptr);
    if (!s) return env->NewStringUTF("");
    std::string tail;
    tail.swap(s->pending);
    return env->NewStringUTF(tail.c_str());
}


// ---------------------------------------------------------------------------
// Prompt evaluation + generation
// ---------------------------------------------------------------------------

extern "C" JNIEXPORT jboolean JNICALL
Java_com_samurai_shugocore_inference_LlamaCppBridge_nativeEvalPrompt(
        JNIEnv* env, jobject, jlong session_ptr, jintArray tokens, jint n_tokens) {
    auto* s = as_session(session_ptr);
    if (!s || !s->ctx || !tokens || n_tokens < 1) return JNI_FALSE;
    if (env->GetArrayLength(tokens) < n_tokens) return JNI_FALSE;

    std::vector<llama_token> prompt(static_cast<size_t>(n_tokens));
    env->GetIntArrayRegion(tokens, 0, n_tokens,
                           reinterpret_cast<jint*>(prompt.data()));

    // Reset KV when the new prompt would overflow the context window.
    if (s->n_past + n_tokens > static_cast<int32_t>(s->n_ctx)) {
        LOGW("context full (n_past=%d), clearing KV cache", s->n_past);
        llama_memory_clear(llama_get_memory(s->ctx), /*data=*/true);
        s->n_past = 0;
    }
    return decode_tokens(s, prompt.data(), n_tokens) ? JNI_TRUE : JNI_FALSE;
}

// Sample one token, decode it into the KV cache and return its id.
// Returns -1 on error, -2 on end-of-generation (token NOT added to KV).
extern "C" JNIEXPORT jint JNICALL
Java_com_samurai_shugocore_inference_LlamaCppBridge_nativeGenerateToken(
        JNIEnv*, jobject, jlong session_ptr, jfloat temperature, jint top_k,
        jfloat top_p, jint repeat_last_n, jfloat repeat_penalty, jint seed) {
    auto* s = as_session(session_ptr);
    if (!s || !s->ctx || !s->vocab) return -1;

    llama_sampler_chain_params sparams = llama_sampler_chain_default_params();
    sparams.no_perf = true; // skip timing instrumentation on-device

    llama_sampler* chain = llama_sampler_chain_init(sparams);
    if (!chain) return -1;

    // Canonical chain order: penalties -> top_k -> top_p -> temp -> dist.
    llama_sampler_chain_add(chain, llama_sampler_init_penalties(
            /*n_vocab=*/llama_vocab_n_tokens(s->vocab),
            repeat_last_n, repeat_penalty, /*frequency=*/0.0f, /*presence=*/0.0f));
    if (top_k > 0) {
        llama_sampler_chain_add(chain, llama_sampler_init_top_k(top_k));
    }
    if (top_p < 1.0f) {
        llama_sampler_chain_add(chain, llama_sampler_init_top_p(top_p, /*min_keep=*/1));
    }
    llama_sampler_chain_add(chain, llama_sampler_init_temp(temperature));
    const uint32_t useed = (seed < 0) ? LLAMA_DEFAULT_SEED
                                      : static_cast<uint32_t>(seed);
    llama_sampler_chain_add(chain, llama_sampler_init_dist(useed));

    const llama_token id = llama_sampler_sample(chain, s->ctx, /*idx=*/-1);
    llama_sampler_free(chain);

    if (llama_vocab_is_eog(s->vocab, id)) return -2;

    if (!decode_tokens(s, &id, 1)) return -1;
    return static_cast<jint>(id);
}

// Clear KV cache + n_past + UTF-8 buffer (start of a fresh request).
extern "C" JNIEXPORT void JNICALL
Java_com_samurai_shugocore_inference_LlamaCppBridge_nativeReset(
        JNIEnv*, jobject, jlong session_ptr) {
    auto* s = as_session(session_ptr);
    if (!s || !s->ctx) return;
    std::lock_guard<std::mutex> lock(g_session_mutex);
    llama_memory_clear(llama_get_memory(s->ctx), /*data=*/true);
    s->n_past = 0;
    s->pending.clear();
}
