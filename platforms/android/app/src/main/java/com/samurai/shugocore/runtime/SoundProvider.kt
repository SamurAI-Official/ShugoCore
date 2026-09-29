// SoundProvider.kt — v1.30.24 hearing beyond speech: what the room sounds like.
//
// AudioProvider listens for WORDS. This listens to the room: level, onsets, and — when the
// models are present — what the sound was. Both need the microphone, and Android gives
// audio to one capture at a time, so this provider is a microphone *citizen*, not a second
// listener:
//
//  * It holds the mic only when [PerceptionState.micOwner] is free. While the speech
//    pipeline owns it, this provider does nothing at all -- no capture, no summary --
//    because a second AudioRecord would read silence and we would then publish a quiet room
//    that was never measured.
//  * It announces ownership while listening and releases it on stop, so the SENSORS tab and
//    the agent both see who is listening: "sound" is `available`, "speech" `busy_speech`.
//  * [listenMode] carries the contract's mode vocabulary (`sound`, `speech`, `off`); `sound`
//    is the default because this is the provider that has to wait its turn.
//
// Nothing leaves the device: frames go to the native bridge in-process, and what is
// published is numbers and label *indices*.
package com.samurai.shugocore.runtime

import android.Manifest
import android.annotation.SuppressLint
import android.content.Context
import android.content.pm.PackageManager
import android.media.AudioFormat
import android.media.AudioRecord
import android.media.MediaRecorder
import android.os.Handler
import android.os.HandlerThread
import android.util.Log
import androidx.core.content.ContextCompat
import com.samurai.shugocore.inference.SoundBridge
import kotlin.math.sqrt
import org.json.JSONArray
import org.json.JSONObject

/** RMS below which a room counts as silent on a phone mic (about -42 dBFS). */
private const val NOISE_FLOOR_RMS = 0.008

class SoundProvider(
    private val context: Context,
    private var bridge: SoundBridge?,
) {
    companion object {
        private const val TAG = "SoundProvider"
        private const val SAMPLE_RATE = SoundBridge.SAMPLE_RATE
        private const val CHUNK_SAMPLES = SoundBridge.CHUNK_SAMPLES
        private const val WINDOW_SAMPLES = SoundBridge.WINDOW_SAMPLES
        private const val FRAME_MS = 32                // a frame is the VAD's chunk
        private const val SOMETHING_HEARD = 0.35f      // VAD probability worth classifying
        private const val ANALYSIS_MIN_GAP_MS = 1000L  // one window per second, at most
        private const val MAX_FRAMES = 32              // bounded per-window frame stats
    }

    /** "sound" (default), "speech" (never capture) or "off". */
    @Volatile var listenMode: String = "sound"

    private var thread: HandlerThread? = null
    private var handler: Handler? = null
    private var record: AudioRecord? = null
    @Volatile private var running = false

    private val window = FloatArray(WINDOW_SAMPLES)
    private var windowFill = 0
    private val frames = ArrayList<JSONObject>(MAX_FRAMES)
    private var loudestProb = 0f
    private var lastAnalysisMs = 0L

    val isRunning: Boolean get() = running
    val holdsMic: Boolean get() = running && PerceptionState.micOwner == "sound"

    private fun hasPermission(): Boolean =
        ContextCompat.checkSelfPermission(context, Manifest.permission.RECORD_AUDIO) ==
            PackageManager.PERMISSION_GRANTED

    /**
     * Start listening, if allowed to: mode `sound`, the speech pipeline not owning the mic,
     * the runtime ready, the permission granted. Every refusal is logged with its reason --
     * "we are not listening" and "the room is quiet" must never be confused.
     */
    @SuppressLint("MissingPermission")
    fun start(): Boolean {
        if (running) return true
        if (listenMode != "sound") {
            Log.i(TAG, "not listening: listenMode=$listenMode")
            return false
        }
        if (PerceptionState.micOwner == "speech") {
            Log.i(TAG, "not listening: the speech pipeline holds the microphone")
            return false
        }
        val snd = bridge
        if (snd == null || !snd.isReady) {
            Log.i(TAG, "not listening: no sound runtime (models not loaded)")
            return false
        }
        if (!hasPermission()) {
            Log.i(TAG, "not listening: RECORD_AUDIO is not granted")
            return false
        }
        val minBuffer = AudioRecord.getMinBufferSize(
            SAMPLE_RATE, AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT)
        val recorder = try {
            AudioRecord(MediaRecorder.AudioSource.MIC, SAMPLE_RATE,
                        AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT,
                        maxOf(minBuffer, CHUNK_SAMPLES * 4))
        } catch (t: Throwable) {
            Log.w(TAG, "AudioRecord construction failed: ${t.message}")
            return false
        }
        if (recorder.state != AudioRecord.STATE_INITIALIZED) {
            Log.w(TAG, "AudioRecord not initialized; not listening")
            recorder.release()
            return false
        }
        record = recorder
        running = true
        windowFill = 0
        frames.clear()
        loudestProb = 0f
        lastAnalysisMs = 0L
        // Announce before recording: a reader must never see a frame of audio that arrived
        // while the owner still said "none".
        PerceptionState.micOwner = "sound"
        PerceptionState.micActive = true
        try {
            recorder.startRecording()
        } catch (t: Throwable) {
            Log.w(TAG, "startRecording failed: ${t.message}")
            running = false
            PerceptionState.micOwner = "none"
            PerceptionState.micActive = false
            recorder.release()
            record = null
            return false
        }
        val worker = HandlerThread("sound-capture").also { it.start() }
        thread = worker
        handler = Handler(worker.looper).also { it.post { captureLoop() } }
        Log.i(TAG, "listening for sound (mode=$listenMode)")
        return true
    }

    fun stop() {
        if (!running && record == null) return
        running = false
        try { record?.stop() } catch (t: Throwable) { /* already stopped */ }
        try { record?.release() } catch (t: Throwable) { /* already released */ }
        record = null
        thread?.quitSafely()
        thread = null
        handler = null
        if (PerceptionState.micOwner == "sound") PerceptionState.micOwner = "none"
        PerceptionState.micActive = false
        Log.i(TAG, "stopped listening for sound")
    }

    /**
     * Follow reality: mode, permission, runtime and arbiter can all change while we run. Called
     * on the service's housekeeping tick, which is how this provider gets its turn at the mic
     * once the speech pipeline lets go of it.
     */
    fun sync() {
        val wanted = listenMode == "sound" && hasPermission() &&
            bridge?.isReady == true && PerceptionState.micOwner != "speech"
        if (wanted && !running) start() else if (!wanted && running) stop()
    }

    private var maxRms = 0.0
    private var baselineRms = 0.0

    /** Read chunks, ask the VAD, roll the window. Runs on its own HandlerThread. */
    private fun captureLoop() {
        val recorder = record ?: return
        val chunk = ShortArray(CHUNK_SAMPLES)
        val floats = FloatArray(CHUNK_SAMPLES)
        Log.i(TAG, "capture loop started (${SAMPLE_RATE} Hz, ${CHUNK_SAMPLES}-sample chunks)")
        while (running) {
            val read = try {
                recorder.read(chunk, 0, chunk.size)
            } catch (t: Throwable) {
                Log.w(TAG, "read failed: ${t.message}")
                -1
            }
            if (read <= 0) {
                if (read < 0) {
                    // A dead capture must not keep claiming the microphone.
                    Log.w(TAG, "read error $read; stopping")
                    break
                }
                continue
            }
            var sum = 0.0
            for (i in 0 until read) {
                floats[i] = chunk[i] / 32768f
                sum += floats[i].toDouble() * floats[i]
            }
            val rms = sqrt(sum / read)
            val prob = bridge?.speechProbability(floats) ?: -1f
            val now = System.currentTimeMillis()
            PerceptionState.lastMicActivityMs = now
            if (prob >= 0f) {
                PerceptionState.speechProbability = PerceptionSignal(prob, now)
                PerceptionState.voiceDetected = prob >= SOMETHING_HEARD
                if (prob > loudestProb) loudestProb = prob
            }
            if (rms > maxRms) maxRms = rms
            if (frames.size < MAX_FRAMES) {
                frames.add(JSONObject().put("rms", rms).put("speech_prob",
                    if (prob >= 0f) prob.toDouble() else JSONObject.NULL))
            }
            rollWindow(floats, read)
            // Classify only when the window is full, something was heard, and the last
            // analysis is over a second old. This gate is the power budget: silence costs one
            // VAD per 32 ms and no classifier at all.
            val gapOk = lastAnalysisMs == 0L || now - lastAnalysisMs >= ANALYSIS_MIN_GAP_MS
            if (windowFill == WINDOW_SAMPLES && gapOk && worthClassifying()) analyze(now)
        }
        Log.i(TAG, "capture loop ended")
    }

    /** Newest [read] samples win; the rest of the window slides left, as a stream does. */
    private fun rollWindow(floats: FloatArray, read: Int) {
        if (read >= WINDOW_SAMPLES) {
            System.arraycopy(floats, read - WINDOW_SAMPLES, window, 0, WINDOW_SAMPLES)
            windowFill = WINDOW_SAMPLES
            return
        }
        System.arraycopy(window, read, window, 0, WINDOW_SAMPLES - read)
        System.arraycopy(floats, 0, window, WINDOW_SAMPLES - read, read)
        windowFill = minOf(WINDOW_SAMPLES, windowFill + read)
    }

    /**
     * Speech counts, and so does a loud burst in a quiet room: a door, a bark, a dropped mug is
     * what the contract's onsets are for, and a VAD-only gate would throw it away. [baselineRms]
     * tracks the room, so "loud" means loud *here*.
     */
    private fun worthClassifying(): Boolean =
        loudestProb >= SOMETHING_HEARD ||
            maxRms >= maxOf(baselineRms * 4.0, NOISE_FLOOR_RMS)

    /** Classify the window and publish what was heard. Never invents a label. */
    private fun analyze(now: Long) {
        val payload = JSONObject()
            .put("status", "no_model")
            .put("frame_ms", FRAME_MS)
            .put("frames", JSONArray(frames.toList()))
            .put("labels", JSONArray())
            .put("speech_prob", loudestProb.toDouble())
            .put("window_ms", Math.round(windowFill * 1000.0 / SAMPLE_RATE).toInt())
        val snd = bridge
        if (snd != null) {
            val reply = try { snd.analyze(window, false) } catch (t: Throwable) {
                Log.w(TAG, "analyze failed: ${t.message}")
                null
            }
            if (reply != null) {
                payload.put("status", reply.optString("status", "no_model"))
                val labels = JSONArray()
                val top = reply.optJSONArray("top")
                for (i in 0 until (top?.length() ?: 0)) {
                    val entry = top!!.optJSONObject(i) ?: continue
                    labels.put(JSONObject().put("index", entry.optInt("index"))
                        .put("score", entry.optDouble("score")))
                }
                payload.put("labels", labels)
            }
        }
        PerceptionState.soundEvent = PerceptionSignal(payload.toString(), now)
        // The room's own baseline: quiet windows teach it, loud ones must not distort it.
        if (maxRms <= maxOf(baselineRms * 4.0, NOISE_FLOOR_RMS)) {
            baselineRms = if (baselineRms <= 0.0) maxRms else baselineRms * 0.7 + maxRms * 0.3
        }
        Log.i(TAG, "heard: ${payload.optString("status")}, " +
            "${payload.optJSONArray("labels")?.length() ?: 0} labels, " +
            "speech=%.2f, rms=%.4f".format(loudestProb, maxRms))
        frames.clear()
        loudestProb = 0f
        maxRms = 0.0
        lastAnalysisMs = now
    }
}
