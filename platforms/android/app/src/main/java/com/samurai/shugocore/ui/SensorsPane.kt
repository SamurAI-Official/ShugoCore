// SensorsPane.kt — SENSORS tab: the capability acknowledgement system.
//   Android hardware -> permission -> capability declaration -> stream
//     -> ShugoCore -> agent acknowledgement (AGENTA ACK section).
// Tapping a capability requests its runtime permission. Android permission
// != agent authority: authority lives in the SECURITY tab.
package com.samurai.shugocore.ui

import android.content.Context
import android.widget.LinearLayout
import android.widget.TextView
import com.samurai.shugocore.runtime.ControlPlaneHost
import com.samurai.shugocore.runtime.SensorCapabilityManager

class SensorsPane(context: Context, private val host: ControlPlaneHost) :
    LinearLayout(context) {

    private data class Row(val row: LinearLayout, val dot: TextView, val value: TextView)

    private val capRows = mutableMapOf<String, Row>()
    private val ackValues = mutableMapOf<String, TextView>()
    private val ackSection: TextView

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
    }
}
