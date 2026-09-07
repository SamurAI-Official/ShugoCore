// VisionProvider.kt — v1.13 camera perception (the first Vision provider).
//
// Person presence from the FRONT camera at ~1 fps, posted as
// HumanObservations (type=visual) through HumanInteractionBus — the same
// contract any future camera (robot, Quest, glasses) will use.
//
// Doctrine:
//   - Edge-triggered + heartbeat: observations post on state CHANGE and
//     every HEARTBEAT_MS while present; ~30 frames/second are analyzed
//     away by the throttle. The bus never floods.
//   - Hysteresis: person_present flips true on the first confident face,
//     false only after ABSENT_AFTER_MS of continuous absence.
//   - Zero ML dependency: android.media.FaceDetector over a downscaled
//     RGB_565 frame. Swappable behind this class for richer vision models.
//   - Honest liveness: every analyzed frame stamps PerceptionState so the
//     SENSORS tab shows CAMERA ACTIVE only while frames truly arrive.
package com.samurai.shugocore.runtime

import android.Manifest
import android.content.Context
import android.content.pm.PackageManager
import android.graphics.Bitmap
import android.graphics.Matrix
import android.media.FaceDetector
import androidx.camera.core.CameraSelector
import androidx.camera.core.ImageAnalysis
import androidx.camera.core.ImageProxy
import androidx.camera.lifecycle.ProcessCameraProvider
import androidx.core.content.ContextCompat
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.LifecycleOwner
import androidx.lifecycle.LifecycleRegistry
import org.json.JSONObject
import java.util.concurrent.Executors

class VisionProvider(private val context: Context) {

    companion object {
        private const val ANALYZE_INTERVAL_MS = 1_000L
        private const val ANALYZE_INTERVAL_ATTENTION_MS = 200L  // v1.20: higher rate for gaze tracking
        private const val ABSENT_AFTER_MS = 20_000L
        private const val HEARTBEAT_MS = 45_000L
        private const val MAX_FACES = 3
        private const val ANALYSIS_WIDTH = 320  // even: FaceDetector requires it
        private const val MIN_FACE_CONFIDENCE = 0.35f
        private const val GAZE_YAW_THRESHOLD = 20  // v1.20: degrees off-center for "gaze toward camera"
    }

    /** A permanently-RESUMED owner: the camera lives as long as the service. */
    private val lifecycleOwner = object : LifecycleOwner {
        private val registry = LifecycleRegistry(this)
        override val lifecycle: Lifecycle get() = registry
        fun resume() {
            registry.currentState = Lifecycle.State.RESUMED
        }
    }

    private val analyzerExecutor = Executors.newSingleThreadExecutor()
    private var cameraProvider: ProcessCameraProvider? = null

    @Volatile var isRunning: Boolean = false
        private set

    /** v1.20: when true, camera runs at higher frame rate for gaze tracking. */
    @Volatile var attentionMode: Boolean = false

    private var lastAnalyzeMs = 0L
    private var lastFaceMs = 0L
    private var lastHeartbeatMs = 0L
    private var personPresent = false

    @Synchronized
    fun start() {
        val granted = ContextCompat.checkSelfPermission(
            context, Manifest.permission.CAMERA) == PackageManager.PERMISSION_GRANTED
        if (!granted || isRunning) return
        isRunning = true
        // CameraX lifecycle mutations belong on the main thread; the
        // housekeeping tick calls start() from a background executor.
        ContextCompat.getMainExecutor(context).execute {
            try {
                if (!isRunning) return@execute
                lifecycleOwner.resume()
                val future = ProcessCameraProvider.getInstance(context)
                future.addListener({
                    if (!isRunning) return@addListener
                    try {
                        val provider = future.get()
                        cameraProvider = provider
                        val analysis = ImageAnalysis.Builder()
                            .setBackpressureStrategy(
                                ImageAnalysis.STRATEGY_KEEP_ONLY_LATEST)
                            .build()
                        analysis.setAnalyzer(analyzerExecutor, ::analyze)
                        provider.unbindAll()
                        provider.bindToLifecycle(
                            lifecycleOwner, CameraSelector.DEFAULT_FRONT_CAMERA,
                            analysis)
                        LogBus.log(LogBus.Category.SENSOR,
                            "vision provider: front camera bound " +
                                "(~1 fps person presence)")
                    } catch (e: Exception) {
                        LogBus.log(LogBus.Category.SENSOR,
                            "vision provider failed: ${e.message}", isError = true)
                        isRunning = false
                    }
                }, ContextCompat.getMainExecutor(context))
            } catch (e: Exception) {
                LogBus.log(LogBus.Category.SENSOR,
                    "vision provider start failed: ${e.message}", isError = true)
                isRunning = false
            }
        }
    }

    @Synchronized
    fun stop() {
        if (!isRunning && cameraProvider == null) return
        isRunning = false
        ContextCompat.getMainExecutor(context).execute {
            try {
                cameraProvider?.unbindAll()
            } catch (_: Exception) {
            }
            cameraProvider = null
            PerceptionState.lastCameraFrameMs = 0L
            PerceptionState.lastFaceCount = -1
            LogBus.log(LogBus.Category.SENSOR, "vision provider stopped")
        }
    }

    /** Sync to permission reality: called every housekeeping tick. */
    @Synchronized
    fun sync() {
        val granted = ContextCompat.checkSelfPermission(
            context, Manifest.permission.CAMERA) == PackageManager.PERMISSION_GRANTED
        if (granted && !isRunning) start()
        if (!granted && isRunning) stop()
    }

    private fun analyze(proxy: ImageProxy) {
        try {
            val now = System.currentTimeMillis()
            val interval = if (attentionMode) ANALYZE_INTERVAL_ATTENTION_MS else ANALYZE_INTERVAL_MS
            if (now - lastAnalyzeMs < interval) return
            lastAnalyzeMs = now
            val bitmap = frameToRgb565(proxy, ANALYSIS_WIDTH) ?: return
            PerceptionState.lastCameraFrameMs = now

            // v1.24: store a JPEG preview of the camera frame for the UI.
            try {
                val jpegStream = java.io.ByteArrayOutputStream()
                bitmap.compress(android.graphics.Bitmap.CompressFormat.JPEG, 60, jpegStream)
                PerceptionState.lastPreviewJpeg = jpegStream.toByteArray()
            } catch (_: Exception) { /* preview is best-effort */ }

            val faces = arrayOfNulls<android.media.FaceDetector.Face>(MAX_FACES)
            val detector = android.media.FaceDetector(
                bitmap.width, bitmap.height, MAX_FACES)
            val found = detector.findFaces(bitmap, faces)
            val faceCount = (0 until found).count {
                (faces[it]?.confidence() ?: 0f) >= MIN_FACE_CONFIDENCE
            }
            PerceptionState.lastFaceCount = faceCount

            // v1.20: gaze extraction from FaceDetector pose (yaw toward camera).
            var gazeTowardCamera = false
            if (faceCount > 0) {
                for (i in 0 until found) {
                    val f = faces[i] ?: continue
                    if (f.confidence() < MIN_FACE_CONFIDENCE) continue
                    val yaw = f.pose(android.media.FaceDetector.Face.EULER_Y)
                    gazeTowardCamera = kotlin.math.abs(yaw) < GAZE_YAW_THRESHOLD
                    break  // use the first confident face
                }
            }
            PerceptionState.gazeTowardCamera = gazeTowardCamera
            PerceptionState.visualPresence = PerceptionSignal(
                faceCount, now, source = "front_camera")

            val detected = faceCount > 0
            if (detected) {
                lastFaceMs = now
                if (!personPresent) {
                    personPresent = true
                    post(true, faceCount, gazeTowardCamera, heartbeat = false)
                } else if (now - lastHeartbeatMs > HEARTBEAT_MS) {
                    lastHeartbeatMs = now
                    post(true, faceCount, gazeTowardCamera, heartbeat = true)
                }
            } else if (personPresent && now - lastFaceMs > ABSENT_AFTER_MS) {
                personPresent = false
                post(false, 0, false, heartbeat = false)
            }
        } catch (e: Exception) {
            LogBus.log(LogBus.Category.SENSOR,
                "vision analysis failed: ${e.message}", isError = true)
        } finally {
            proxy.close()  // every proxy closes: skipped frames too
        }
    }

    private fun post(personPresent: Boolean, faceCount: Int,
                     gazeTowardCamera: Boolean = false, heartbeat: Boolean = false) {
        val payload = JSONObject()
            .put("person_present", personPresent)
            .put("face_count", faceCount)
            .put("gaze_direction", if (gazeTowardCamera) "toward_camera" else "away")
        if (heartbeat) payload.put("heartbeat", true)
        HumanInteractionBus.post("visual", "front_camera", payload)
        LogBus.log(LogBus.Category.SENSOR,
            "vision: person ${if (personPresent) "PRESENT" else "ABSENT"} " +
                "(faces=$faceCount, gaze=${if (gazeTowardCamera) "camera" else "away"})")
    }

    /** CameraX YUV -> upright, downscaled, even-width RGB_565 bitmap. */
    private fun frameToRgb565(proxy: ImageProxy, targetWidth: Int): Bitmap? {
        return try {
            var bmp = proxy.toBitmap() ?: return null
            val rotation = proxy.imageInfo.rotationDegrees
            if (rotation != 0) {
                val matrix = Matrix().apply { postRotate(rotation.toFloat()) }
                bmp = Bitmap.createBitmap(bmp, 0, 0, bmp.width, bmp.height,
                    matrix, true)
            }
            var height = (bmp.height * (targetWidth.toFloat() / bmp.width)).toInt()
            if (height % 2 == 1) height += 1
            bmp = Bitmap.createScaledBitmap(bmp, targetWidth, height, true)
            if (bmp.config != Bitmap.Config.RGB_565) {
                bmp = bmp.copy(Bitmap.Config.RGB_565, false)
            }
            bmp
        } catch (e: Exception) {
            LogBus.log(LogBus.Category.SENSOR,
                "vision frame conversion failed: ${e.message}")
            null
        }
    }
}
