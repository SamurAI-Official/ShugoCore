// CompanionPane.kt — v1.23 COMPANION tab: peripheral discovery and management.
package com.samurai.shugocore.ui

import android.bluetooth.BluetoothDevice
import android.content.Context
import android.widget.Button
import android.widget.LinearLayout
import android.widget.TextView
import com.samurai.shugocore.runtime.ControlPlaneHost

class CompanionPane(context: Context, private val host: ControlPlaneHost) :
    LinearLayout(context) {

    private val modeText: TextView
    private val modeToggle: Button
    private val statusText: TextView
    private val connectedList: LinearLayout
    private val connectedLabel: TextView
    private val pairedList: LinearLayout
    private val pairedLabel: TextView

    init {
        orientation = VERTICAL
        val (scroll, col) = Ui.pane(context)
        addView(scroll)

        col.addView(Ui.section(context, "Companion mode"))
        val modeRow = LinearLayout(context).apply {
            orientation = HORIZONTAL; gravity = android.view.Gravity.CENTER_VERTICAL
        }
        modeText = TextView(context).apply {
            textSize = 14f; setTextColor(Ui.TEXT); text = "Primary agent"
            layoutParams = LinearLayout.LayoutParams(0, LayoutParams.WRAP_CONTENT, 1f)
        }
        modeToggle = Button(context).apply {
            text = "Switch to peripheral"; textSize = 12f; isAllCaps = false
            setOnClickListener { host.onCompanionModeToggle() }
        }
        modeRow.addView(modeText); modeRow.addView(modeToggle)
        col.addView(modeRow)

        // v1.27: live status line — confirms a mode switch completed and shows
        // what a peripheral is streaming to (or how many sensor agents a primary sees).
        statusText = TextView(context).apply {
            textSize = 11f; setTextColor(Ui.DIM); text = ""
            setPadding(0, Ui.dp(context, 2), 0, Ui.dp(context, 6))
        }
        col.addView(statusText)

        col.addView(Ui.section(context, "Paired devices"))
        val hint = TextView(context).apply {
            textSize = 12f; setTextColor(Ui.DIM)
            text = "Pair devices via Android Settings → Bluetooth, then tap Connect"
            setPadding(0, 0, 0, Ui.dp(context, 4))
        }
        col.addView(hint)

        pairedLabel = TextView(context).apply {
            textSize = 13f; setTextColor(Ui.DIM); text = "No paired devices"
            setPadding(0, Ui.dp(context, 4), 0, Ui.dp(context, 4))
        }
        col.addView(pairedLabel)
        pairedList = LinearLayout(context).apply { orientation = VERTICAL }
        col.addView(pairedList)


        col.addView(Ui.section(context, "Connected peers"))
        connectedLabel = TextView(context).apply {
            textSize = 13f; setTextColor(Ui.DIM); text = "No connected devices"
            setPadding(0, Ui.dp(context, 4), 0, Ui.dp(context, 4))
        }
        col.addView(connectedLabel)
        connectedList = LinearLayout(context).apply { orientation = VERTICAL }
        col.addView(connectedList)
    }

    fun bind(snap: Map<*, *>?) {
        // v1.27: read companion_mode from the TOP-LEVEL snapshot (it is a Kotlin
        // flag), NOT from agent_status (the Python status, which is empty when
        // the agent is stopped — i.e. always in peripheral mode). This was the
        // root cause of the toggle never appearing to take effect.
        val companionMode = snap?.get("companion_mode") as? Boolean ?: false
        modeText.text = if (companionMode) "Peripheral mode" else "Primary agent"
        modeToggle.text = if (companionMode) "Switch to primary" else "Switch to peripheral"

        val svc = host.service()

        // v1.27: live status line from the richer sensor-agent state.
        val sensorState = snap?.get("sensor_agent_state") as? String
        val sensorTarget = snap?.get("sensor_agent_target") as? String
        val meshPeerCount = (snap?.get("mesh_peer_count") as? Number)?.toInt() ?: 0
        statusText.text = when (sensorState) {
            "peripheral_streaming" -> "Streaming sensors to ${sensorTarget ?: "primary"} — connected"
            "peripheral_connecting" -> "Peripheral — connecting to ${sensorTarget ?: "primary"}…"
            "peripheral_disconnected" -> "Peripheral — link down; will retry"
            "primary" -> "Primary agent — $meshPeerCount sensor agent(s) connected"
            else -> ""
        }

        val paired = svc?.getPairedDevices() ?: emptyList()
        pairedLabel.text = if (paired.isEmpty()) "No paired devices"
            else "Paired: ${paired.size} device(s):"
        pairedList.removeAllViews()
        for (device in paired) {
            val row = LinearLayout(context).apply {
                orientation = HORIZONTAL; setPadding(0, 0, 0, Ui.dp(context, 4))
            }
            val dot = TextView(context).apply {
                text = "○"; textSize = 12f; setTextColor(Ui.DIM)
                setPadding(0, 0, Ui.dp(context, 8), 0)
            }
            val name = device.name ?: device.address
            val label = TextView(context).apply {
                text = name; textSize = 13f; setTextColor(Ui.TEXT)
                layoutParams = LinearLayout.LayoutParams(0, LayoutParams.WRAP_CONTENT, 1f)
            }
            val connectBtn = Button(context).apply {
                text = "Connect"; textSize = 11f; isAllCaps = false
                setOnClickListener { svc?.connectToMeshPeer(device) }
            }
            row.addView(dot); row.addView(label); row.addView(connectBtn)
            pairedList.addView(row)
        }

        val peers = svc?.getMeshPeers() ?: emptyList()
        connectedLabel.text = if (peers.isEmpty()) "No connected devices"
            else "Connected: ${peers.size} device(s)"
        connectedList.removeAllViews()
        for (peer in peers) {
            val row = LinearLayout(context).apply {
                orientation = HORIZONTAL; setPadding(0, 0, 0, Ui.dp(context, 4))
            }
            val dot = TextView(context).apply {
                text = "●"; textSize = 12f; setTextColor(Ui.OK)
                setPadding(0, 0, Ui.dp(context, 8), 0)
            }
            val caps = if (peer.capabilities.contains("camera")) "📷" else ""
            val mic = if (peer.capabilities.contains("microphone")) "🎤" else ""
            val label = TextView(context).apply {
                text = "${peer.name} [${peer.role}] $caps$mic"; textSize = 13f; setTextColor(Ui.TEXT)
                layoutParams = LinearLayout.LayoutParams(0, LayoutParams.WRAP_CONTENT, 1f)
            }
            val disconnectBtn = Button(context).apply {
                text = "Disconnect"; textSize = 11f; isAllCaps = false
                setOnClickListener { svc?.disconnectMeshPeer(peer.deviceId) }
            }
            row.addView(dot); row.addView(label); row.addView(disconnectBtn)
            connectedList.addView(row)
        }
    }
}