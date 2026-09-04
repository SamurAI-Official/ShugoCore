// LlamaCppBridge.kt - Kotlin JNI wrapper for llama.cpp
package com.samurai.shugocore.inference

import android.util.Log
import java.util.concurrent.atomic.AtomicBoolean

/**
 * Kotlin wrapper for native llama.cpp inference.
 *
 * Call chain: LocalApiServer -> generate() -> JNI -> llama.cpp -> GGUF.
 * The native side owns a ShugoSession (model + context + vocab + KV state);
 * every native call here passes [sessionPtr] as its first argument.
 */
class LlamaCppBridge(private val modelPath: String) : AutoCloseable {
    private val TAG = "LlamaCppBridge"
    private val isInitialized = AtomicBoolean(false)
    private var sessionPtr: Long = 0

    // One llama.cpp context is NOT thread-safe: serialize all inference.
    private val inferLock = Any()

    // Default parameters
    var nCtx: Int = 2048
    var nThreads: Int = Runtime.getRuntime().availableProcessors()
    var nGpuLayers: Int = 0  // CPU only by default

    // Callback for streamed tokens
    interface CompletionCallback {
        fun onToken(token: String)
        fun onComplete(stats: Map<String, Any>)
        fun onError(error: String)
    }

    init {
        System.loadLibrary("llama_jni")
    }

    val isReady: Boolean
        get() = isInitialized.get() && sessionPtr != 0L

    /**
     * Initialize the native inference session (loads the GGUF model).
     */
    fun initialize(): Boolean {
        if (isInitialized.get()) return true

        sessionPtr = nativeInit(modelPath, nCtx, nThreads, nGpuLayers)

        if (sessionPtr == 0L) {
            Log.e(TAG, "Failed to initialize native session for $modelPath")
            return false
        }

        isInitialized.set(true)
        Log.i(TAG, "Native session initialized: $sessionPtr")
        return true
    }

    /**
     * Generate a completion for [prompt], streaming tokens to [callback].
     * Blocks until [maxTokens] are produced, EOG is hit, or an error occurs.
     */
    fun generate(
        prompt: String,
        maxTokens: Int = 256,
        temperature: Float = 0.7f,
        topK: Int = 40,
        topP: Float = 0.9f,
        repeatPenalty: Float = 1.1f,
        seed: Int = -1,
        callback: CompletionCallback? = null
    ): String {
        if (!isReady) {
            callback?.onError("Native session not initialized")
            return ""
        }

        synchronized(inferLock) {
            // Fresh KV state per request.
            nativeReset(sessionPtr)

            val startTime = System.currentTimeMillis()

            // Reserve room in the context for generated tokens.
            val budget = (nCtx - maxTokens).coerceAtLeast(64)
            val promptTokens = tokenize(prompt, budget)
            if (promptTokens.isEmpty()) {
                callback?.onError("Prompt too long: exceeds $budget token budget")
                return ""
            }

            if (!nativeEvalPrompt(sessionPtr, promptTokens, promptTokens.size)) {
                callback?.onError("Failed to evaluate prompt (${promptTokens.size} tokens)")
                return ""
            }

            val output = StringBuilder()
            for (i in 0 until maxTokens) {
                // -2 = end-of-generation, -1 = native error
                val tokenId = nativeGenerateToken(
                    sessionPtr, temperature, topK, topP,
                    /*repeatLastN=*/64, repeatPenalty, seed
                )
                when {
                    tokenId == -2 -> break
                    tokenId == -1 -> {
                        callback?.onError("Generation error at token $i")
                        break
                    }
                    else -> {
                        val piece = decodeToken(tokenId)
                        if (piece.isNotEmpty()) {
                            output.append(piece)
                            callback?.onToken(piece)
                        }
                    }
                }
            }

            // Flush any partial multi-byte character held by the UTF-8 buffer.
            val tail = nativeDrain(sessionPtr)
            if (tail.isNotEmpty()) {
                output.append(tail)
                callback?.onToken(tail)
            }

            val elapsed = System.currentTimeMillis() - startTime
            val produced = output.length
            val stats = mapOf(
                "tokens_per_second" to if (elapsed > 0) (maxTokens.toFloat() / (elapsed / 1000f)) else 0f,
                "total_tokens" to maxTokens,
                "chars" to produced,
                "elapsed_ms" to elapsed
            )
            callback?.onComplete(stats)

            return output.toString()
        }
    }

        // --- Tokenization helpers ---
    private fun tokenize(text: String, maxTokens: Int): IntArray {
        val tokenBuffer = IntArray(maxTokens)
        val nTokens = nativeTokenize(sessionPtr, text, tokenBuffer, maxTokens, /*addSpecial=*/true)
        if (nTokens <= 0) return IntArray(0)
        return tokenBuffer.copyOfRange(0, nTokens)
    }

    private fun decodeToken(tokenId: Int): String {
        return nativeDetokenize(sessionPtr, tokenId)
    }

    // --- Native methods (all take the session pointer first) ---
    private external fun nativeInit(
        modelPath: String,
        nCtx: Int,
        nThreads: Int,
        nGpuLayers: Int
    ): Long

    private external fun nativeTokenize(
        sessionPtr: Long,
        text: String,
        outTokens: IntArray,
        maxTokens: Int,
        addSpecial: Boolean
    ): Int

    private external fun nativeDetokenize(sessionPtr: Long, tokenId: Int): String

    private external fun nativeDrain(sessionPtr: Long): String

    private external fun nativeEvalPrompt(sessionPtr: Long, tokens: IntArray, nTokens: Int): Boolean

    private external fun nativeGenerateToken(
        sessionPtr: Long,
        temperature: Float,
        topK: Int,
        topP: Float,
        repeatLastN: Int,
        repeatPenalty: Float,
        seed: Int
    ): Int

    private external fun nativeReset(sessionPtr: Long)

    private external fun nativeFree(sessionPtr: Long)

    override fun close() {
        if (isInitialized.compareAndSet(true, false)) {
            nativeFree(sessionPtr)
            sessionPtr = 0
        }
    }
}
