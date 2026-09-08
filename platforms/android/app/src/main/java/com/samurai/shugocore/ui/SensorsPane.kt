// SensorsPane.kt — SENSORS tab: the capability acknowledgement system.
//   Android hardware -> permission -> capability declaration -> stream
//     -> ShugoCore -> agent acknowledgement (AGENTA ACK section).
// Tapping a capability requests its runtime permission. Android permission
// != agent authority: authority lives in the SECURITY tab.
package com.samurai.shugocore.ui

import android.content.Context
import android.graphics.Bitmap
import android.graphics.BitmapFactory
import android.widget.ImageView
import android.widget.LinearLayout
import android.widget.TextView
import com.samurai.shugocore.runtime.ControlPlaneHost
import com.samurai.shugocore.runtime.PerceptionState
import com.samurai.shugocore.runtime.SensorCapabilityManager

class SensorsPane(context: Context, private val host: ControlPlaneHost) :
    LinearLayout(context) {

    private data class Row(val row: LinearLayout, val dot: TextView, val value: TextView)

    private val capRows = mutableMapOf<String, Row>()
    private val ackValues = mutableMapOf<String, TextView>()
    private val ackSection: TextView
    private val meshPeersLabel: TextView
    private val meshPeersList: LinearLayout

    // -- v1.24 sensor verification ------------------------------------------
    private val cameraPreview: ImageView
    private val cameraInfo: TextView
    private val micStatus: TextView
    private val transcript: TextView
    private val vadBar: TextView
    // v1.28: visual-audio binding verdict row.
    private val speechSourceLabel: TextView

    init {
        orientation = VERTICAL
        // Ui.pane returns (ScrollView, inner column) with the column already
        // parented inside the ScrollView — add the ScrollView, fill the column.
        val (scroll, col) = Ui.pane(context)
        addView(scroll)

        col.addView(Ui.section(context, "Device capabilities"))
        for (id in SensorCapabilityManager.IDS) {
            val (row, dot, value) = Ui.statusRow(context, SensorCapabilityManager.labelFor(id))
            capRows[id] = Row(row, dot, value)
            row.setOnClickListener { host.requestCapabilityPermission(id) }
            row.isClickable = true
            col.addView(row)
        }
        col.addView(TextView(context).apply {
            textSize = 12f
            setTextColor(Ui.DIM)
            text = "Tap a capability to request its Android permission. " +
                "● Active = data arrived recently; ○ Idle = no stream yet."
        })

        col.addView(Ui.section(context, "Agent acknowledgement"))
        for (id in SensorCapabilityManager.IDS) {
            val (row, value) = Ui.kv(context, SensorCapabilityManager.labelFor(id))
            ackValues[id] = value
            col.addView(row)
        }
        ackSection = TextView(context).apply {
            textSize = 12f
            setTextColor(Ui.DIM)
            setPadding(0, Ui.dp(context, 8), 0, 0)
            text = "The agent only acknowledges capabilities it has received as an " +
                "explicit declaration — it never assumes a capability exists " +
                "because Android granted a permission."
        }
        col.addView(ackSection)

        // v1.22: device mesh peers section
        col.addView(Ui.section(context, "Mesh peers"))
        val meshCount = TextView(context).apply {
            textSize = 14f; setTextColor(Ui.DIM)
            text = "No connected devices"
        }
        col.addView(meshCount)
        meshPeersLabel = meshCount

        // v1.22: dynamic mesh peer list
        meshPeersList = LinearLayout(context).apply {
            orientation = VERTICAL
        }
        col.addView(meshPeersList)

        // -- v1.24 sensor verification: live camera + microphone ------------
        col.addView(Ui.section(context, "What Shugo sees"))
        cameraPreview = ImageView(context).apply {
            setImageBitmap(Bitmap.createBitmap(
                Ui.dp(context, 160), Ui.dp(context, 120),
                Bitmap.Config.RGB_565))
            layoutParams = LinearLayout.LayoutParams(
                Ui.dp(context, 160), Ui.dp(context, 120))
        }
        col.addView(cameraPreview)
        cameraInfo = TextView(context).apply {
            textSize = 13f; setTextColor(Ui.DIM)
            text = "Waiting for camera…"
            setPadding(0, Ui.dp(context, 2), 0, Ui.dp(context, 4))
        }
        col.addView(cameraInfo)

        col.addView(Ui.section(context, "What Shugo hears"))
        micStatus = TextView(context).apply {
            textSize = 13f; setTextColor(Ui.DIM)
            text = "Microphone inactive"
            setPadding(0, Ui.dp(context, 2), 0, Ui.dp(context, 4))
        }
        col.addView(micStatus)
        // v1.28: visual-audio binding verdict — "person talking to Shugo"
        // vs TV / music / ambient noise.
        speechSourceLabel = TextView(context).apply {
            textSize = 13f; setTextColor(Ui.DIM)
            text = "Speech source: —"
            setPadding(0, Ui.dp(context, 2), 0, Ui.dp(context, 4))
        }
        col.addView(speechSourceLabel)
        vadBar = TextView(context).apply {
            textSize = 12f; setTextColor(Ui.WARN)
            text = "Voice level: —"
            setPadding(0, Ui.dp(context, 2), 0, Ui.dp(context, 4))
        }
        col.addView(vadBar)
        transcript = TextView(context).apply {
            textSize = 13f; typeface = android.graphics.Typeface.MONOSPACE
            setTextColor(Ui.TEXT)
            text = "No speech detected yet"
            setPadding(0, Ui.dp(context, 2), 0, Ui.dp(context, 4))
        }
        col.addView(transcript)
    }

    fun bind(snap: Map<*, *>?) {
        val caps = Ui.list(snap, "capabilities")
        val declared = mutableMapOf<String, Map<*, *>>()
        for (c in caps) {
            val m = c as? Map<*, *> ?: continue
            val id = m["id"]?.toString() ?: continue
            declared[id] = m
        }

        for ((id, row) in capRows) {
            val c = declared[id]
            val permission = c?.get("permission")?.toString() ?: "unknown"
            val hardware = c?.get("hardware") == true
            val stream = c?.get("stream")?.toString() ?: "idle"
            val detail = c?.get("detail")?.toString() ?: ""

            row.dot.setTextColor(when (permission) {
                "GRANTED" -> if (stream == "ACTIVE") Ui.OK else Ui.WARN
                "DENIED" -> Ui.BAD
                else -> Ui.DIM
            })
            row.value.text = when {
                !hardware -> "unavailable"
                permission == "GRANTED" ->
                    (if (stream == "ACTIVE") "✓ Granted · ● Active" else "✓ Granted · ○ Idle") +
                        (if (detail.isNotEmpty()) " · $detail" else "")
                permission == "DENIED" -> "✗ Denied — tap to grant"
                else -> "no permission needed"
            }
            row.value.setTextColor(when (permission) {
                "GRANTED" -> if (stream == "ACTIVE") Ui.OK else Ui.WARN
                "DENIED" -> Ui.BAD
                else -> Ui.DIM
            })
        }

        val agent = Ui.sub(snap, "agent_status")
        val acks = agent?.get("capabilities") as? Map<*, *> ?: emptyMap<Any, Any>()
        for ((id, value) in ackValues) {
            val decl = acks[id] as? Map<*, *>
            value.text = when {
                decl == null -> "no declaration"
                decl["agent_ack"] == true ->
                    "ACK ✓ · ${decl["permission"]} · ${decl["stream"]}"
                else -> "pending"
            }
            value.setTextColor(if (decl?.get("agent_ack") == true) Ui.OK else Ui.DIM)
        }

        // v1.22: bind mesh peers
        val meshAgent = agent ?: Ui.sub(snap, "agent_status")
        val meshCount = (meshAgent?.get("mesh_peer_count") as? Number)?.toInt() ?: 0
        val meshPeers = meshAgent?.get("mesh_peers") as? List<*> ?: emptyList<Any>()
        meshPeersLabel.text = when {
            meshCount > 0 -> "Connected: $meshCount device(s)"
            else -> "No connected devices"
        }
        meshPeersList.removeAllViews()
        for (peer in meshPeers) {
            val p = peer as? Map<*, *> ?: continue
            val id = p["device_id"]?.toString() ?: "unknown"
            val hasCamera = p["camera"] == true
            val hasMic = p["mic"] == true
            val row = LinearLayout(context).apply {
                orientation = HORIZONTAL
                setPadding(0, 0, 0, Ui.dp(context, 4))
            }
            val dot = TextView(context).apply {
                text = "●"; textSize = 12f
                setTextColor(if (hasCamera || hasMic) Ui.OK else Ui.DIM)
                setPadding(0, 0, Ui.dp(context, 8), 0)
            }
            val label = TextView(context).apply {
                text = "$id ${if (hasCamera) "📷" else ""}${if (hasMic) "🎤" else ""}"
                textSize = 13f; setTextColor(Ui.TEXT)
            }
            row.addView(dot); row.addView(label)
            meshPeersList.addView(row)
        }

        // -- v1.24 sensor verification bind ---------------------------------
        // Camera preview: render the latest JPEG frame if available.
        val jpeg: ByteArray? = PerceptionState.lastPreviewJpeg
        if (jpeg != null && jpeg.size > 0) {
            try {
                val bmp = android.graphics.BitmapFactory.decodeByteArray(
                    jpeg, 0, jpeg.size)
                if (bmp != null) cameraPreview.setImageBitmap(bmp)
            } catch (_: Exception) { /* keep last frame on decode failure */ }
        }
        val faceCount: Int? = if (PerceptionState.visualPresence.fresh(5_000))
            PerceptionState.visualPresence.value else null
        cameraInfo.text = when {
            faceCount != null && faceCount >= 0 ->
                "face_count=$faceCount · gaze=${if (PerceptionState.gazeTowardCamera) "toward_camera" else "away"}"
            else -> "no face detected (waiting)"
        }
        cameraInfo.setTextColor(
            if (faceCount != null && faceCount > 0) Ui.OK else Ui.DIM)

        // Microphone status from PerceptionState signals.
        val micActive = PerceptionState.micActive
        val voiceActive = PerceptionState.voiceDetected
        val humanSpeech = PerceptionState.humanSpeech
        micStatus.text = when {
            humanSpeech -> "🎤 ● LISTENING (speech being transcribed)"
            voiceActive -> "🎤 ● VOICE ACTIVITY"
            micActive -> "🎤 ● Active (awaiting speech)"
            else -> "🎤 ○ Mic inactive"
        }
        micStatus.setTextColor(when {
            humanSpeech -> Ui.OK
            voiceActive -> Ui.WARN
            micActive -> Ui.OK
            else -> Ui.DIM
        })
        // VAD meter: a fake "level" from how recently mic data arrived.
        val micAge = System.currentTimeMillis() - PerceptionState.lastMicActivityMs
        val level = when {
            micAge < 1_000 -> if (voiceActive || humanSpeech) "████" else "██░░"
            micAge < 3_000 -> "██░░"
            micAge < 10_000 -> "█░░░"
            else -> "░░░░"
        }
        vadBar.text = "Voice level: $level"
        vadBar.setTextColor(
            if (voiceActive || humanSpeech) Ui.OK else Ui.WARN)
        // Last transcript.
        val lastTrans = PerceptionState.lastTranscript ?: ""
        transcript.text = if (lastTrans.isNotEmpty()) "\"$lastTrans\""
            else "No speech detected yet"
        transcript.setTextColor(
            if (lastTrans.isNotEmpty()) Ui.TEXT else Ui.DIM)

        // v1.28: visual-audio binding. Local verdict comes from the signal
        // overlap computed right here; the remote verdict (a sensor agent's
        // streaming attribution) rides in the status snapshot.
        val (src, conf) = PerceptionState.computeSpeechSource()
        val remoteSrc = (agent?.get("remote_binding") as? Map<*, *>)
            ?.get("speech_source")?.toString()
        val base = when (src) {
            "verified_person" -> "Speech source: ● VERIFIED person talking (conf $conf)"
            "unattributed_audio" -> "Speech source: △ audio, no visible talker (conf $conf)"
            "person_present_silent" -> "Speech source: ○ person present, silent"
            else -> "Speech source: — no speech evidence"
        }
        speechSourceLabel.text = if (remoteSrc != null &&
            remoteSrc != "none" && remoteSrc != src)
            "$base · remote: $remoteSrc" else base
        speechSourceLabel.setTextColor(when (src) {
            "verified_person" -> Ui.OK
            "unattributed_audio" -> Ui.WARN
            else -> Ui.DIM
        })
    }
}
