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
    private val discoveredList: LinearLayout
    private val connectedList: LinearLayout
    private val discoveredLabel: TextView
    private val connectedLabel: TextView
    private val scanButton: Button
    private var isScanning = false

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

        col.addView(Ui.section(context, "Discovery"))
        val scanRow = LinearLayout(context).apply { orientation = HORIZONTAL }
        scanButton = Button(context).apply {
            text = "Start scan"; textSize = 12f; isAllCaps = false
            setOnClickListener {
                if (isScanning) {
                    host.service()?.stopMeshDiscovery()
                    isScanning = false; scanButton.text = "Start scan"
                } else {
                    host.service()?.startMeshDiscovery()
                    isScanning = true; scanButton.text = "Stop scan"
                }
            }
        }
        scanRow.addView(scanButton)
        val hint = TextView(context).apply {
            textSize = 12f; setTextColor(Ui.DIM)
            text = "  Ensure Bluetooth is enabled on both devices"
            layoutParams = LinearLayout.LayoutParams(0, LayoutParams.WRAP_CONTENT, 1f)
        }
        scanRow.addView(hint); col.addView(scanRow)

        discoveredLabel = TextView(context).apply {
            textSize = 13f; setTextColor(Ui.DIM); text = "No devices discovered"
            setPadding(0, Ui.dp(context, 4), 0, Ui.dp(context, 4))
        }
        col.addView(discoveredLabel)
        discoveredList = LinearLayout(context).apply { orientation = VERTICAL }
        col.addView(discoveredList)

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
        val discovered = svc?.getDiscoveredDevices() ?: emptyList()
        discoveredLabel.text = if (discovered.isEmpty()) "No devices discovered"
            else "Found ${discovered.size} device(s):"
        discoveredList.removeAllViews()
        for (device in discovered) {
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
            discoveredList.addView(row)
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