// SoundBridge.kt - Kotlin JNI wrapper for the native audio-perception runtime.
package com.samurai.shugocore.inference

import android.content.Context
import android.util.Log
import java.util.concurrent.atomic.AtomicBoolean
import org.json.JSONObject

/**
 * Kotlin wrapper for native audio perception (Silero VAD + YAMNet, ONNX Runtime).
 *
 * Call chain: ShugoCoreService -> a mic frame -> [speechProbability] per 512-sample chunk,
 *             [analyze] per 15600-sample window -> PerceptionState -> the sound/ contract
 *             in Python.
 *
 * Three facts shape this class:
 *
 *  * **No audio leaves the device.** [analyze] takes a window the caller already holds and
 *    returns scores (optionally with an embedding). Nothing is uploaded or written to disk
 *    -- the same rule as NRR's rendered frames, and for a microphone it is also the privacy
 *    rule.
 *  * **A chunk is 512 samples, not 576.** The 64-sample context Silero needs, and its RNN
 *    state, live inside the native layer, so a caller owning a 16 kHz frame loop need not
 *    know either.
 *  * **Two models, two answers.** [isVadReady] and [isClassifierReady] are separate because
 *    they can be: a device with one model loaded says so, and the capability report is built
 *    from that rather than from what the app hoped for.
 */
class SoundBridge(vadModelPath: String, modelPath: String) : AutoCloseable {
    private val TAG = "SoundBridge"
    private val isInitialized = AtomicBoolean(false)
    private var sessionPtr: Long = 0

    // The native session runs one frame at a time.
    private val frameLock = Any()

    private val vadPath: String = vadModelPath
    private val yamnetPath: String = modelPath
    private var caps: Map<String, Any> = emptyMap()

    init {
        System.loadLibrary("sound_jni")
    }

    val isReady: Boolean
        get() = isInitialized.get() && sessionPtr != 0L

    /** True when the VAD loaded: speech-or-not is available even without the classifier. */
    val isVadReady: Boolean
        get() = caps["vad"] == true

    /** True when the classifier loaded: labels (and the embedding) are available. */
    val isClassifierReady: Boolean
        get() = caps["classifier"] == true

    /**
     * Create the native sessions. Returns false when neither model could load (0 handle):
     * no silent fallback, and no pretending this node can hear.
     */
    fun initialize(): Boolean {
        if (isInitialized.get()) return true
        val ptr = nativeCreate(vadPath, yamnetPath)
        if (ptr == 0L) {
            Log.e(TAG, "nativeCreate failed (vad=$vadPath model=$yamnetPath)")
            return false
        }
        sessionPtr = ptr
        caps = parseJson(nativeCapabilitiesJson(ptr))
        isInitialized.set(true)
        Log.i(TAG, "sound ready: vad=$isVadReady classifier=$isClassifierReady")
        return true
    }

    /** What the native runtime can actually do (execution provider, models, shapes). */
    fun capabilities(): Map<String, Any> = caps

    /**
     * Speech probability for one 512-sample chunk, in 0..1, or **-1** when there is no VAD.
     * A caller must never read -1 as "not speech": it means "not measured".
     */
    fun speechProbability(chunk: FloatArray): Float {
        if (!isReady || !isVadReady || chunk.size != CHUNK_SAMPLES) return -1f
        synchronized(frameLock) { return nativeSpeechProbability(sessionPtr, chunk) }
    }

    /** Drop the VAD's carried state: a gap in the audio is not context for what follows. */
    fun resetVad() {
        if (!isReady) return
        synchronized(frameLock) { nativeResetVad(sessionPtr) }
    }

    /**
     * Analyse one window of 15600 samples (0.975 s at 16 kHz).
     *
     * @param wantEmbedding when true the reply also carries the 1024-d embedding -- the one
     *        part of this payload we do not publish by default, because it is a fingerprint
     *        of what was heard rather than a label.
     * @return the parsed reply, or null when unavailable. Class *indices* are model indices:
     *         the label table lives in Python's sound.models, so this layer never ships one.
     */
    fun analyze(window: FloatArray, wantEmbedding: Boolean = false): JSONObject? {
        if (!isReady || !isClassifierReady) return null
        if (window.size != WINDOW_SAMPLES) {
            Log.w(TAG, "analyze: window is ${window.size}, expected $WINDOW_SAMPLES")
            return null
        }
        val raw = synchronized(frameLock) {
            nativeAnalyze(sessionPtr, window, wantEmbedding)
        }
        return try {
            JSONObject(raw)
        } catch (t: Throwable) {
            Log.w(TAG, "analyze: unparseable reply: $raw")
            null
        }
    }

    override fun close() {
        if (!isInitialized.getAndSet(false)) return
        val ptr = sessionPtr
        sessionPtr = 0
        caps = emptyMap()
        if (ptr != 0L) synchronized(frameLock) { nativeDestroy(ptr) }
    }

    private fun parseJson(raw: String): Map<String, Any> = try {
        val obj = JSONObject(raw)
        obj.keys().asSequence().associateWith { key -> obj.get(key) }
    } catch (t: Throwable) {
        Log.w(TAG, "could not parse native JSON: $raw")
        emptyMap()
    }

    companion object {
        const val SAMPLE_RATE = 16000
        const val CHUNK_SAMPLES = 512
        const val WINDOW_SAMPLES = 15600

        /**
         * Copy a model out of `assets` into app-private storage.
         *
         * ONNX Runtime's CreateSession needs a filesystem path and an asset is a stream
         * inside the APK, so it is copied once and reused while the asset is unchanged
         * (size check). Returns null when the asset is missing -- which the caller reports
         * rather than treating as "no sounds happened".
         */
        fun extractAssetModel(context: Context, assetPath: String): String? {
            val target = java.io.File(context.filesDir, assetPath.substringAfterLast('/'))
            val expected: Long = try {
                context.assets.openFd(assetPath).use { it.length }
            } catch (t: Throwable) {
                -1L
            }
            if (target.isFile && target.length() > 0 &&
                (expected < 0L || target.length() == expected)) {
                return target.absolutePath
            }
            return try {
                context.assets.open(assetPath).use { input ->
                    target.outputStream().use { output -> input.copyTo(output) }
                }
                target.absolutePath
            } catch (t: Throwable) {
                Log.w("SoundBridge", "could not extract asset $assetPath", t)
                null
            }
        }
    }

    // Must match the JNI exports in sound_jni.cpp: tests/test_sound_jni_contract.py fails
    // the suite if either side renames one, because a mismatch is an UnsatisfiedLinkError
    // on the first frame a device tries to hear.
    private external fun nativeCreate(vadModelPath: String, modelPath: String): Long
    private external fun nativeDestroy(handle: Long)
    private external fun nativeResetVad(handle: Long)
    private external fun nativeSpeechProbability(handle: Long, chunk: FloatArray): Float
    private external fun nativeAnalyze(handle: Long, window: FloatArray,
                                       wantEmbedding: Boolean): String
    private external fun nativeCapabilitiesJson(handle: Long): String
}
