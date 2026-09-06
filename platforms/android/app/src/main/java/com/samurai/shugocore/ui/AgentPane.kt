// AgentPane.kt — AGENT tab: subsystem statuses, current cycle, the
// OBSERVE→…→CONSOLIDATE pipeline with the current stage highlighted,
// memory tiers, and START/STOP AGENT. Every row is honest state from
// AndroidAgent.get_status() — no decorative green dots.
package com.samurai.shugocore.ui

import android.content.Context
import android.graphics.Typeface
import android.widget.LinearLayout
import android.widget.TextView
import com.samurai.shugocore.runtime.ControlPlaneHost
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

class AgentPane(context: Context, private val host: ControlPlaneHost) :
    LinearLayout(context) {

    private val statusDots = mutableMapOf<String, TextView>()
    private val statusValues = mutableMapOf<String, TextView>()
    private val engineError: TextView
    private val cycle: TextView
    private val lastTick: TextView
    private val lastDecision: TextView
    private val lastAction: TextView
    private val lastEvaluation: TextView
    private val pipelineDots = mutableMapOf<String, TextView>()
    private val pipelineLabels = mutableMapOf<String, TextView>()
    private val tier0: TextView
    private val tier1: TextView
    private val tier2: TextView
    private val tier3: TextView
    private val statusLine: TextView
    private val timeFmt = SimpleDateFormat("HH:mm:ss", Locale.US)

    init {
        orientation = VERTICAL
        // Ui.pane returns (ScrollView, inner column) with the column already
        // parented inside the ScrollView — add the ScrollView, fill the column.
        val (scroll, col) = Ui.pane(context)
        addView(scroll)

        // -- Status -------------------------------------------------------------
        col.addView(Ui.section(context, "Status"))
        for (label in listOf("Agent", "Decision Engine", "Memory", "Policy", "Tools", "Execution")) {
            val (row, dot, value) = Ui.statusRow(context, label)
            statusDots[label] = dot
            statusValues[label] = value
            col.addView(row)
        }
        engineError = TextView(context).apply {
            textSize = 12f
            typeface = Typeface.MONOSPACE
            setTextColor(Ui.BAD)
            setPadding(0, Ui.dp(context, 2), 0, 0)
        }
        col.addView(engineError)

        // -- Current cycle --------------------------------------------------------
        col.addView(Ui.section(context, "Current cycle"))
        cycle = col.addKv("Cycle")
        lastTick = col.addKv("Last tick")
        lastDecision = col.addKv("Last decision")
        lastAction = col.addKv("Last action")
        lastEvaluation = col.addKv("Last evaluation")

        // -- Pipeline ---------------------------------------------------------------
        col.addView(Ui.section(context, "Pipeline"))
        for (stage in listOf("OBSERVE", "GATE", "DECIDE",
                             "EXECUTE", "EVALUATE", "RECORD", "CONSOLIDATE")) {
            val row = LinearLayout(context).apply {
                orientation = HORIZONTAL
                gravity = android.view.Gravity.CENTER_VERTICAL
                setPadding(Ui.dp(context, 12), Ui.dp(context, 2), 0, Ui.dp(context, 2))
            }
            val dot = TextView(context).apply {
                text = "○"; textSize = 14f; setTextColor(Ui.DIM)
                setPadding(0, 0, Ui.dp(context, 10), 0)
            }
            val label = TextView(context).apply {
                text = stage; textSize = 13f
                typeface = Typeface.MONOSPACE; setTextColor(Ui.DIM)
            }
            pipelineDots[stage] = dot
            pipelineLabels[stage] = label
            row.addView(dot)
            row.addView(label)
            col.addView(row)
        }

        // -- Memory ------------------------------------------------------------------
        col.addView(Ui.section(context, "Memory"))
        tier0 = col.addKv("Tier 0 (scratchpad)")
        tier1 = col.addKv("Tier 1 (episodic)")
        tier2 = col.addKv("Tier 2 (semantic)")
        tier3 = col.addKv("Tier 3 (identity)")

        // -- Controls -------------------------------------------------------------------
        col.addView(Ui.section(context, "Controls"))
        val row = LinearLayout(context).apply { orientation = HORIZONTAL }
        row.addView(Ui.button(context, "Start agent").apply {
            setOnClickListener { host.onAgentStartClicked() }
            layoutParams = LinearLayout.LayoutParams(0, LayoutParams.WRAP_CONTENT, 1f)
        })
        row.addView(Ui.button(context, "Stop agent").apply {
            setOnClickListener { host.onAgentStopClicked() }
            layoutParams = LinearLayout.LayoutParams(0, LayoutParams.WRAP_CONTENT, 1f)
                .apply { setMargins(Ui.dp(context, 8), 0, 0, 0) }
        })
        col.addView(row)
        col.addView(Ui.button(context, "Stop node service").apply {
            setOnClickListener { host.onStopNodeClicked() }
        })
        statusLine = TextView(context).apply {
            textSize = 13f; setTextColor(Ui.WARN)
            setPadding(0, Ui.dp(context, 8), 0, 0)
        }
        col.addView(statusLine)
    }

    fun status(msg: String) {
        statusLine.text = msg
    }

    fun bind(snap: Map<*, *>?) {
        val agent = Ui.sub(snap, "agent_status")
        val running = Ui.bool(snap, "agent_running")
        val engineReady = Ui.bool(agent, "engine_ready")

        setStatus("Agent", if (running) "RUNNING" else "STOPPED", running)
        setStatus("Decision Engine", if (engineReady) "READY" else "ABSENT", engineReady)
        setStatus("Memory", if (running && agent != null) "READY" else "—",
            running && agent != null)
        val policy = Ui.sub(agent, "policy")
        val policyOk = agent != null && policy != null
        setStatus("Policy", if (policyOk) "READY" else "—", policyOk)
        setStatus("Tools", if (engineReady) "READY" else "—", engineReady)
        setStatus("Execution", if (engineReady) "READY" else "—", engineReady)

        cycle.text = if (agent != null) Ui.num(agent, "tick_count").toString() else "—"
        val ts = Ui.num(agent, "last_tick_ts")
        lastTick.text = if (ts > 0) timeFmt.format(Date(ts * 1000)) else "—"
        lastDecision.text = Ui.str(agent, "last_decision")
        lastAction.text = Ui.str(agent, "last_action")
        lastEvaluation.text = Ui.str(agent, "last_evaluation")
        lastEvaluation.setTextColor(Ui.colorFor(Ui.str(agent, "last_evaluation", "")))

        val stages = Ui.list(agent, "pipeline_stages").map { it.toString() }
        val current = stages.lastOrNull()
        for ((stage, dot) in pipelineDots) {
            val ran = stage in stages
            val isCurrent = stage == current
            dot.text = if (ran) "●" else "○"
            dot.setTextColor(when {
                isCurrent -> Ui.OK
                ran -> 0xFF2E7D32.toInt()
                else -> Ui.DIM
            })
            pipelineLabels[stage]?.setTextColor(when {
                isCurrent -> Ui.OK
                ran -> Ui.TEXT
                else -> Ui.DIM
            })
            pipelineLabels[stage]?.setTypeface(
                Typeface.MONOSPACE, if (isCurrent) Typeface.BOLD else Typeface.NORMAL)
        }

        // Surface the real engine/init error instead of a silent ABSENT.
        val engErr = Ui.str(agent, "engine_error")
        val initErr = Ui.str(agent, "init_error")
        engineError.text = when {
            engErr.isNotEmpty() -> "engine: $engErr"
            initErr.isNotEmpty() -> "init: $initErr"
            !engineReady -> "engine: no engine constructed"
            else -> ""
        }
        engineError.visibility = if (engineError.text.isEmpty()) android.view.View.GONE else android.view.View.VISIBLE

        tier0.text = "${Ui.num(agent, "tier0_entries")} items"
        tier1.text = "${Ui.num(agent, "tier1_entries")} items"
        tier2.text = "${Ui.num(agent, "tier2_facts")} facts"
        tier3.text = Ui.str(agent, "tier3", "READ ONLY")
    }

    private fun setStatus(label: String, value: String, ok: Boolean) {
        statusDots[label]?.setTextColor(if (ok) Ui.OK else Ui.DIM)
        statusValues[label]?.text = value
        statusValues[label]?.setTextColor(if (ok) Ui.OK else Ui.DIM)
    }

    private fun LinearLayout.addKv(key: String): TextView {
        val (row, value) = Ui.kv(context, key)
        addView(row)
        return value
    }
}
