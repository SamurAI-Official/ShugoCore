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
import android.hardware.camera2.CaptureRequest
import androidx.camera.camera2.interop.Camera2CameraControl
import androidx.camera.camera2.interop.CaptureRequestOptions
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

    /**
     * Presence on a timeline: the last face count logged and when.
     *
     * Face presence has always lived in PerceptionState for the UI and the agent, but a
     * correlation between what a device *hears* and what it *sees* needs a series, and logcat is
     * the only series a host can read. One line per change, plus a slow heartbeat so a steady
     * count is still visible, at the existing ~1 fps analysis rate.
     */
    private var lastLoggedFaces = -2
    private var lastPresenceLogMs = 0L
    /** Whether the last logged frame was too dark to see anything in. */
    private var lastLoggedDark = false
    /** Verdict of the last logged frame, so a change to "motion" gets logged. */
    private var lastLoggedVerdict = ""

    companion object {

        /**
         * Below this mean luma the camera is effectively blind, so faces=0 means "cannot see"
         * rather than "nobody there" -- the same distinction the sound contract draws between a
         * room nobody measured and a quiet one.
         */
        private const val DARK_LUMA_MAX = 12
        private const val ANALYZE_INTERVAL_MS = 1_000L
        private const val ANALYZE_INTERVAL_ATTENTION_MS = 200L  // v1.20: higher rate for gaze tracking
        private const val ABSENT_AFTER_MS = 20_000L
        private const val HEARTBEAT_MS = 45_000L
        private const val MAX_FACES = 3
        /**
         * Width of the analysed frame at NRR's `max_resolution_scale` (1.00).
         * Even, because FaceDetector requires it.
         *
         * NRR's power manager may ask for a smaller share of this under thermal
         * or battery pressure; see [applyAdvisedResolutionScale].
         */
        private const val BASE_ANALYSIS_WIDTH = 320
        /** NRR's `min_resolution_scale` (0.50) of [BASE_ANALYSIS_WIDTH]. Even too. */
        private const val MIN_ANALYSIS_WIDTH = 160
        private const val MIN_FACE_CONFIDENCE = 0.35f
        /**
         * Contrast gain applied to a COPY of the frame for a second detection
         * pass when the frame is dim enough that contrast is the limiting factor.
         * Detection-side only: the published NRR frame is the original bitmap.
         */
        private const val CONTRAST_GAIN = 3
        /** Above this mean luma the raw pass is left to speak for itself. */
        private const val STRETCH_LUMA_MAX = 48
        /**
         * Mean absolute luma difference between consecutive analysed frames that
         * counts as movement.
         *
         * Motion is the one presence signal the dark does not take away -- a
         * person in an unlit room still changes pixels -- so it is measured
         * whether or not faces are visible. The floor is provisional until a
         * staged dark run sets it from data; the measured value is logged either
         * way, so the run has something to set it from.
         */
        private const val MOTION_MIN = 1.5
        private const val GAZE_YAW_THRESHOLD = 20  // v1.20: degrees off-center for "gaze toward camera"
    }

    /** One-shot logcat flags: the NRR frame capture is best-effort and silent
     * on success, so log only the first of each outcome. */
    private val frameLogged = java.util.concurrent.atomic.AtomicBoolean(false)
    private val frameErrorLogged = java.util.concurrent.atomic.AtomicBoolean(false)
    private val analyzeLogged = java.util.concurrent.atomic.AtomicBoolean(false)
    private val frameConvertFailedLogged =
        java.util.concurrent.atomic.AtomicBoolean(false)
    private val analyzeErrorLogged = java.util.concurrent.atomic.AtomicBoolean(false)
    /** Set on the first analysed frame; read by the no-frames watchdog. */
    private val firstFrameSeen = java.util.concurrent.atomic.AtomicBoolean(false)
    private val WATCHDOG_MS = 20_000L
    private val probeHandler =
        android.os.Handler(android.os.Looper.getMainLooper())

    @Volatile private var boundCamera: androidx.camera.core.Camera? = null

    /**
     * Human-readable reason the camera is not delivering frames, or "" when it
     * is fine (or too early to tell).
     *
     * The provider calls `bindToLifecycle()` successfully even when the HAL
     * then refuses the device ("Camera 1 disabled by policy" on this hardware),
     * so binding success is NOT evidence that perception works. The UI reads
     * this so a never-delivering camera is reported instead of showing
     * "Granted · Idle" forever.
     */
    @Volatile var cameraFault: String = ""
        private set

    /** True when at least one frame has been analysed. */
    val hasFrames: Boolean get() = firstFrameSeen.get()


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

    /**
     * Width of the analysed frame -- and therefore of the RGBA frame published
     * for NRR, which is the same bitmap.
     *
     * NRR's power manager owns this policy (see [applyAdvisedResolutionScale]),
     * so it is state rather than a constant: a thermal throttle has to reach the
     * analyser, or the app would keep feeding full-size frames while NRR asks
     * for smaller ones.
     */
    @Volatile private var analysisWidth: Int = BASE_ANALYSIS_WIDTH

    /**
     * Mean luma at or below which this device is judged blind, so faces=0 means
     * "cannot see" rather than "nobody there".
     *
     * Overridable because the threshold is a property of the sensor and its
     * exposure, not of this code: [DARK_LUMA_MAX] is where a calibration run
     * starts, not where it ends.
     */
    @Volatile private var darkLumaMax: Int = DARK_LUMA_MAX

    /** One-shot: whether the dark threshold has been logged yet. */
    private val darkThresholdLogged = java.util.concurrent.atomic.AtomicBoolean(false)

    /** Camera2 AE compensation steps; 0 means "leave exposure to the camera". */
    @Volatile private var exposureSteps: Int = 0

    /** One-shot: whether the exposure decision has been logged yet. */
    private val exposureLogged = java.util.concurrent.atomic.AtomicBoolean(false)

    /** When true, log one line per analysed frame instead of one per change. */
    @Volatile var calibrationMode: Boolean = false

    /** Sampled luma of the previous analysed frame, for the motion measure. */
    private var previousLumaSamples: IntArray? = null

    /**
     * Set the mean-luma threshold below which this device is judged blind.
     * `null` restores the built-in default. Logged on every change, because a
     * threshold nobody can see in the log is not a calibrated one.
     */
    fun applyDarkLumaMax(value: Int?) {
        val applied = (value ?: DARK_LUMA_MAX).coerceIn(0, 128)
        val changed = applied != darkLumaMax
        darkLumaMax = applied
        if (changed || darkThresholdLogged.compareAndSet(false, true)) {
            android.util.Log.i("VisionProvider",
                "dark threshold=$applied luma" +
                " (raw above $STRETCH_LUMA_MAX is trusted as-is; stretch gain" +
                " $CONTRAST_GAIN; motion floor $MOTION_MIN)" +
                (if (value == null) " [default]" else ""))
        }
    }

    /**
     * Low-light exposure compensation, in Camera2 AE steps (0 = off, which is
     * the default). Applied to the bound camera, and remembered so a camera bound
     * later still gets it.
     *
     * This is the one night-vision lever that changes what NRR renders: the
     * analysed frame IS the frame published for NRR, so raising exposure moves the
     * pixels NRR sees, not just the ones the detector sees. That is why it is a
     * preference rather than a policy, why every change is logged, and why the
     * evidence of what it did is the NRR camera probe's distinct-byte count
     * compared against a recording -- not an assumption that it only helped.
     *
     * Detection-side processing (the stretch) needs no such disclaimer: it cannot
     * reach the published frame at all.
     */
    fun applyExposureCompensation(steps: Int?) {
        val applied = (steps ?: 0).coerceIn(-12, 12)
        val changed = applied != exposureSteps
        exposureSteps = applied
        applyExposureToCamera()
        if (changed || exposureLogged.compareAndSet(false, true)) {
            android.util.Log.i("VisionProvider",
                if (applied == 0)
                    "low-light exposure: off (AE auto; NRR frames unchanged)"
                else
                    "low-light exposure: AE compensation=$applied steps" +
                    " (NRR renders this frame too, so its pixels change" +
                    " -- compare the NRR camera probe's distinct bytes)")
        }
    }

    /**
     * Push the current exposure setting to the bound camera, if there is one.
     *
     * Uses the Camera2 interop rather than rebuilding the analysis use case, so
     * the setting can change without a rebind. Fail-open: a device that refuses
     * the option keeps auto exposure and the log says nothing changed.
     */
    private fun applyExposureToCamera() {
        val camera = boundCamera ?: return
        try {
            val control = Camera2CameraControl.from(camera.cameraControl)
            if (exposureSteps == 0) {
                control.clearCaptureRequestOptions()
            } else {
                control.setCaptureRequestOptions(
                    CaptureRequestOptions.Builder()
                        .setCaptureRequestOption(
                            CaptureRequest.CONTROL_AE_EXPOSURE_COMPENSATION,
                            exposureSteps)
                        .build())
            }
        } catch (t: Throwable) {
            android.util.Log.w("VisionProvider",
                "low-light exposure not applied: ${t.message}")
        }
    }

    /** One-shot: whether the width decision has been logged yet. */
    private val widthLogged = java.util.concurrent.atomic.AtomicBoolean(false)

    /**
     * Follow NRR's advised resolution scale for the analysed frame.
     *
     * The scale maps onto the width over NRR's own declared range: its
     * `max_resolution_scale` (1.00) is [BASE_ANALYSIS_WIDTH] and its
     * `min_resolution_scale` (0.50) is [MIN_ANALYSIS_WIDTH], so the whole range
     * of advice maps onto [MIN_ANALYSIS_WIDTH]..[BASE_ANALYSIS_WIDTH] and the
     * frame only shrinks when NRR itself asks. The width stays even, because
     * FaceDetector requires it.
     *
     * `scale` is null when NRR offers no trustworthy advice (its power-manager
     * inputs are stubs on this platform -- see `NRRBridge.advisedResolutionScale`)
     * or when NRR is not available at all. The base width stands in that case,
     * and the decision is logged either way: a frame-geometry change must never
     * be silent, because the same geometry is what NRR renders.
     */
    fun applyAdvisedResolutionScale(scale: Double?) {
        val advised = (scale ?: 1.0).coerceIn(0.5, 1.0)
        val width = (((BASE_ANALYSIS_WIDTH * advised).toInt() / 2) * 2)
            .coerceIn(MIN_ANALYSIS_WIDTH, BASE_ANALYSIS_WIDTH)
        val previous = analysisWidth
        if (width != previous) {
            analysisWidth = width
            android.util.Log.i("VisionProvider",
                "analysis width $previous -> $width " +
                "(NRR advised resolution_scale=${"%.2f".format(advised)})")
        } else if (widthLogged.compareAndSet(false, true)) {
            android.util.Log.i("VisionProvider",
                "analysis width=$width (NRR advised resolution_scale=" +
                "${"%.2f".format(advised)}; range " +
                "$MIN_ANALYSIS_WIDTH..$BASE_ANALYSIS_WIDTH)")
        }
    }

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
        // New session: re-evaluate frame delivery and clear any prior fault, so
        // the watchdog judges this attempt rather than the last one.
        firstFrameSeen.set(false)
        cameraFault = ""
        PerceptionState.cameraFault = ""
        PerceptionState.visionHasFrames = false
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
                        val camera = provider.bindToLifecycle(
                            lifecycleOwner, CameraSelector.DEFAULT_FRONT_CAMERA,
                            analysis)
                        boundCamera = camera
                        // A camera bound after the setting arrived still gets it.
                        applyExposureToCamera()
                        LogBus.log(LogBus.Category.SENSOR,
                            "vision provider: front camera bound " +
                                "(~1 fps person presence)")
                        android.util.Log.i("VisionProvider",
                            "front camera bound (~1 fps)")
                        // Watchdog: binding "succeeding" does NOT mean frames
                        // arrive. On this hardware the front camera is refused
                        // asynchronously ("Camera 1 disabled by policy"), so the
                        // provider used to sit silently at zero frames forever.
                        // Surface that once, with the CameraX error code.
                        probeHandler.postDelayed({
                            // Guard on isRunning: a watchdog posted before
                            // stop() must not resurrect a fault after the
                            // camera was intentionally shut down.
                            if (isRunning && !firstFrameSeen.get()) {
                                @Suppress("UNCHECKED_CAST")
                                val st = boundCamera?.cameraInfo
                                    ?.cameraState?.value
                                cameraFault = "camera not delivering frames " +
                                    "(state=${st?.type}, error=${st?.error?.code})"
                                PerceptionState.cameraFault = cameraFault
                                android.util.Log.w("VisionProvider",
                                    "no camera frames after ${WATCHDOG_MS / 1000}s " +
                                    "($cameraFault) - vision + NRR camera " +
                                    "frames unavailable")
                            }
                        }, WATCHDOG_MS)
                    } catch (e: Exception) {
                        LogBus.log(LogBus.Category.SENSOR,
                            "vision provider failed: ${e.message}", isError = true)
                        android.util.Log.w("VisionProvider",
                            "camera bind failed: ${e.message}")
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
            // A stopped camera is not a faulty one: clear the fault so the UI
            // does not show a stale "not delivering frames" note.
            cameraFault = ""
            PerceptionState.cameraFault = ""
            PerceptionState.visionHasFrames = false
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
            firstFrameSeen.set(true)
            cameraFault = ""
            PerceptionState.cameraFault = ""
            PerceptionState.visionHasFrames = true
            if (analyzeLogged.compareAndSet(false, true)) {
                android.util.Log.i("VisionProvider",
                    "first frame analysed (${proxy.width}x${proxy.height}, " +
                    "rotation=${proxy.imageInfo.rotationDegrees})")
            }
            val bitmap = frameToRgb565(proxy, analysisWidth)
            if (bitmap == null) {
                if (frameConvertFailedLogged.compareAndSet(false, true)) {
                    android.util.Log.w("VisionProvider",
                        "frame conversion returned null (format=" +
                        "${proxy.format}, ${proxy.width}x${proxy.height})")
                }
                return
            }
            PerceptionState.lastCameraFrameMs = now

            // v1.29: publish the same frame as RGBA8 for the NRR render path.
            // Reuses the bitmap already decoded for face detection, so the only
            // added cost is one getPixels per ANALYSED frame (throttled above),
            // not per camera frame. Best-effort: a failure here must never
            // break vision. First success/failure is logged to logcat (once
            // each) because the NRR camera probe depends on this stamp.
            try {
                val w = bitmap.width
                val h = bitmap.height
                val pixels = IntArray(w * h)
                bitmap.getPixels(pixels, 0, w, 0, 0, w, h)
                val rgba = ByteArray(w * h * 4)
                for (i in pixels.indices) {
                    val c = pixels[i]
                    val o = i * 4
                    rgba[o] = ((c shr 16) and 0xFF).toByte()      // R
                    rgba[o + 1] = ((c shr 8) and 0xFF).toByte()   // G
                    rgba[o + 2] = (c and 0xFF).toByte()           // B
                    rgba[o + 3] = 0xFF.toByte()                   // A (opaque)
                }
                PerceptionState.stampFrameRgba(w, h, rgba)
                if (frameLogged.compareAndSet(false, true)) {
                    android.util.Log.i("VisionProvider",
                        "first camera frame published for NRR: ${w}x$h")
                }
            } catch (e: Exception) {
                if (frameErrorLogged.compareAndSet(false, true)) {
                    android.util.Log.w("VisionProvider",
                        "NRR frame capture failed: ${e.message}")
                }
            }

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
            // Mean luma, sampled every 16th pixel: enough at ~1 fps, and it is the only way an
            // unlit room can be told from an empty one. The face detector needs visible features,
            // so without this "faces=0" in the dark reads as "nobody there" -- a claim the camera
            // is in no position to make.
            //
            // The same samples give the motion measure, which is the presence signal the dark
            // does NOT take away: a person in an unlit room still changes pixels. Both are read
            // from the frame as published, so neither is affected by the detection-side contrast
            // gain below.
            var lumaSum = 0L
            var lumaCount = 0
            var motionSum = 0L
            var motionCount = 0
            var cursor = 0
            val pixelCount = bitmap.width * bitmap.height
            val samples = IntArray((pixelCount + 15) / 16)
            val previousSamples = previousLumaSamples
            while (cursor < pixelCount) {
                val pixel = bitmap.getPixel(cursor % bitmap.width, cursor / bitmap.width)
                val sample = (((pixel shr 16) and 0xFF) + ((pixel shr 8) and 0xFF)
                    + (pixel and 0xFF)) / 3
                samples[lumaCount] = sample
                lumaSum += sample
                // Only comparable at the same frame size: NRR's power manager can ask
                // for a different analysis width between frames.
                if (previousSamples != null && previousSamples.size == samples.size) {
                    motionSum += kotlin.math.abs(sample - previousSamples[lumaCount])
                    motionCount++
                }
                lumaCount++
                cursor += 16
            }
            previousLumaSamples = samples
            val luma = if (lumaCount > 0) (lumaSum / lumaCount).toInt() else -1
            val motion = if (motionCount > 0) motionSum.toDouble() / motionCount else 0.0
            val tooDark = luma in 0..darkLumaMax
            // Night vision, detection-side: a dim frame is stretched on a COPY for a
            // second detection pass. The copy is deliberate and load-bearing -- the
            // published NRR frame above IS this bitmap, so stretching it in place
            // would change what NRR renders, and NRR's power manager is the only
            // thing allowed to decide that frame's size or content.
            //
            // The result is a hint, never a presence claim: a stretched dark frame
            // can reveal a face or invent one, and only a staged run can tell those
            // apart. `raw=` and `stretched=` carry both counts so the gain is
            // measured per device rather than assumed.
            var stretchedCount = faceCount
            if (luma in 0..STRETCH_LUMA_MAX) {
                stretchedCount = stretchAndDetect(bitmap) ?: faceCount
            }
            // The presence vocabulary: what this device can actually stand behind.
            // `faces` is a claim (raw detection only, so the series means the same
            // thing it always did), `motion` is evidence without identification, and
            // a blind camera says `unavailable` instead of reporting an empty room.
            val verdict = when {
                faceCount > 0 -> "faces"
                motion >= MOTION_MIN -> "motion"
                tooDark -> "unavailable"
                else -> "none"
            }
            // The correlation series: what this device can see, when it saw it. Change-driven
            // with a heartbeat, because a device that sees nobody for ten minutes has to be
            // distinguishable both from one whose camera is dead and from one that cannot see.
            // Calibration mode logs every analysed frame instead, because the point of that run
            // is the luma series itself.
            val presenceNow = System.currentTimeMillis()
            if (calibrationMode || faceCount != lastLoggedFaces ||
                    verdict != lastLoggedVerdict || tooDark != lastLoggedDark ||
                    presenceNow - lastPresenceLogMs >= 30_000L) {
                lastLoggedFaces = faceCount
                lastLoggedVerdict = verdict
                lastLoggedDark = tooDark
                lastPresenceLogMs = presenceNow
                // The legacy tokens stay first and contiguous: fleet_correlation.py parses
                // this line, so an existing series has to keep reading. New fields are
                // appended, never inserted between them.
                android.util.Log.i("VisionProvider", "presence faces=$faceCount luma=$luma" +
                    (if (tooDark) " unavailable=too_dark" else "") +
                    " raw=$faceCount stretched=$stretchedCount" +
                    " motion=${"%.1f".format(motion)}" +
                    " width=${bitmap.width} dark=${if (tooDark) 1 else 0}" +
                    " verdict=$verdict")
            }

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
            if (analyzeErrorLogged.compareAndSet(false, true)) {
                android.util.Log.w("VisionProvider",
                    "analyse failed: ${e.message}")
            }
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

    /**
     * Count faces in a contrast-stretched COPY of [source], or null when the
     * copy could not be made or the detector threw.
     *
     * [source] is never modified. That is not tidiness: it is also the frame
     * published for NRR, and the whole point of doing night vision detection-side
     * is that NRR's input stays byte-identical. The stretch is a plain linear gain
     * about mid-grey -- the coarsest thing that can work, chosen because it has no
     * parameters to tune beyond the gain and a wrong gain can be seen in the logs
     * rather than hidden in a histogram.
     */
    private fun stretchAndDetect(source: Bitmap): Int? {
        return try {
            val copy = source.copy(Bitmap.Config.RGB_565, true) ?: return null
            val w = copy.width
            val h = copy.height
            val pixels = IntArray(w * h)
            copy.getPixels(pixels, 0, w, 0, 0, w, h)
            for (i in pixels.indices) {
                val c = pixels[i]
                val r = ((((c shr 16) and 0xFF) - 128) * CONTRAST_GAIN + 128)
                    .coerceIn(0, 255)
                val g = ((((c shr 8) and 0xFF) - 128) * CONTRAST_GAIN + 128)
                    .coerceIn(0, 255)
                val b = (((c and 0xFF) - 128) * CONTRAST_GAIN + 128).coerceIn(0, 255)
                pixels[i] = (0xFF shl 24) or (r shl 16) or (g shl 8) or b
            }
            copy.setPixels(pixels, 0, w, 0, 0, w, h)
            val faces = arrayOfNulls<android.media.FaceDetector.Face>(MAX_FACES)
            val found = android.media.FaceDetector(w, h, MAX_FACES)
                .findFaces(copy, faces)
            val count = (0 until found).count {
                (faces[it]?.confidence() ?: 0f) >= MIN_FACE_CONFIDENCE
            }
            copy.recycle()
            count
        } catch (t: Throwable) {
            null
        }
    }
}
