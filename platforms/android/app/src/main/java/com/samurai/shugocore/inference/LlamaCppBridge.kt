// LlamaCppBridge.kt - Kotlin JNI wrapper for llama.cpp
package com.samurai.shugocore.inference

import android.util.Log
import java.nio.IntBuffer
import java.util.concurrent.atomic.AtomicBoolean

/**
 * Kotlin wrapper for native llama.cpp inference.
 * Provides token streaming, batching, and resource management.
 */
class LlamaCppBridge(private val modelPath: String) : AutoCloseable {
    private val TAG = "LlamaCppBridge"
    private val isInitialized = AtomicBoolean(false)
    private var nativeContext: Long = 0
    
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
    
    /**
     * Initialize the native inference context.
     */
    fun initialize(): Boolean {
        if (isInitialized.get()) return true
        
        nativeContext = nativeInit(
            modelPath,
            nCtx,
            nThreads,
            nGpuLayers
        )
        
        if (nativeContext == 0L) {
            Log.e(TAG, "Failed to initialize native context")
            return false
        }
        
        isInitialized.set(true)
        Log.i(TAG, "Native context initialized: $nativeContext")
        return true
    }
    
    /**
     * Generate text completion with token streaming.
     */
    suspend fun generate(
        prompt: String,
        maxTokens: Int = 128,
        temperature: Float = 0.7f,
        topK: Int = 40,
        topP: Float = 0.9f,
        callback: CompletionCallback
    ): String {
        if (!isInitialized.get()) {
            callback.onError("Native context not initialized")
            return ""
        }
        
        val startTime = System.currentTimeMillis()
        val tokens = tokenize(prompt)
        val predictedTokens = mutableListOf<String>()
        
        // Evaluate prompt
        if (!nativeEvalPrompt(tokens.toIntArray(), tokens.size)) {
            callback.onError("Failed to evaluate prompt")
            return ""
        }
        
        // Generate tokens
        for (i in 0 until maxTokens) {
            val tokenId = nativeGenerateToken(temperature, topK, topP)
            when {
                tokenId == -2 -> break  // EOS
                tokenId == -1 -> {      // Error
                    callback.onError("Generation error at token $i")
                    break
                }
                else -> {
                    val tokenStr = decodeToken(tokenId)
                    predictedTokens.add(tokenStr)
                    callback.onToken(tokenStr)
                }
            }
        }
        
        val elapsed = System.currentTimeMillis() - startTime
        val stats = mapOf(
            "tokens_per_second" to (maxTokens.toFloat() / (elapsed / 1000f)),
            "total_tokens" to predictedTokens.size,
            "elapsed_ms" to elapsed
        )
        callback.onComplete(stats)
        
        return predictedTokens.joinToString("")
    }
    
    // --- Tokenization helpers ---
    private fun tokenize(text: String): IntArray {
        // BPE tokenization logic (simplified)
        // In production, use llama_tokenize from native side
        return IntArray(0)  // Placeholder - would call native tokenizer
    }
    
    private fun decodeToken(tokenId: Int): String {
        // Convert token ID to string
        return ""  // Placeholder - would call native detokenizer
    }
    
    // --- Native methods ---
    @JvmName("nativeInit")
    private external fun nativeInit(
        modelPath: String,
        nCtx: Int,
        nThreads: Int,
        nGpuLayers: Int
    ): Long
    
    @JvmName("nativeEvalPrompt")
    private external fun nativeEvalPrompt(tokens: IntArray, nTokens: Int): Boolean
    
    @JvmName("nativeGenerateToken")
    private external fun nativeGenerateToken(
        temperature: Float,
        topK: Int,
        topP: Float
    ): Int
    
    @JvmName("nativeFree")
    private external fun nativeFree(contextPtr: Long)
    
    override fun close() {
        if (isInitialized.compareAndSet(true, false)) {
            nativeFree(nativeContext)
            nativeContext = 0
        }
    }
}