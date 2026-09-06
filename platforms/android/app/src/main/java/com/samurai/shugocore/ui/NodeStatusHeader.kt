// NodeStatusHeader.kt — the node dashboard pinned above every tab.
package com.samurai.shugocore.ui

import android.content.Context
import android.graphics.Typeface
import android.view.Gravity
import android.widget.LinearLayout
import android.widget.TextView
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

class NodeStatusHeader(context: Context) : LinearLayout(context) {

    private val agentDot: TextView
    private val agentText: TextView
    private val model: TextView
    private val inference: TextView
    private val memory: TextView
    private val sensors: TextView
    private val policy: TextView
    private val network: TextView
    private val temperature: TextView
    private val battery: TextView

    init {
        orientation = VERTICAL
        setBackgroundColor(Ui.PANEL_BG)
        val pad = Ui.dp(context, 14)
        setPadding(pad, pad, pad, pad)

        val title = TextView(context).apply {
            text = "SHUGOCORE"
            textSize = 20f
            setTypeface(typeface, Typeface.BOLD)
            setTextColor(Ui.TEXT)
        }
        addView(title)

        val agentRow = LinearLayout(context).apply {
            orientation = HORIZONTAL
            gravity = Gravity.CENTER_VERTICAL
        }
        agentDot = TextView(context).apply {
            text = "●"; textSize = 14f; setTextColor(Ui.DIM)
            setPadding(0, 0, Ui.dp(context, 8), 0)
        }
        agentText = TextView(context).apply {
            text = "AGENT —"; textSize = 14f; setTypeface(typeface, Typeface.BOLD)
            setTextColor(Ui.DIM)
        }
        agentRow.addView(agentDot)
        agentRow.addView(agentText)
        addView(agentRow)
        addView(Ui.divider(context))

        fun addRow(label: String): TextView {
            val (row, value) = Ui.kv(context, label)
            addView(row)
            return value
        }
        model = addRow("Model")
        inference = addRow("Inference")
        memory = addRow("Memory")
        sensors = addRow("Sensors")
        policy = addRow("Policy")
        network = addRow("Network")
        temperature = addRow("Temperature")
        battery = addRow("Battery")
    }

    @Suppress("FunctionName")
    private fun Gravity_NO_GRAVITY(): Int = android.view.Gravity.CENTER_VERTICAL

    fun bind(snap: Map<*, *>?) {
        val agent = Ui.sub(snap, "agent_status")
        val agentRunning = Ui.bool(snap, "agent_running")
        val serverRunning = Ui.bool(snap, "server_running")
        val modelName = Ui.str(snap, "model", "")
        val fallback = Ui.str(snap, "fallback_url", "")

        agentDot.setTextColor(if (agentRunning) Ui.OK else Ui.BAD)
        agentText.text = if (agentRunning) "AGENT ONLINE" else "AGENT OFFLINE"
        agentText.setTextColor(if (agentRunning) Ui.OK else Ui.BAD)

        model.text = modelName.ifEmpty { "—" }
        inference.text = when {
            serverRunning -> "LOCAL"
            fallback.isNotEmpty() -> "BACKUP ${hostOf(fallback)}"
            else -> "OFFLINE"
        }
        inference.setTextColor(
            when {
                serverRunning -> Ui.OK
                fallback.isNotEmpty() -> Ui.WARN
                else -> Ui.BAD
            })
        val engineReady = Ui.bool(agent, "engine_ready")
        memory.text = when {
            agentRunning && engineReady -> "READY"
            agentRunning -> "DEGRADED"
            else -> "—"
        }
        memory.setTextColor(
            when {
                agentRunning && engineReady -> Ui.OK
                agentRunning -> Ui.WARN
                else -> Ui.DIM
            })

        val caps = agent?.get("capabilities") as? Map<*, *> ?: emptyMap<Any, Any>()
        val acked = caps.values.count { (it as? Map<*, *>)?.get("agent_ack") == true }
        sensors.text = if (agentRunning) "$acked / ${caps.size}" else "—"
        sensors.setTextColor(if (agentRunning && caps.isNotEmpty()) Ui.OK else Ui.DIM)

        policy.text = if (agentRunning) "ACTIVE" else "—"
        policy.setTextColor(if (agentRunning) Ui.OK else Ui.DIM)

        network.text = when {
            serverRunning -> "localhost"
            fallback.isNotEmpty() -> hostOf(fallback)
            else -> "OFFLINE"
        }
        network.setTextColor(if (serverRunning || fallback.isNotEmpty()) Ui.WARN else Ui.DIM)

        val thermal = Ui.sub(snap, "thermal")
        val cpuTemp = Ui.num(thermal, "cpu_temp")
        temperature.text = if (cpuTemp > 0) "$cpuTemp°C · ${Ui.str(thermal, "state", "NONE")}"
        else "—"
        val batteryLevel = Ui.num(thermal, "battery")
        val charging = Ui.bool(thermal, "charging")
        battery.text = if (batteryLevel > 0) "$batteryLevel%${if (charging) " ⚡" else ""}" else "—"
    }

    private fun hostOf(url: String): String = try {
        java.net.URI(url).host ?: url
    } catch (_: Exception) {
        url
    }

    companion object {
        @Suppress("unused")
        private val timeFmt = SimpleDateFormat("HH:mm:ss", Locale.US)

        @Suppress("unused")
        fun fmtTime(ts: Double): String = timeFmt.format(Date((ts * 1000).toLong()))
    }
}
