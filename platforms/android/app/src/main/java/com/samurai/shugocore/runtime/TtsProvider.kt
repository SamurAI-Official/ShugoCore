// TtsProvider.kt — v1.19 per-utterance state machine (hardening).
//
// v1.18's AtomicInteger pending counter had a race: stopAll() set pending=0,
// but late onDone/onError callbacks then decremented it past zero into
// negative territory, which neutered the queue-limit guard. Worse, the
// "pending <= 0 -> isSpeaking = false" branch in a late callback could
// re-arm the half-duplex barge-in gate *mid-utterance* if a new speak()
// had already started after the clear, resurfacing the self-cutoff bug
// we shipped the v1.18 half-duplex gate to fix.
//
// v1.19 fix: per-utterance identity (ConcurrentHashMap + AtomicLong seq)
// instead of a counter. Every utterance gets a unique id; onDone/onError
// do atomic map.remove(id) -- null return means already completed/cancelled
// and is a pure no-op. stopAll() drains the map; late callbacks harmlessly
// skip. isSpeaking is governed by map.isEmpty(), not a counter crossing
// zero. Idempotent by construction; no negative counter can exist.
package com.samurai.shugocore.runtime

import android.content.Context
import android.speech.tts.TextToSpeech
import android.speech.tts.UtteranceProgressListener
import java.util.Locale
import java.util.concurrent.ConcurrentHashMap
import java.util.concurrent.atomic.AtomicLong

class TtsProvider(private val context: Context) {

    companion object {
        private const val MAX_QUEUE = 3
    }

    /** Engine is initialised and ready to accept speak() calls. */
    @Volatile var isReady: Boolean = false
        private set

    /** Engine init reached the failure callback (terminal -- boot greeting
     * and callers should stop retrying). */
    @Volatile var isFailed: Boolean = false
        private set

    /** True while at least one utterance is on the speaker or queued
     * (map non-empty). */
    @Volatile var isSpeaking: Boolean = false
        private set

    private var tts: TextToSpeech? = null
    private val idSeq = AtomicLong(0)
    private val utterances = ConcurrentHashMap<String, Phase>()

    private enum class Phase { PENDING, STARTED }

    fun start() {
        if (tts != null) return
        isFailed = false
        tts = TextToSpeech(context) { status ->
            if (status == TextToSpeech.SUCCESS) {
                try { tts?.language = Locale.getDefault() } catch (_: Exception) {}
                isReady = true
                tts?.setOnUtteranceProgressListener(object : UtteranceProgressListener() {
                    override fun onStart(utteranceId: String?) {
                        val id = utteranceId ?: return
                        // Promote PENDING -> STARTED; skip if already cancelled.
                        val prev = utterances.replace(id, Phase.PENDING, Phase.STARTED)
                        if (prev == null) return  // removed before start
                        isSpeaking = true
                        PerceptionState.ttsSpeaking = true
                    }
                    override fun onDone(utteranceId: String?) {
                        val id = utteranceId ?: return
                        val removed = utterances.remove(id)
                        if (removed == null) return  // already cancelled/completed
                        if (utterances.isEmpty()) {
                            isSpeaking = false
                            PerceptionState.ttsSpeaking = false
                            PerceptionState.ttsLastEndMs =
                                android.os.SystemClock.elapsedRealtime()
                        }
                    }
                    @Deprecated("Kept for API compatibility")
                    override fun onError(utteranceId: String?) {
                        _onError(utteranceId)
                    }
                    override fun onError(utteranceId: String?, errorCode: Int) {
                        _onError(utteranceId)
                    }
                    private fun _onError(utteranceId: String?) {
                        val id = utteranceId ?: return
                        val removed = utterances.remove(id)
                        if (removed == null) return  // already cancelled/completed
                        if (utterances.isEmpty()) {
                            isSpeaking = false
                            PerceptionState.ttsSpeaking = false
                            PerceptionState.ttsLastEndMs =
                                android.os.SystemClock.elapsedRealtime()
                        }
                    }
                })
                LogBus.log(LogBus.Category.AGENT, "speech provider ready")
            } else {
                isReady = false
                isFailed = true
                LogBus.log(LogBus.Category.AGENT,
                    "speech provider unavailable (TTS init status=$status)",
                    isError = true)
            }
        }
    }

    /** Enqueue one utterance. Returns false when the provider is not ready
     * or the queue is full (the caller reports the drop honestly). */
    fun speak(text: String): Boolean {
        val engine = tts
        if (!isReady || engine == null || text.isBlank()) return false
        // Map-based queue-limit: the size check + insert is non-atomic
        // but a concurrent overshoot by at most 1 on an edge race is
        // harmless (the engine serialises QUEUE_ADD). Id uniqueness
        // is guaranteed by the monotonic idSeq.
        if (utterances.size >= MAX_QUEUE) return false
        val id = "shugo-${idSeq.incrementAndGet()}"
        utterances[id] = Phase.PENDING
        val result = engine.speak(text, TextToSpeech.QUEUE_ADD, null, id)
        if (result != TextToSpeech.SUCCESS) {
            utterances.remove(id)
            return false
        }
        return true
    }

    /** Stop playback and clear the queue immediately (called on barge-in
     * and on shutdown). Drains the utterance map so any late callback
     * sees remove() -> null -> no-op. */
    fun stopAll() {
        if (utterances.isNotEmpty()) {
            try { tts?.stop() } catch (_: Exception) {}
        }
        utterances.clear()
        isSpeaking = false
        PerceptionState.ttsSpeaking = false
        PerceptionState.ttsLastEndMs = android.os.SystemClock.elapsedRealtime()
    }

    fun shutdown() {
        stopAll()
        isReady = false
        isFailed = false
        try { tts?.shutdown() } catch (_: Exception) {}
        tts = null
    }
}
