// Widgets.kt — tiny programmatic-UI helpers shared by the control-plane panes.
// Zero new dependencies: plain Views, same conventions as the old MainActivity.
package com.samurai.shugocore.ui

import android.content.Context
import android.graphics.Typeface
import android.view.Gravity
import android.view.View
import android.widget.Button
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.TextView

object Ui {
    val PANEL_BG = 0xFF1F2A4E.toInt()
    val OK = 0xFF4CAF50.toInt()
    val WARN = 0xFFFFC107.toInt()
    val BAD = 0xFFE53935.toInt()
    val DIM = 0xFF8A93B2.toInt()
    val TEXT = 0xFFFFFFFF.toInt()

    fun dp(context: Context, v: Int): Int =
        (v * context.resources.displayMetrics.density + 0.5f).toInt()

    /** Grey uppercase section label. */
    fun section(context: Context, title: String): TextView = TextView(context).apply {
        text = title.uppercase()
        textSize = 12f
        setTextColor(DIM)
        setTypeface(typeface, Typeface.BOLD)
        setPadding(0, dp(context, 18), 0, dp(context, 4))
    }

    fun divider(context: Context): View = View(context).apply {
        setBackgroundColor(0x33FFFFFF)
        layoutParams = LinearLayout.LayoutParams(
            LinearLayout.LayoutParams.MATCH_PARENT, dp(context, 1))
    }

    /** key (left) / value (right, monospace, dim) row. */
    fun kv(context: Context, key: String): Pair<LinearLayout, TextView> {
        val row = LinearLayout(context).apply {
            orientation = LinearLayout.HORIZONTAL
            setPadding(0, dp(context, 3), 0, dp(context, 3))
        }
        val k = TextView(context).apply {
            text = key; textSize = 14f; setTextColor(TEXT)
        }
        val v = TextView(context).apply {
            textSize = 14f; typeface = Typeface.MONOSPACE; setTextColor(DIM)
            gravity = Gravity.END
            layoutParams = LinearLayout.LayoutParams(
                0, LinearLayout.LayoutParams.WRAP_CONTENT, 1f)
        }
        row.addView(k)
        row.addView(v)
        return row to v
    }

    /** status row: colored dot + label + right-aligned value. */
    fun statusRow(context: Context, label: String): Triple<LinearLayout, TextView, TextView> {
        val row = LinearLayout(context).apply {
            orientation = LinearLayout.HORIZONTAL
            gravity = Gravity.CENTER_VERTICAL
            setPadding(0, dp(context, 3), 0, dp(context, 3))
        }
        val dot = TextView(context).apply {
            text = "●"; textSize = 14f; setTextColor(DIM)
            setPadding(0, 0, dp(context, 8), 0)
        }
        val k = TextView(context).apply {
            text = label; textSize = 14f; setTextColor(TEXT)
        }
        val v = TextView(context).apply {
            textSize = 13f; typeface = Typeface.MONOSPACE; setTextColor(DIM)
            gravity = Gravity.END
            layoutParams = LinearLayout.LayoutParams(
                0, LinearLayout.LayoutParams.WRAP_CONTENT, 1f)
        }
        row.addView(dot)
        row.addView(k)
        row.addView(v)
        return Triple(row, dot, v)
    }

    fun button(context: Context, label: String): Button = Button(context).apply {
        text = label
        isAllCaps = false
        textSize = 14f
    }

    /** Honest-state coloring for a status word. */
    fun colorFor(state: String): Int = when (state.lowercase()) {
        "running", "online", "granted", "active", "ready", "enabled", "ok",
        "success", "allowed", "enforced", "on" -> OK
        "idle", "mild", "unknown", "degraded", "required", "read only", "warn",
        "no_action", "policy_block", "governor_block", "in_flight", "waiting" -> WARN
        "stopped", "offline", "denied", "disabled", "error", "refused",
        "failed", "unavailable", "blocked",
        "task_failure", "backend_failure", "engine_failure" -> BAD
        else -> DIM
    }

    /** Scroll container with a padded vertical LinearLayout, as pane root. */
    fun pane(context: Context): Pair<ScrollView, LinearLayout> {
        val col = LinearLayout(context).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(dp(context, 16), dp(context, 8), dp(context, 16), dp(context, 24))
        }
        val scroll = ScrollView(context).apply {
            layoutParams = LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT,
                LinearLayout.LayoutParams.MATCH_PARENT)
            isVerticalScrollBarEnabled = true
        }
        scroll.addView(col)
        return scroll to col
    }

    // -- snapshot accessors (service binder maps) ----------------------------

    fun sub(m: Map<*, *>?, key: String): Map<*, *>? = m?.get(key) as? Map<*, *>

    fun list(m: Map<*, *>?, key: String): List<*> =
        m?.get(key) as? List<*> ?: emptyList<Any?>()

    fun str(m: Map<*, *>?, key: String, default: String = "—"): String =
        (m?.get(key))?.toString()?.takeIf { it.isNotEmpty() } ?: default

    fun bool(m: Map<*, *>?, key: String): Boolean = m?.get(key) == true

    fun num(m: Map<*, *>?, key: String): Long = (m?.get(key) as? Number)?.toLong() ?: 0L
}
