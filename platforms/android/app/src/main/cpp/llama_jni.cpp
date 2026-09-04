// llama_jni.cpp - JNI bindings for llama.cpp on Android
#include <jni.h>
#include <string>
#include <vector>
#include <thread>
#include <mutex>
#include <android/log.h>
#include "llama.cpp/llama.h"

#define TAG "llama_jni"
#define LOGI(...) __android_log_print(ANDROID_LOG_INFO, TAG, __VA_ARGS__)
#define LOGW(...) __android_log_print(ANDROID_LOG_WARN, TAG, __VA_ARGS__)
#define LOGE(...) __android_log_print(ANDROID_LOG_ERROR, TAG, __VA_ARGS__)

static std::mutex g_mutex;
static llama_context* g_context = nullptr;
static llama_model* g_model = nullptr;

extern "C" JNIEXPORT jint JNICALL JNI_OnLoad(JavaVM* vm, void* reserved) {
    LOGI("JNI_OnLoad called");
    return JNI_VERSION_1_6;
}

extern "C" JNIEXPORT jlong JNICALL
Java_com_samurai_shugocore_inference_LlamaCppBridge_nativeInit(
    JNIEnv* env, jobject thiz, jstring model_path, jint n_ctx,
    jint n_threads, jint n_gpu_layers) {
    LOGI("nativeInit called");
    std::lock_guard<std::mutex> lock(g_mutex);
    const char* path = env->GetStringUTFChars(model_path, 0);
    if (!path) return 0;
    std::string model_path_str(path);
    env->ReleaseStringUTFChars(model_path, path);
    if (n_threads < 1) n_threads = std::thread::hardware_concurrency();
    llama_model_backend_init();
    llama_model_params model_params = llama_model_default_params();
    model_params.n_gpu_layer = n_gpu_layers;
    g_model = llama_model_load(model_path_str.c_str(), model_params);
    if (!g_model) {
        LOGE("Failed to load model: %s", model_path_str.c_str());
        llama_model_backend_free();
        return 0;
    }
    llama_context_params ctx_params = llama_context_default_params();
    ctx_params.n_ctx = n_ctx;
    ctx_params.n_threads = n_threads;
    g_context = llama_new_context(g_model, ctx_params);
    if (!g_context) {
        LOGE("Failed to create context");
        llama_model_free(g_model);
        g_model = nullptr;
        llama_model_backend_free();
        return 0;
    }
    LOGI("nativeInit successful, context=%p", g_context);
    return reinterpret_cast<jlong>(g_context);
}

extern "C" JNIEXPORT jboolean JNICALL
Java_com_samurai_shugocore_inference_LlamaCppBridge_nativeEvalPrompt(
    JNIEnv* env, jobject thiz, jintArray tokens, jint n_tokens) {
    if (!g_context) return JNI_FALSE;
    jsize len = env->GetArrayLength(tokens);
    jint* data = env->GetIntArrayElements(tokens, 0);
    std::vector<llama_token> prompt_tokens(data, data + len);
    env->ReleaseIntArrayElements(tokens, data, 0);
    if (llama_eval(g_context, prompt_tokens.data(), prompt_tokens.size(), 0, 1)) {
        return JNI_TRUE;
    }
    return JNI_FALSE;
}

extern "C" JNIEXPORT jint JNICALL
Java_com_samurai_shugocore_inference_LlamaCppBridge_nativeGenerateToken(
    JNIEnv* env, jobject thiz, jfloat temp, jint top_k, jfloat top_p) {
    if (!g_context) return -1;
    llama_token id = llama_sample_top_p_top_k(g_context, top_p, top_k, temp, 1);
    if (id == llama_token_eog(g_context)) return -2;
    if (!llama_eval(g_context, &id, 1, llama_n_past(g_context), 1)) {
        LOGE("Failed to eval generated token");
        return -1;
    }
    return static_cast<jint>(id);
}

extern "C" JNIEXPORT void JNICALL
Java_com_samurai_shugocore_inference_LlamaCppBridge_nativeFree(
    JNIEnv* env, jobject thiz, jlong context_ptr) {
    LOGI("nativeFree called");
    std::lock_guard<std::mutex> lock(g_mutex);
    if (g_context) {
        llama_free(g_context);
        g_context = nullptr;
    }
    if (g_model) {
        llama_model_free(g_model);
        g_model = nullptr;
    }
    llama_model_backend_free();
}