// ActivityPane.kt — ACTIVITY tab: the bounded recent-cycle ring, the loop
// accounting (cycles / outcomes / sources / rate), and the fleet memory-mesh
// activity. Every row is honest state from AndroidAgent.get_status()
// (loop / loop_stages / mesh_activity / uptime_seconds); missing evidence
// renders as "—", never as success.
package com.samurai.shugocore.ui

import android.content.Context
import android.graphics.Typeface
import android.widget.LinearLayout
import android.widget.TextView
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

class ActivityPane(context: Context) : LinearLayout(context) {

    private val timeFmt = SimpleDateFormat("HH:mm:ss", Locale.US)
    private val cycles: TextView
    private val conversational: TextView
    private val cyclesPerMinute: TextView
    private val successRate: TextView
    private val uptime: TextView
    private val lastCycle: TextView
    private val outcomeRows = mutableMapOf<String, TextView>()
    private val meshTotal: TextView
    private val meshPeers: TextView
    private val activityList: TextView

    init {
        orientation = VERTICAL
        val (scroll, col) = Ui.pane(context)
        addView(scroll)

        // -- Loop accounting -----------------------------------------------------
        col.addView(Ui.section(context, "Loop activity"))
        cycles = col.addKv("Cycles")
        conversational = col.addKv("Conversational ticks")
        cyclesPerMinute = col.addKv("Cycles / minute")
        successRate = col.addKv("Success rate")
        uptime = col.addKv("Uptime")
        lastCycle = col.addKv("Last cycle")

        // -- Outcome ledger ------------------------------------------------------
        col.addView(Ui.section(context, "By outcome"))
        for (outcome in listOf("SUCCESS", "NO_ACTION", "POLICY_BLOCK",
                               "GOVERNOR_BLOCK", "TASK_FAILURE",
                               "BACKEND_FAILURE", "ENGINE_FAILURE",
                               "CONVERSATION")) {
            outcomeRows[outcome] = col.addKv(
                outcome.lowercase().replace('_', ' '))
        }

        // -- Memory mesh ----------------------------------------------------------
        col.addView(Ui.section(context, "Memory mesh"))
        meshTotal = col.addKv("Shared facts (in)")
        meshPeers = col.addKv("Peers")

        // -- Recent cycles ---------------------------------------------------------
        col.addView(Ui.section(context, "Recent cycles"))
        activityList = TextView(context).apply {
            textSize = 12f
            typeface = Typeface.MONOSPACE
            setTextColor(Ui.TEXT)
            setPadding(0, Ui.dp(context, 4), 0, 0)
        }
        col.addView(activityList)
    }

    // -- binding ---------------------------------------------------------------

    /**
     * Bind from the agent status snapshot (get_status_json → flattened map).
     * Missing keys render as "—": an absent loop/uptime/mesh surface is
     * reported, never fabricated as success.
     */
    fun bind(snap: Map<*, *>?) {
        val agent = Ui.sub(snap, "agent_status")
        val loop = Ui.sub(agent, "loop")
        if (loop == null) {
            cycles.text = "—"
            conversational.text = "—"
            cyclesPerMinute.text = "—"
            successRate.text = "—"
            uptime.text = "—"
            lastCycle.text = "—"
            meshTotal.text = "—"
            meshPeers.text = "—"
            activityList.text = "—"
            return
        }

        cycles.text = Ui.str(loop, "cycles")
        conversational.text = Ui.str(loop, "conversational_ticks")
        cyclesPerMinute.text = Ui.str(loop, "cycles_per_minute")

        // success_rate: 0..1 → percent; null (no cycles yet) renders "—".
        val rate = loop.get("success_rate") as? Number
        successRate.text = if (rate == null) "—"
        else "${(rate.toDouble() * 100).toInt()}%"

        uptime.text = Ui.duration(Ui.num(agent, "uptime_seconds").toDouble())

        // Last cycle: ts · source · action · outcome · duration.
        val last = Ui.sub(loop, "last_cycle")
        lastCycle.text = if (last == null) "—" else describe(last)

        // Outcome ledger: counters the agent actually recorded (bounded).
        val byOutcome = Ui.sub(loop, "by_outcome")
        for ((outcome, view) in outcomeRows) {
            val n = (byOutcome?.get(outcome) as? Number)?.toInt() ?: 0
            view.text = n.toString()
            view.setTextColor(if (n > 0) Ui.TEXT else Ui.DIM)
        }

        // Memory mesh: shared facts by provenance peer.
        val mesh = Ui.sub(agent, "mesh_activity")
        val peers = Ui.list(mesh, "peers")
        meshTotal.text = Ui.str(mesh, "total_shared_facts", "0") + " shared fact(s)"
        meshPeers.text = when {
            peers.isEmpty() -> "—"
            else -> peers.joinToString(", ") { peer ->
                when (peer) {
                    is org.json.JSONObject ->
                        "${peer.optString("peer")}: ${peer.optInt("shared_facts")}"
                    else -> peer.toString()
                }
            }
        }

        // Recent cycles, chronological (the ring is oldest → newest).
        val lines = StringBuilder()
        for (entry in recent(loop)) lines.append(describe(entry)).append("\n")
        activityList.text = if (lines.isEmpty()) "—"
        else lines.toString().trimEnd()
    }

    // -- helpers -----------------------------------------------------------------

    private fun LinearLayout.addKv(key: String): TextView {
        val (row, value) = Ui.kv(context, key)
        addView(row)
        return value
    }

    private fun recent(loop: Map<*, *>?): List<org.json.JSONObject> =
        Ui.list(loop, "recent").mapNotNull { it as? org.json.JSONObject }

    /**
     * ts · action_type [outcome] via source · duration.
     * Entries arrive two ways: dicts nested in dicts are converted to Maps,
     * but dicts inside JSON arrays stay org.json.JSONObject — accept both.
     */
    private fun describe(entry: Any?): String {
        val ts: Double
        val action: String
        val outcome: String
        val source: String
        val ms: Long
        when (entry) {
            is org.json.JSONObject -> {
                ts = entry.optDouble("ts", 0.0)
                action = entry.optString("action_type").ifEmpty { "none" }
                outcome = entry.optString("outcome").ifEmpty { "unknown" }
                source = entry.optString("source").ifEmpty { "unknown" }
                ms = entry.optLong("duration_ms", 0L)
            }
            is Map<*, *> -> {
                ts = (entry["ts"] as? Number)?.toDouble() ?: 0.0
                action = Ui.str(entry, "action_type", "none")
                outcome = Ui.str(entry, "outcome", "unknown")
                source = Ui.str(entry, "source", "unknown")
                ms = (entry["duration_ms"] as? Number)?.toLong() ?: 0L
            }
            else -> return "—"
        }
        return "${timeFmt.format(Date((ts * 1000).toLong()))}  " +
               "$action [$outcome] via $source · ${ms}ms"
    }
}
