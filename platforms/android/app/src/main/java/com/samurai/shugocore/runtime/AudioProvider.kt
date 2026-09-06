// AudioProvider.kt — v1.14 hearing: the first AudioInput provider.
//
// The roadmap's non-negotiable flow: microphone -> VAD -> STT -> transcript
// -> HumanObservation(type=speech). The VAD (energy RMS over 20 ms frames)
// gates the recognizer so ambient noise never wakes the STT engine; the
// recognizer is the platform's ON-DEVICE SpeechRecognizer (no egress — if
// the device cannot provide one, hearing degrades honestly to VAD-only
// speech-detected events and says so).
//
// Doctrine carried over from VisionProvider:
//   - Honest liveness: every mic read stamps PerceptionState so the SENSORS
//     tab shows MICROPHONE ACTIVE only while the provider truly holds the
//     mic and processes audio.
//   - Edge/utterance-triggered: one observation per recognized utterance,
//     never per audio frame. The bus never floods.
//   - Provider rule: this class posts observations; the decision core only
//     ever sees them as data.
package com.samurai.shugocore.runtime

import android.Manifest
import android.annotation.SuppressLint
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.media.AudioFormat
import android.media.AudioRecord
import android.media.MediaRecorder
import android.os.Bundle
import android.os.Handler
import android.os.HandlerThread
import android.speech.RecognitionListener
import android.speech.RecognizerIntent
import android.speech.SpeechRecognizer
import androidx.core.content.ContextCompat
import org.json.JSONObject

class AudioProvider(private val context: Context) {

    companion object {
        private const val SAMPLE_RATE = 16_000
        private const val FRAME_SAMPLES = 320          // 20 ms @ 16 kHz
        private const val ONSET_FRAMES = 3             // consecutive loud frames
        private const val NOISE_FLOOR_MIN = 300.0
        private const val THRESHOLD_MULTIPLIER = 3.0
        private const val THRESHOLD_MARGIN = 200.0
        private const val ERROR_BACKOFF_BASE_MS = 1_000L
        private const val ERROR_BACKOFF_MAX_MS = 30_000L
        private const val MAX_TRANSCRIPT_LEN = 200
    }

    private enum class Mode { IDLE, VAD, RECOGNIZING }

    private val thread = HandlerThread("shugocore-audio").apply { start() }
    private val handler = Handler(thread.looper)

    @Volatile var isRunning: Boolean = false
        private set

    private var mode = Mode.IDLE
    private var audioRecord: AudioRecord? = null
    private var recognizer: SpeechRecognizer? = null
    private var noiseFloor = NOISE_FLOOR_MIN
    private var loudStreak = 0
    private var errorBackoffMs = ERROR_BACKOFF_BASE_MS

    /** Barge-in hook (wired by the 1.15 TTS provider): fired the instant the
     * VAD detects speech onset, so playback can stop before it competes. */
    @Volatile var onSpeechOnset: (() -> Unit)? = null

    @Synchronized
    fun start() {
        val granted = ContextCompat.checkSelfPermission(
            context, Manifest.permission.RECORD_AUDIO) ==
            PackageManager.PERMISSION_GRANTED
        if (!granted || isRunning) return
        isRunning = true
        handler.post {
            mode = Mode.IDLE
            startVad()
            LogBus.log(LogBus.Category.SENSOR,
                "hearing provider: VAD listening (on-device STT gated)")
        }
    }

    @Synchronized
    fun stop() {
        if (!isRunning && mode == Mode.IDLE) return
        isRunning = false
        handler.post {
            stopVad()
            destroyRecognizer()
            mode = Mode.IDLE
            PerceptionState.lastMicActivityMs = 0L
            LogBus.log(LogBus.Category.SENSOR, "hearing provider stopped")
        }
    }

    /** Sync to permission reality: called every housekeeping tick. */
    @Synchronized
    fun sync() {
        val granted = ContextCompat.checkSelfPermission(
            context, Manifest.permission.RECORD_AUDIO) ==
            PackageManager.PERMISSION_GRANTED
        if (granted && !isRunning) start()
        if (!granted && isRunning) stop()
    }

    // -- VAD: hold the mic, watch energy, hand off on speech onset ------------

    @SuppressLint("MissingPermission")  // start() gates on RECORD_AUDIO
    private fun startVad() {
        if (!isRunning || mode == Mode.VAD) return
        val minBuf = AudioRecord.getMinBufferSize(
            SAMPLE_RATE, AudioFormat.CHANNEL_IN_MONO,
            AudioFormat.ENCODING_PCM_16BIT)
        if (minBuf <= 0) {
            LogBus.log(LogBus.Category.SENSOR,
                "hearing: AudioRecord unavailable (minBuf=$minBuf)", isError = true)
            scheduleRestartVad(5_000L)
            return
        }
        val record = try {
            AudioRecord(MediaRecorder.AudioSource.MIC, SAMPLE_RATE,
                AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT,
                maxOf(minBuf * 2, FRAME_SAMPLES * 4))
        } catch (e: Exception) {
            LogBus.log(LogBus.Category.SENSOR,
                "hearing: AudioRecord init failed: ${e.message}", isError = true)
            scheduleRestartVad(5_000L)
            return
        }
        if (record.state != AudioRecord.STATE_INITIALIZED) {
            record.release()
            scheduleRestartVad(5_000L)
            return
        }
        audioRecord = record
        mode = Mode.VAD
        record.startRecording()
        handler.post { vadLoop(record) }
    }

    private fun vadLoop(record: AudioRecord) {
        val buffer = ShortArray(FRAME_SAMPLES)
        while (isRunning && mode == Mode.VAD && audioRecord === record) {
            val read = record.read(buffer, 0, FRAME_SAMPLES)
            if (read < FRAME_SAMPLES) continue
            PerceptionState.lastMicActivityMs = System.currentTimeMillis()
            val rms = kotlin.math.sqrt(
                buffer.fold(0.0) { acc, s -> acc + s.toDouble() * s } / read)
            // Adaptive noise floor: track ambient level while quiet.
            noiseFloor = 0.95 * noiseFloor + 0.05 * maxOf(rms, NOISE_FLOOR_MIN)
            val threshold = noiseFloor * THRESHOLD_MULTIPLIER + THRESHOLD_MARGIN
            if (rms > threshold) {
                loudStreak += 1
                if (loudStreak >= ONSET_FRAMES) {
                    loudStreak = 0
                    onSpeechOnset?.invoke()
                    startRecognition()
                    return
                }
            } else {
                loudStreak = 0
            }
        }
    }

    private fun stopVad() {
        try {
            audioRecord?.stop()
        } catch (_: Exception) {
        }
        try {
            audioRecord?.release()
        } catch (_: Exception) {
        }
        audioRecord = null
    }

    private fun scheduleRestartVad(delayMs: Long) {
        mode = Mode.IDLE
        handler.postDelayed({
            if (isRunning) startVad()
        }, delayMs)
    }

    // -- STT: on-device recognition of the utterance the VAD detected ---------

    private fun startRecognition() {
        if (!isRunning || mode == Mode.RECOGNIZING) return
        stopVad()  // release the mic: the recognizer needs it exclusively
        if (!SpeechRecognizer.isRecognitionAvailable(context)) {
            LogBus.log(LogBus.Category.SENSOR,
                "hearing: no recognizer available — speech-detected only",
                isError = true)
            postSpeechDetected()
            scheduleRestartVad(10_000L)
            return
        }
        mode = Mode.RECOGNIZING
        // SpeechRecognizer must be created (and destroyed) on the main
        // thread; its callbacks arrive there, so backToVad hops the VAD
        // restart back onto the audio handler.
        ContextCompat.getMainExecutor(context).execute {
            try {
                val sr = SpeechRecognizer.createOnDeviceSpeechRecognizer(context)
                recognizer = sr
                sr.setRecognitionListener(listener)
                val intent = Intent(RecognizerIntent.ACTION_RECOGNIZE_SPEECH).apply {
                    putExtra(RecognizerIntent.EXTRA_LANGUAGE_MODEL,
                        RecognizerIntent.LANGUAGE_MODEL_FREE_FORM)
                    putExtra(RecognizerIntent.EXTRA_MAX_RESULTS, 1)
                }
                sr.startListening(intent)
            } catch (e: Exception) {
                LogBus.log(LogBus.Category.SENSOR,
                    "hearing: on-device recognizer unavailable: ${e.message}",
                    isError = true)
                postSpeechDetected()
                scheduleRestartVad(10_000L)
            }
        }
    }

    private val listener = object : RecognitionListener {
        override fun onRmsChanged(rmsdB: Float) {
            PerceptionState.lastMicActivityMs = System.currentTimeMillis()
        }

        override fun onBeginningOfSpeech() {
            PerceptionState.lastMicActivityMs = System.currentTimeMillis()
        }

        override fun onResults(results: Bundle?) {
            val text = results
                ?.getStringArrayList(SpeechRecognizer.RESULTS_RECOGNITION)
                ?.firstOrNull()?.trim().orEmpty()
            if (text.isNotEmpty()) postTranscript(text) else postSpeechDetected()
            backToVad()
        }

        override fun onError(error: Int) {
            LogBus.log(LogBus.Category.SENSOR,
                "hearing: STT error $error", isError = true)
            errorBackoffMs =
                (errorBackoffMs * 2).coerceAtMost(ERROR_BACKOFF_MAX_MS)
            backToVad(errorBackoffMs)
        }

        override fun onReadyForSpeech(params: Bundle?) {}
        override fun onBufferReceived(buffer: ByteArray?) {}
        override fun onEndOfSpeech() {}
        override fun onPartialResults(partialResults: Bundle?) {}
        override fun onEvent(eventType: Int, params: Bundle?) {}
    }

    private fun backToVad(backoffMs: Long = 0L) {
        destroyRecognizer()  // main thread: matches where it was created
        if (!isRunning) return
        handler.post {
            if (!isRunning) return@post
            if (backoffMs > 0) scheduleRestartVad(backoffMs) else startVad()
        }
    }

    private fun destroyRecognizer() {
        try {
            recognizer?.destroy()
        } catch (_: Exception) {
        }
        recognizer = null
    }

    private fun postTranscript(text: String) {
        errorBackoffMs = ERROR_BACKOFF_BASE_MS  // success resets the backoff
        val trimmed = text.take(MAX_TRANSCRIPT_LEN)
        PerceptionState.lastTranscript = trimmed
        HumanInteractionBus.post("speech", "on_device_stt",
            JSONObject()
                .put("transcript", trimmed)
                .put("stt", "on_device"))
        LogBus.log(LogBus.Category.SENSOR, "hearing: \"$trimmed\"")
    }

    /** VAD fired but the recognizer produced nothing usable: still an honest
     * speech observation (someone spoke), just without words. */
    private fun postSpeechDetected() {
        HumanInteractionBus.post("speech", "vad",
            JSONObject().put("speech_detected", true))
    }
}
