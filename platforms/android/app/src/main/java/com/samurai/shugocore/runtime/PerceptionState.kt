// PerceptionState.kt — v1.19 unified perception signal registry.
//
// Every provider stamps its signals here when data actually arrives:
//   micActive       microphone stream open and holding the mic
//   voiceDetected   energy threshold crossed (VAD or recognizer audio)
//   humanSpeech     recogniser actively transcribing words (echo-gated:
//                   never true while TTS is on the speaker or in echo
//                   dead time)
//   transcription  last partial/final transcript (value, timestamp,
//                   confidence where the platform provides it, source)
//   visualPresence  last camera analysis result (face count, person
//                   present — value=Int for face count, -1 = not yet)
//   ttsSpeaking     TTS audibly speaking (drives SPEAKING face state)
//   taskInFlight    decision cycle in flight (drives THINKING face state)
//
// Doctrine unchanged: ACTIVE always means a data pulse actually arrived,
// never that a provider merely exists. Honest liveness.
package com.samurai.shugocore.runtime

/** A perception signal — its last known value, the epoch-ms timestamp
 * when it was stamped, an optional confidence (null when the platform
 * does not provide one), and the provider source name. */
data class PerceptionSignal<T>(
    val value: T?,
    val tsMs: Long,
    val confidence: Float? = null,
    val source: String = ""
) {
    /** True when this signal was updated within [maxAgeMs] ms of now. */
    fun fresh(maxAgeMs: Long): Boolean =
        tsMs > 0L && (System.currentTimeMillis() - tsMs) < maxAgeMs
}

object PerceptionState {

    // -- v1.19 unified signals ------------------------------------------------

    /** Microphone stream is open and the provider holds the mic. */
    @Volatile var micActive: Boolean = false

    /** Voice energy detected (VAD threshold crossed / recogniser audio). */
    @Volatile var voiceDetected: Boolean = false

    /** Human speech actively being transcribed (recogniser has words;
     * echo-gated — false while the speaker is our own TTS). */
    @Volatile var humanSpeech: Boolean = false

    /** Last transcript (partial or final) with metadata. */
    @Volatile var transcription: PerceptionSignal<String> =
        PerceptionSignal(null, 0L)

    /** Last camera analysis result (value = face count, -1 = not yet). */
    @Volatile var visualPresence: PerceptionSignal<Int> =
        PerceptionSignal(-1, 0L)

    /** v1.20 attention signals: gaze direction and speech directedness. */
    @Volatile var gazeTowardCamera: Boolean = false
    @Volatile var speechDirectedAtAgent: Boolean = false

    // -- v1.18 legacy compat (migration targets; callers migrate to the
    //    signal names above as the codebase is updated) -----------------------

    @Volatile var lastCameraFrameMs: Long = 0L
    @Volatile var lastFaceCount: Int = -1
    @Volatile var lastMicActivityMs: Long = 0L
    @Volatile var lastTranscript: String? = null
    @Volatile var lastPartialTranscript: String = ""
    @Volatile var ttsSpeaking: Boolean = false
    @Volatile var taskInFlight: Boolean = false
    @Volatile var ttsLastEndMs: Long = 0L
}
