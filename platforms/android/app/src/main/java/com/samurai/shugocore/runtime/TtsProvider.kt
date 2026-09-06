// TtsProvider.kt — v1.15 speech: the first SpeechOutput provider.
//
// Roadmap flow, other direction: agent response -> TTS -> speaker -> human.
// Doctrine carried over from AudioProvider:
//   - Honest liveness: isSpeaking is true only while an utterance is truly
//     on the speaker; isReady only after a real engine init.
//   - Bounded queue: a runaway monologue is impossible — at most MAX_QUEUE
//     utterances pending, beyond that the text is dropped and reported.
//   - Barge-in: the AudioProvider's VAD onset hook stops playback instantly,
//     so the human always wins the audio channel.
//   - Provider rule: this class is attached to the Python agent as the
//     executor of the internal `speak` action; the decision core only ever
//     proposes the action, it never imports this class.
package com.samurai.shugocore.runtime

import android.content.Context
import android.speech.tts.TextToSpeech
import android.speech.tts.UtteranceProgressListener
import java.util.Locale
import java.util.concurrent.atomic.AtomicInteger

class TtsProvider(private val context: Context) {

    companion object {
        private const val MAX_QUEUE = 3
    }

    @Volatile var isReady: Boolean = false
        private set
    @Volatile var isSpeaking: Boolean = false
        private set

    private var tts: TextToSpeech? = null
    private val pending = AtomicInteger(0)

    fun start() {
        if (tts != null) return
        tts = TextToSpeech(context) { status ->
            if (status == TextToSpeech.SUCCESS) {
                // Best-effort language; the engine default is fine if the
                // requested locale is missing (speech is still honest).
                try { tts?.language = Locale.getDefault() } catch (_: Exception) {}
                isReady = true
                tts?.setOnUtteranceProgressListener(object : UtteranceProgressListener() {
                    override fun onStart(utteranceId: String?) {
                        isSpeaking = true
                    }
                    override fun onDone(utteranceId: String?) {
                        isSpeaking = pending.get() > 0
                        pending.decrementAndGet()
                        if (pending.get() <= 0) isSpeaking = false
                    }
                    @Deprecated("Kept for API compatibility")
                    override fun onError(utteranceId: String?) {
                        pending.decrementAndGet()
                        if (pending.get() <= 0) isSpeaking = false
                    }
                    override fun onError(utteranceId: String?, errorCode: Int) {
                        pending.decrementAndGet()
                        if (pending.get() <= 0) isSpeaking = false
                    }
                })
                LogBus.log(LogBus.Category.AGENT, "speech provider ready")
            } else {
                isReady = false
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
        if (pending.get() >= MAX_QUEUE) return false
        val id = "shugo-${System.currentTimeMillis()}"
        pending.incrementAndGet()
        val result = engine.speak(text, TextToSpeech.QUEUE_ADD, null, id)
        if (result != TextToSpeech.SUCCESS) {
            pending.decrementAndGet()
            return false
        }
        return true
    }

    /** Barge-in: stop playback and clear the queue immediately. */
    fun stopAll() {
        if (pending.get() > 0) {
            try { tts?.stop() } catch (_: Exception) {}
        }
        pending.set(0)
        isSpeaking = false
    }

    fun shutdown() {
        stopAll()
        isReady = false
        try { tts?.shutdown() } catch (_: Exception) {}
        tts = null
    }
}
