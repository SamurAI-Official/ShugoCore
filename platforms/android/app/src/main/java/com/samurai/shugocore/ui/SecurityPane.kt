// SecurityPane.kt — SECURITY tab: ShugoCore's capability governor.
// Four distinct states per capability: Android permission vs agent authority.
// Toggles persist locally and are pushed into AndroidAgent.update_policy()
// which enforces the network posture in tick() (fail closed).
package com.samurai.shugocore.ui

import android.content.Context
import android.widget.LinearLayout
import android.widget.TextView
import androidx.appcompat.widget.SwitchCompat
import com.samurai.shugocore.runtime.ControlPlaneHost

class SecurityPane(context: Context, private val host: ControlPlaneHost) :
    LinearLayout(context) {

    private val permValues = mutableMapOf<String, TextView>()
    private val capSwitches = mutableMapOf<String, SwitchCompat>()
    private val auditDot: TextView
    private val auditValue: TextView
    private val consentValue: TextView
    private val lanSwitch: SwitchCompat
    private val internetSwitch: SwitchCompat

    init {
        orientation = VERTICAL
        // Ui.pane returns (ScrollView, inner column) with the column already
        // parented inside the ScrollView — add the ScrollView, fill the column.
        val (scroll, col) = Ui.pane(context)
        addView(scroll)

        // -- Device permissions (Android) --------------------------------------
        col.addView(Ui.section(context, "Device permissions"))
        for ((id, label) in listOf("camera" to "Camera", "microphone" to "Microphone",
                                   "gps" to "Location", "bluetooth" to "Bluetooth")) {
            val (row, value) = Ui.kv(context, label)
            permValues[id] = value
            col.addView(row)
        }

        // -- Agent capabilities (ShugoCore authority) ---------------------------
        col.addView(Ui.section(context, "Agent capabilities"))
        col.addView(TextView(context).apply {
            textSize = 12f
            setTextColor(Ui.DIM)
            text = "Android permission ≠ agent authority. An agent capability is " +
                "only enabled here, explicitly, and defaults to DISABLED."
        })
        for ((id, label) in listOf("camera" to "Camera", "microphone" to "Microphone",
                                   "gps" to "Location", "bluetooth" to "Bluetooth")) {
            val sw = SwitchCompat(context).apply {
                text = label
                textSize = 14f
                isChecked = host.securityState().agentCaps[id] == true
                setOnCheckedChangeListener { _, checked ->
                    host.onAgentCapToggled(id, checked)
                }
            }
            capSwitches[id] = sw
            col.addView(sw)
        }

        // -- Network --------------------------------------------------------------
        col.addView(Ui.section(context, "Network"))
        val (lhRow, lhValue) = Ui.kv(context, "localhost")
        lhValue.text = "ENABLED (always)"
        lhValue.setTextColor(Ui.OK)
        col.addView(lhRow)
        lanSwitch = SwitchCompat(context).apply {
            text = "LAN"
            textSize = 14f
            isChecked = host.securityState().lan
            setOnCheckedChangeListener { _, checked -> host.onNetworkToggled("lan", checked) }
        }
        col.addView(lanSwitch)
        internetSwitch = SwitchCompat(context).apply {
            text = "Internet"
            textSize = 14f
            isChecked = host.securityState().internet
            setOnCheckedChangeListener { _, checked -> host.onNetworkToggled("internet", checked) }
        }
        col.addView(internetSwitch)

        // -- Tools ------------------------------------------------------------------
        col.addView(Ui.section(context, "Tools"))
        for ((label, value) in listOf(
            "Filesystem" to "READ ONLY", "Shell" to "DENIED", "HTTP" to "DENIED",
            "ROS 2" to "DENIED", "Actuators" to "DENIED")) {
            val (row, v) = Ui.kv(context, label)
            v.text = value
            v.setTextColor(if (value == "DENIED") Ui.BAD else Ui.WARN)
            col.addView(row)
        }

        // -- Policy --------------------------------------------------------------------
        col.addView(Ui.section(context, "Policy"))
        val (fcRow, fcDot, fcValue) = Ui.statusRow(context, "FAIL CLOSED")
        fcDot.setTextColor(Ui.OK)
        fcValue.text = "ENFORCED"
        fcValue.setTextColor(Ui.OK)
        col.addView(fcRow)
        val (auRow, auD, auV) = Ui.statusRow(context, "AUDIT")
        auditDot = auD
        auditValue = auV
        col.addView(auRow)
        val (coRow, _, coV) = Ui.statusRow(context, "CONSENT")
        consentValue = coV
        col.addView(coRow)
    }

    fun bind(snap: Map<*, *>?) {
        // Android permission states (from the capability snapshot).
        val caps = Ui.list(snap, "capabilities")
        val byId = caps.mapNotNull { it as? Map<*, *> }
            .associateBy { it["id"]?.toString() ?: "" }
        for ((id, value) in permValues) {
            val granted = byId[id]?.get("permission") == "GRANTED"
            value.text = if (granted) "ALLOWED" else "DENIED"
            value.setTextColor(if (granted) Ui.OK else Ui.BAD)
        }

        // Policy flags from the agent's live state.
        val policy = Ui.sub(Ui.sub(snap, "agent_status"), "policy")
        val auditOn = Ui.bool(policy, "audit")
        auditDot.setTextColor(if (auditOn) Ui.OK else Ui.WARN)
        auditValue.text = if (auditOn) "ENABLED" else "CHAIN UNAVAILABLE"
        auditValue.setTextColor(if (auditOn) Ui.OK else Ui.WARN)
        consentValue.text = if (policy != null) "REQUIRED (side effects)" else "—"
        consentValue.setTextColor(Ui.WARN)
    }
}
