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

    // -- v1.24 sensor verification previews ---------------------------------
    /** Latest camera frame as JPEG bytes (null until first frame). */
    @Volatile var lastPreviewJpeg: ByteArray? = null

    // -- v1.22 device mesh signals ---
    @Volatile var meshPeerCount: Int = 0
    @Volatile var meshPeersJson: String = "[]"

    // -- v1.28 visual-audio binding signals ---
    /** Local speech-source verdict (computed from the same freshness windows
     * the Python attention layer uses — face + gaze + speech overlap). */
    @Volatile var speechSource: String = "none"
    @Volatile var speechSourceConfidence: Float = 0f
    /** Remote verdict from the most recent mesh sensor/batch payload. */
    @Volatile var remoteSpeechSource: String = "none"
    @Volatile var remoteSpeechConfidence: Float = 0f
    /** Remote face/voice facts carried in the payload (not assumed). */
    @Volatile var remoteFacePresent: Boolean = false
    @Volatile var remoteVoiceActive: Boolean = false
    @Volatile var remoteGazeTowardCamera: Boolean = false
    /** v1.29: the peer's latest recognized transcript (final-only, ≤400
     * chars) streamed over the mesh — fused into classifyScene() so a
     * remote instruction can drive the primary's own pipeline. Fresh for
     * the same 15 s window as the local transcript. */
    @Volatile var remoteTranscript: String = ""
    @Volatile var remoteTranscriptTs: Long = 0L

    /** Recompute the local speech-source verdict from the current signal
     * snapshot. v1.29: superseded by classifyScene() — delegate so every
     * entry point shares ONE taxonomy (the v1.28 standalone mirror emitted
     * divergent strings like "unattributed_speech"). */
    fun computeSpeechSource(): Pair<String, Float> {
        val (source, conf) = classifyScene()
        PerceptionState.speechSource = source
        PerceptionState.speechSourceConfidence = conf
        return source to conf
    }

    /**
     * v1.29: full scene classification fusing LOCAL and REMOTE perception
     * into one honest label the agent can act on. The taxonomy strings
     * MATCH AttentionLayer.SpeechSource in the Python agent exactly, so a
     * peripheral's verdict can be consumed by the primary as-is. Scenes:
     *   instruction_directed           sentence-initial wake word ("Shugo…",
     *                                  "Hey Shugo…") — addressed to the agent
     *   verified_person                real words + face + gaze (talking to us)
     *   person_present_ambient_noise   face + voice energy but no words (TV/music)
     *   person_present_silent          face, no voice, no words
     *   unattributed_audio             real words, no visible attending talker
     *   ambient_noise                  voice energy, no words, no face
     *   none                           nothing fresh
     * Returns label to confidence.
     */
    fun classifyScene(): Pair<String, Float> {
        val now = System.currentTimeMillis()
        val vis = PerceptionState.visualPresence
        val faceLocal = vis.fresh(8_000) && (vis.value ?: 0) > 0
        val faceHere = faceLocal || remoteFacePresent
        val gaze = gazeTowardCamera || remoteGazeTowardCamera
        val voiceLocal = voiceDetected || humanSpeech
        val voiceHere = voiceLocal || remoteVoiceActive
        // Freshest transcript wins (local STT or a peer's streamed words).
        val localT = transcription
        val localFresh = localT.value != null && (now - localT.tsMs) < 15_000
        val remoteFresh = remoteTranscript.isNotEmpty() &&
            (now - remoteTranscriptTs) < 15_000
        val transcriptValue: String? = when {
            localFresh && (!remoteFresh || localT.tsMs >= remoteTranscriptTs) -> localT.value
            remoteFresh -> remoteTranscript
            else -> null
        }
        val words = transcriptValue?.trim()
            ?.split(Regex("\\s+"))?.filter { it.length >= 2 } ?: emptyList()
        val realSpeech = words.isNotEmpty()
        // v1.29: sentence-initial wake word only — a bare greeting like
        // "hello shugo" is a conversation with a present person, not an
        // instruction call (mirrors Python is_wake_word_call()).
        val t0 = transcriptValue?.trim()?.lowercase()
        val directed = realSpeech && t0 != null && (
            t0.startsWith("shugo") || t0.startsWith("hey shugo") ||
            t0.startsWith("ok shugo") || t0.startsWith("okay shugo"))
        return when {
            directed -> "instruction_directed" to 0.95f
            realSpeech && faceHere && gaze -> "verified_person" to 0.9f
            realSpeech -> "unattributed_audio" to 0.7f
            voiceHere && faceHere -> "person_present_ambient_noise" to 0.75f
            voiceHere -> "ambient_noise" to 0.7f
            faceHere -> "person_present_silent" to 0.9f
            else -> "none" to 1.0f
        }
    }

    /** Stamp a remote camera observation from a mesh peer. */
    fun stampRemoteCamera(deviceId: String, payload: org.json.JSONObject) {
        val now = System.currentTimeMillis()
        val fc = payload.optInt("face_count", -1)
        PerceptionState.visualPresence = PerceptionSignal(
            if (fc >= 0) fc else null, now, source = "remote:$deviceId")
        remoteFacePresent = fc > 0
        payload.optString("gaze_direction").let { g ->
            if (g == "toward_camera") remoteGazeTowardCamera = true
            else if (g == "away") remoteGazeTowardCamera = false
        }
        payload.optString("speech_source").let { s ->
            if (s.isNotEmpty() && s != "null") {
                remoteSpeechSource = s
                remoteSpeechConfidence =
                    payload.optDouble("speech_source_confidence", 0.0).toFloat()
            }
        }
    }

    /** Stamp a remote microphone observation from a mesh peer. */
    fun stampRemoteMic(deviceId: String, payload: org.json.JSONObject) {
        val now = System.currentTimeMillis()
        val act = payload.optBoolean("voice_active", false)
        PerceptionState.micActive = act
        PerceptionState.lastMicActivityMs = now
        remoteVoiceActive = act
        // v1.29: remote transcript crosses the mesh so the primary can
        // classify scenes and FEED REMOTE INSTRUCTIONS into its own
        // conversation pipeline (the peripheral has no agent of its own —
        // its STT words would otherwise die there). Words live only in the
        // interaction bus / bounded conversation memory, never in journals.
        val rt = payload.optString("transcript", "")
        if (rt.isNotEmpty()) {
            remoteTranscript = rt
            remoteTranscriptTs = now
            PerceptionState.transcription = PerceptionSignal(
                rt.take(400), now, source = "remote:$deviceId")
            PerceptionState.humanSpeech = true
        }
        payload.optString("speech_source").let { s ->
            if (s.isNotEmpty() && s != "null") {
                remoteSpeechSource = s
                remoteSpeechConfidence =
                    payload.optDouble("speech_source_confidence", 0.0).toFloat()
            }
        }
    }
}
