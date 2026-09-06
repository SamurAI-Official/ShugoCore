// AudioProvider.kt — v1.18 hearing: the recognizer IS the listener.
//
// The roadmap's non-negotiable flow: microphone -> STT -> transcript
// -> HumanObservation(type=speech). v1.17 gated the recognizer behind a
// hand-rolled VAD, which cost us BOTH ends of every sentence: the mic
// handoff cold-started the recognizer (clipped beginnings) and default
// endpointing closed the utterance on the first pause (clipped ends).
//
// v1.18 (VAD demoted to a signal, recognizer promoted to the listener):
//   - A persistent on-device SpeechRecognizer holds the mic continuously
//     and restarts immediately after each result/error — it is already
//     listening when the user starts talking, so nothing is lost in a
//     handoff.
//   - Endpointing hints stretch the silence windows so a mid-sentence
//     pause no longer closes the utterance.
//   - Our energy VAD survives only as the honest FALLBACK when the device
//     has no on-device recognizer (speech-detected events, no words).
//   - Partial transcripts stream into PerceptionState (LOG liveliness);
//     the journal still only ever records final transcripts.
//   - No egress: the recognizer is the platform's ON-DEVICE recognizer; if
//     the device cannot provide one, hearing degrades honestly and says so.
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
import android.os.Looper
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
        // v1.18 endpointing hints (honored by most on-device recognizers):
        // a mid-sentence pause no longer closes the utterance, and a short
        // utterance gets room to finish before the engine cuts it off.
        private const val COMPLETE_SILENCE_MS = 1_600
        private const val POSSIBLY_COMPLETE_SILENCE_MS = 900
        private const val MIN_UTTERANCE_MS = 2_500
        // Partial transcripts are logged at most once per second.
        private const val PARTIAL_LOG_MIN_INTERVAL_MS = 1_000L
        // Routine no-speech timeouts (NO_MATCH / SPEECH_TIMEOUT) restart the
        // listener with this small fixed gap — they are normal idle cycling,
        // not faults, and must NOT grow the error backoff.
        private const val IDLE_RESTART_DELAY_MS = 750L
    }

    private enum class Mode { IDLE, VAD, RECOGNIZING }

    private val thread = HandlerThread("shugocore-audio").apply { start() }
    private val handler = Handler(thread.looper)
    // SpeechRecognizer requires the MAIN thread for every call (learned on
    // device: a backoff restart posted onto the audio looper is rejected).
    private val mainHandler = Handler(Looper.getMainLooper())

    @Volatile var isRunning: Boolean = false
        private set

    private var mode = Mode.IDLE
    private var audioRecord: AudioRecord? = null
    private var recognizer: SpeechRecognizer? = null
    private var noiseFloor = NOISE_FLOOR_MIN
    private var loudStreak = 0
    private var errorBackoffMs = ERROR_BACKOFF_BASE_MS
    private var lastPartialLogMs = 0L
    private var recognizerFailed = false
    // True while a recognition session was entered via the VAD fallback
    // path (its results return the mic to the VAD loop; persistent-path
    // results simply listen again).
    private var recognitionFromVad = false

    /** Barge-in hook (wired by the service with a half-duplex gate): fired
     * on the recognizer's own speech onset (or the VAD's in fallback mode),
     * so playback can stop before it competes with the user. */
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
            // Primary: the persistent recognizer IS the listener (v1.18).
            // Fallback: VAD-only when the device has no on-device STT.
            if (!startPersistentRecognition()) startVad()
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
            PerceptionState.lastPartialTranscript = ""
            PerceptionState.micActive = false  // v1.19
            PerceptionState.voiceDetected = false
            PerceptionState.humanSpeech = false
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
            PerceptionState.micActive = true  // v1.19: we hold the mic
            val rms = kotlin.math.sqrt(
                buffer.fold(0.0) { acc, s -> acc + s.toDouble() * s } / read)
            // Adaptive noise floor: track ambient level while quiet.
            noiseFloor = 0.95 * noiseFloor + 0.05 * maxOf(rms, NOISE_FLOOR_MIN)
            val threshold = noiseFloor * THRESHOLD_MULTIPLIER + THRESHOLD_MARGIN
            if (rms > threshold) {
                loudStreak += 1
                if (loudStreak >= ONSET_FRAMES) {
                    loudStreak = 0
                    PerceptionState.voiceDetected = true  // v1.19
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

    // -- PRIMARY: persistent on-device recognition (v1.18) --------------------
    // One recognizer instance, created once on the main thread, listening
    // continuously. After each result it starts listening again AT ONCE —
    // the gap between utterances is a single startListening call, not a
    // cold init, so sentence beginnings survive.

    private fun buildIntent(): Intent =
        Intent(RecognizerIntent.ACTION_RECOGNIZE_SPEECH).apply {
            putExtra(RecognizerIntent.EXTRA_LANGUAGE_MODEL,
                RecognizerIntent.LANGUAGE_MODEL_FREE_FORM)
            putExtra(RecognizerIntent.EXTRA_MAX_RESULTS, 1)
            putExtra(RecognizerIntent.EXTRA_PARTIAL_RESULTS, true)
            putExtra(
                RecognizerIntent.EXTRA_SPEECH_INPUT_COMPLETE_SILENCE_LENGTH_MILLIS,
                COMPLETE_SILENCE_MS)
            putExtra(
                RecognizerIntent.EXTRA_SPEECH_INPUT_POSSIBLY_COMPLETE_SILENCE_LENGTH_MILLIS,
                POSSIBLY_COMPLETE_SILENCE_MS)
            putExtra(RecognizerIntent.EXTRA_SPEECH_INPUT_MINIMUM_LENGTH_MILLIS,
                MIN_UTTERANCE_MS)
        }

    /** Returns false (leaving VAD as the fallback path) when no on-device
     * recognizer is available or it already failed before. */
    private fun startPersistentRecognition(): Boolean {
        if (!isRunning || mode == Mode.RECOGNIZING) return true
        if (recognizerFailed || !SpeechRecognizer.isRecognitionAvailable(context)) {
            if (!recognizerFailed) {
                LogBus.log(LogBus.Category.SENSOR,
                    "hearing: no on-device recognizer — falling back to VAD-only",
                    isError = true)
                recognizerFailed = true
            }
            return false
        }
        ContextCompat.getMainExecutor(context).execute {
            try {
                val sr = recognizer ?: SpeechRecognizer
                    .createOnDeviceSpeechRecognizer(context).also {
                        recognizer = it
                        it.setRecognitionListener(listener)
                    }
                mode = Mode.RECOGNIZING
                sr.startListening(buildIntent())
                LogBus.log(LogBus.Category.SENSOR,
                    "hearing: on-device STT listening (continuous)")
            } catch (e: Exception) {
                LogBus.log(LogBus.Category.SENSOR,
                    "hearing: recognizer unavailable: ${e.message}",
                    isError = true)
                recognizerFailed = true
                recognizer = null
                handler.post { if (isRunning && mode != Mode.VAD) startVad() }
            }
        }
        return true
    }

    /** Listen again on the SAME instance (never a destroy/recreate cold
     * start) — immediately after results, backoff after errors. */
    private fun restartRecognition(backoffMs: Long = 0L) {
        if (!isRunning || mode != Mode.RECOGNIZING) return
        recognitionFromVad = false
        if (backoffMs > 0) {
            // Everything SpeechRecognizer touches must run on the MAIN
            // thread — including the delayed restart.
            mainHandler.postDelayed({
                if (!isRunning || mode != Mode.RECOGNIZING) return@postDelayed
                val sr = recognizer ?: return@postDelayed
                try { sr.startListening(buildIntent()) }
                catch (e: Exception) {
                    LogBus.log(LogBus.Category.SENSOR,
                        "hearing: restart failed: ${e.message}", isError = true)
                }
            }, backoffMs)
        } else {
            ContextCompat.getMainExecutor(context).execute {
                if (!isRunning || mode != Mode.RECOGNIZING) return@execute
                val sr = recognizer ?: return@execute
                try { sr.startListening(buildIntent()) }
                catch (e: Exception) {
                    LogBus.log(LogBus.Category.SENSOR,
                        "hearing: restart failed: ${e.message}", isError = true)
                }
            }
        }
    }

    // -- FALLBACK STT: VAD-triggered recognition (mic handoff, v1.14 path) ----

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
        recognitionFromVad = true
        // SpeechRecognizer must be created (and destroyed) on the main
        // thread; its callbacks arrive there, so backToVad hops the VAD
        // restart back onto the audio handler.
        ContextCompat.getMainExecutor(context).execute {
            try {
                val sr = SpeechRecognizer.createOnDeviceSpeechRecognizer(context)
                recognizer = sr
                sr.setRecognitionListener(listener)
                sr.startListening(buildIntent())
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
            PerceptionState.voiceDetected = true  // v1.19: recogniser receiving audio
        }

        override fun onBeginningOfSpeech() {
            PerceptionState.lastMicActivityMs = System.currentTimeMillis()
            PerceptionState.voiceDetected = true  // v1.19: endpointer confirms speech
            // The recognizer's own endpointer heard speech onset: signal
            // barge-in (the service half-duplex gate decides whether it
            // actually stops playback — echo suppression, v1.18).
            onSpeechOnset?.invoke()
        }

        override fun onPartialResults(partialResults: Bundle?) {
            val text = partialResults
                ?.getStringArrayList(SpeechRecognizer.RESULTS_RECOGNITION)
                ?.firstOrNull()?.trim().orEmpty()
            if (text.isEmpty()) return
            // Partials: LOG/face liveliness only — never the observation
            // bus, never the journal (only final transcripts are recorded).
            PerceptionState.lastPartialTranscript = text.take(MAX_TRANSCRIPT_LEN)
            PerceptionState.lastMicActivityMs = System.currentTimeMillis()
            // v1.19: stamp the transcription signal + humanSpeech (echo-gated).
            PerceptionState.transcription = PerceptionSignal(
                text.take(MAX_TRANSCRIPT_LEN), System.currentTimeMillis(),
                source = "on_device")
            PerceptionState.humanSpeech = true
            val now = System.currentTimeMillis()
            if (now - lastPartialLogMs >= PARTIAL_LOG_MIN_INTERVAL_MS) {
                lastPartialLogMs = now
                LogBus.log(LogBus.Category.SENSOR, "hearing: … $text")
            }
        }

        override fun onResults(results: Bundle?) {
            val text = results
                ?.getStringArrayList(SpeechRecognizer.RESULTS_RECOGNITION)
                ?.firstOrNull()?.trim().orEmpty()
            if (text.isNotEmpty()) postTranscript(text) else postSpeechDetected()
            errorBackoffMs = ERROR_BACKOFF_BASE_MS
            if (recognitionFromVad) {
                // Fallback path: hand the mic back to the VAD loop (v1.14).
                backToVad()
            } else {
                // v1.18 primary path: keep listening AT ONCE on the same
                // instance (no destroy/recreate cold start — that was the
                // clipped-beginning bug). The error path recovers if the
                // device rejects immediate reuse.
                restartRecognition()
            }
        }

        override fun onError(error: Int) {
            LogBus.log(LogBus.Category.SENSOR,
                "hearing: STT error $error", isError = true)
            if (error == SpeechRecognizer.ERROR_INSUFFICIENT_PERMISSIONS) {
                // Unrecoverable for the recognizer path: degrade honestly.
                recognizerFailed = true
                recognitionFromVad = false
                destroyRecognizer()
                mode = Mode.IDLE
                handler.post { if (isRunning) startVad() }
                return
            }
            if (error == SpeechRecognizer.ERROR_NO_MATCH ||
                error == SpeechRecognizer.ERROR_SPEECH_TIMEOUT
            ) {
                // Nobody spoke before the recognizer's idle timeout: normal
                // cycling. Log at info level (an empty room is not an error)
                // and restart promptly with a FIXED small gap — the doubled
                // backoff would leave the listener asleep most of the day,
                // and sentences would land in the gaps.
                LogBus.log(LogBus.Category.SENSOR,
                    "hearing: idle (no speech) — listening again")
                if (recognitionFromVad) backToVad(IDLE_RESTART_DELAY_MS)
                else restartRecognition(IDLE_RESTART_DELAY_MS)
                return
            }
            errorBackoffMs =
                (errorBackoffMs * 2).coerceAtMost(ERROR_BACKOFF_MAX_MS)
            if (recognitionFromVad) backToVad(errorBackoffMs)
            else restartRecognition(errorBackoffMs)
        }

        override fun onReadyForSpeech(params: Bundle?) {
            PerceptionState.lastMicActivityMs = System.currentTimeMillis()
        }
        override fun onBufferReceived(buffer: ByteArray?) {}
        override fun onEndOfSpeech() {}
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
        // v1.19: stamp the full transcription signal + humanSpeech.
        PerceptionState.transcription = PerceptionSignal(
            trimmed, System.currentTimeMillis(), source = "on_device")
        PerceptionState.humanSpeech = true
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
