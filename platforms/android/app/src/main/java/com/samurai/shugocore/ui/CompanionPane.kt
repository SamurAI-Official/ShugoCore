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
        val agent = Ui.sub(snap, "agent_status")
        val companionMode = agent?.get("companion_mode") as? Boolean ?: false
        modeText.text = if (companionMode) "Peripheral mode" else "Primary agent"
        modeToggle.text = if (companionMode) "Switch to primary" else "Switch to peripheral"

        val svc = host.service()
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
                text = "${peer.name} $caps$mic"; textSize = 13f; setTextColor(Ui.TEXT)
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