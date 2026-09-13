// NRRBridge.kt - Kotlin JNI wrapper for the native NRR runtime.
package com.samurai.shugocore.inference

import android.util.Log
import java.util.concurrent.atomic.AtomicBoolean
import org.json.JSONObject

/**
 * Kotlin wrapper for native NRR neural rendering (ONNX Runtime).
 *
 * Call chain: ShugoCore -> AndroidBackend/LocalApiServer -> render() -> JNI
 *             -> NRR device+model -> mobile execution kernel (CPU EP) -> RGB8.
 *
 * The native side owns a [ShugoNrrSession]; every native call passes its
 * handle as the first argument. Frames stay on the device: [render] takes
 * local RGBA8 bytes and returns rendered RGB8 bytes. Nothing is uploaded.
 *
 * [preferredBackend] must be explicit. The vendor backends report themselves
 * as selectable without probing for their GPU and Adreno outranks CPU in the
 * priority table, so upstream's auto-selection resolves to Adreno on every
 * device and then fails GPU detection (see docs/nrr_android_port.md).
 */
class NRRBridge(
    modelPath: String,
    val preferredBackend: String = "CPU",
) : AutoCloseable {
    private val TAG = "NRRBridge"
    private val isInitialized = AtomicBoolean(false)
    private var sessionPtr: Long = 0

    // The native session is single-threaded: serialize frames.
    private val renderLock = Any()

    val modelPath: String = modelPath

    init {
        System.loadLibrary("nrr_jni")
    }

    val isReady: Boolean
        get() = isInitialized.get() && sessionPtr != 0L

    /** True when a model was loaded; rendering is unavailable without one. */
    val isModelLoaded: Boolean
        get() = isReady && nativeIsModelLoaded(sessionPtr)

    /**
     * Create the native NRR device + kernel and load [modelPath].
     * Returns false when the device could not be created (no silent fallback).
     */
    fun initialize(): Boolean {
        if (isInitialized.get()) return true
        val ptr = nativeCreate(preferredBackend, modelPath)
        if (ptr == 0L) {
            Log.e(TAG, "nativeCreate failed for backend=$preferredBackend")
            return false
        }
        sessionPtr = ptr
        isInitialized.set(true)
        Log.i(TAG, "NRR ready: backend=${activeBackend()} model=${isModelLoaded}")
        return true
    }

    /** Backend actually selected by the native runtime (e.g. "CPU"). */
    fun activeBackend(): String =
        if (sessionPtr == 0L) "" else nativeBackendName(sessionPtr)

    /**
     * Capability snapshot. `execution_provider` is reported honestly: it is
     * "CPU" unless the kernel genuinely appended the NNAPI provider.
     */
    fun capabilities(): Map<String, Any> {
        if (sessionPtr == 0L) return emptyMap()
        return parseJson(nativeCapabilitiesJson(sessionPtr))
    }

    /** Battery / thermal snapshot plus the resolution scale NRR would use. */
    fun powerStatus(): Map<String, Any> {
        if (sessionPtr == 0L) return emptyMap()
        return parseJson(nativePowerStatusJson(sessionPtr))
    }

    /**
     * Render one frame locally.
     *
     * @param rgba packed RGBA8, exactly width*height*4 bytes.
     * @return packed RGB8 (width*height*3), or null on failure.
     */
    fun render(width: Int, height: Int, rgba: ByteArray): ByteArray? {
        if (!isReady) {
            Log.w(TAG, "render called before initialize()")
            return null
        }
        if (width <= 0 || height <= 0) return null
        val expected = width * height * 4
        if (rgba.size != expected) {
            Log.e(TAG, "rgba is ${rgba.size} bytes, expected $expected")
            return null
        }
        synchronized(renderLock) {
            return try {
                nativeRenderFrame(sessionPtr, width, height, rgba)
            } catch (t: Throwable) {
                Log.e(TAG, "render failed", t)
                null
            }
        }
    }

    override fun close() {
        if (sessionPtr != 0L) {
            try {
                nativeDestroy(sessionPtr)
            } catch (t: Throwable) {
                Log.w(TAG, "nativeDestroy failed", t)
            }
            sessionPtr = 0
        }
        isInitialized.set(false)
    }

    /**
     * Render a deterministic synthetic frame as a self-test.
     *
     * Renders a 16x16 gradient locally and reports what came back, so the
     * whole native path (device -> ONNX session -> kernel -> output texture)
     * can be exercised without a camera. Returns null when the bridge is not
     * ready or the render failed.
     */
    fun selfTest(): Map<String, Any>? {
        if (!isReady) return null
        val w = 16
        val h = 16
        val rgba = ByteArray(w * h * 4)
        for (y in 0 until h) {
            for (x in 0 until w) {
                val i = (y * w + x) * 4
                rgba[i] = (x * 255 / (w - 1)).toByte()
                rgba[i + 1] = (y * 255 / (h - 1)).toByte()
                rgba[i + 2] = 128.toByte()
                rgba[i + 3] = 255.toByte()
            }
        }
        val started = System.nanoTime()
        val rgb = render(w, h, rgba) ?: return null
        val elapsedMs = (System.nanoTime() - started) / 1_000_000.0
        return mapOf(
            "width" to w,
            "height" to h,
            "output_bytes" to rgb.size,
            "distinct_bytes" to rgb.toSortedSet().size,
            "render_time_ms" to elapsedMs,
        )
    }

    companion object {
        /**
         * Extract an NRR model from `assets` into app-private storage.
         *
         * ONNX Runtime's CreateSession needs a real filesystem path; an asset
         * is a compressed stream inside the APK, so it is copied once into
         * filesDir and reused while the asset is unchanged (size check).
         * Returns the path, or null when the asset is missing.
         */
        fun extractAssetModel(context: android.content.Context,
                              assetPath: String): String? {
            val target = java.io.File(context.filesDir,
                                      assetPath.substringAfterLast('/'))
            // Prefer the uncompressed length (build.gradle marks *.onnx
            // noCompress); assets.openFd() throws when an asset IS compressed,
            // so fall back to "copy when absent" rather than failing.
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
                Log.w("NRRBridge", "could not extract asset $assetPath", t)
                null
            }
        }
    }

    private fun parseJson(raw: String): Map<String, Any> = try {
        val obj = JSONObject(raw)
        obj.keys().asSequence().associateWith { key -> obj.get(key) }
    } catch (t: Throwable) {
        Log.w(TAG, "could not parse native JSON: $raw")
        emptyMap()
    }

    private external fun nativeCreate(preferredBackend: String, modelPath: String): Long
    private external fun nativeDestroy(handle: Long)
    private external fun nativeRenderFrame(
        handle: Long,
        width: Int,
        height: Int,
        rgba: ByteArray,
    ): ByteArray?
    private external fun nativeCapabilitiesJson(handle: Long): String
    private external fun nativePowerStatusJson(handle: Long): String
    private external fun nativeBackendName(handle: Long): String
    private external fun nativeIsModelLoaded(handle: Long): Boolean
}
