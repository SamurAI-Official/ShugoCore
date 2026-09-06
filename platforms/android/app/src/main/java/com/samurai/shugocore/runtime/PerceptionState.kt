// PerceptionState.kt — shared liveness holders for perception providers.
// SensorCapabilityManager reads these to render HONEST stream states:
// ACTIVE means frames/data actually arrived recently, never that a
// provider merely exists.
package com.samurai.shugocore.runtime

object PerceptionState {
    /** Last camera frame actually analyzed (0 = none yet). */
    @Volatile var lastCameraFrameMs: Long = 0L

    /** Face count from the most recent analysis (-1 = not analyzed yet). */
    @Volatile var lastFaceCount: Int = -1

    /** Last mic activity (VAD read or recognizer audio) — 0 = none yet. */
    @Volatile var lastMicActivityMs: Long = 0L

    /** Transcript of the most recent recognized utterance (null = none). */
    @Volatile var lastTranscript: String? = null

    // ---- v1.18 voice & presence ----

    /** Live partial transcript from the recognizer (LOG + face liveliness;
     * the journal still only records FINAL transcripts, never partials). */
    @Volatile var lastPartialTranscript: String = ""

    /** True while TTS is audibly speaking (drives the SPEAKING face state
     * and the half-duplex barge-in suppression). */
    @Volatile var ttsSpeaking: Boolean = false

    /** True while a decision cycle is in flight (THINKING face state). */
    @Volatile var taskInFlight: Boolean = false

    /** ElapsedRealtime of the last TTS end (echo dead-time for barge-in). */
    @Volatile var ttsLastEndMs: Long = 0L
}
