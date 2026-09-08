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

    /** Recompute the local speech-source verdict from the current signal
     * snapshot. Mirrors AttentionLayer.speech_source() in v1.28 Python —
     * used by the peripheral to stream a truthful attribution. */
    fun computeSpeechSource(): Pair<String, Float> {
        val vis = PerceptionState.visualPresence
        val visFresh = vis.fresh(8_000)
        val faceHere = visFresh && (vis.value ?: 0) > 0
        val speechFresh = PerceptionState.humanSpeech ||
            (System.currentTimeMillis() - PerceptionState.transcription.tsMs) < 15_000
        val gaze = PerceptionState.gazeTowardCamera
        val source: String
        val conf: Float
        if (!speechFresh) {
            source = if (faceHere) "person_present_silent" else "none"
            conf = if (faceHere) 0.9f else 1.0f
        } else if (faceHere && gaze) {
            source = "verified_person"
            conf = 0.9f
        } else {
            source = "unattributed_audio"
            conf = if (faceHere) 0.8f else 0.6f
        }
        PerceptionState.speechSource = source
        PerceptionState.speechSourceConfidence = conf
        return source to conf
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
        payload.optString("speech_source").let { s ->
            if (s.isNotEmpty() && s != "null") {
                remoteSpeechSource = s
                remoteSpeechConfidence =
                    payload.optDouble("speech_source_confidence", 0.0).toFloat()
            }
        }
    }
}
