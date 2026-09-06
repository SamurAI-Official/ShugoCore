// LogPane.kt — LOG tab: live feed from LogBus (Kotlin + Python events) with
// [ALL] [MODEL] [AGENT] [SENSOR] [POLICY] [MEMORY] [ERROR] filters.
// Monospace TextView in a ScrollView — zero new dependencies; the text is
// rebuilt at most every 250 ms even under log bursts.
package com.samurai.shugocore.ui

import android.content.Context
import android.graphics.Typeface
import android.text.SpannableStringBuilder
import android.text.style.ForegroundColorSpan
import android.widget.Button
import android.widget.HorizontalScrollView
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.TextView
import com.samurai.shugocore.runtime.LogBus
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

class LogPane(context: Context) : LinearLayout(context) {

    private val timeFmt = SimpleDateFormat("HH:mm:ss", Locale.US)
    private val logText: TextView
    private val scroll: ScrollView
    private val filterButtons = mutableMapOf<String, Button>()
    private var filter = "ALL"
    private var rebuildPending = false
    private val listener: (LogBus.Entry) -> Unit = { scheduleRebuild() }

    init {
        orientation = VERTICAL

        // Filter chips
        val chips = HorizontalScrollView(context).apply {
            isHorizontalScrollBarEnabled = false
            setBackgroundColor(Ui.PANEL_BG)
        }
        val chipRow = LinearLayout(context).apply {
            orientation = HORIZONTAL
            setPadding(Ui.dp(context, 8), Ui.dp(context, 4), Ui.dp(context, 8), Ui.dp(context, 4))
        }
        for (f in listOf("ALL", "MODEL", "AGENT", "SENSOR", "POLICY", "MEMORY", "ERROR")) {
            val b = Button(context).apply {
                text = f
                isAllCaps = false
                textSize = 11f
                minHeight = Ui.dp(context, 36)
                minimumWidth = Ui.dp(context, 56)
                setOnClickListener { setFilter(f) }
            }
            filterButtons[f] = b
            chipRow.addView(b)
        }
        chips.addView(chipRow)
        addView(chips)

        scroll = ScrollView(context).apply {
            layoutParams = LinearLayout.LayoutParams(
                LayoutParams.MATCH_PARENT, 0, 1f)
        }
        logText = TextView(context).apply {
            typeface = Typeface.MONOSPACE
            textSize = 11f
            setTextColor(Ui.DIM)
            setPadding(Ui.dp(context, 8), Ui.dp(context, 8),
                Ui.dp(context, 8), Ui.dp(context, 16))
        }
        scroll.addView(logText)
        addView(scroll)

        setFilter("ALL")
        rebuild()
        LogBus.addListener(listener)
    }

    private fun setFilter(f: String) {
        filter = f
        filterButtons.forEach { (name, b) ->
            val active = name == f
            b.setTextColor(if (active) Ui.TEXT else Ui.DIM)
            b.setBackgroundColor(if (active) 0x33E94560 else 0x00000000)
        }
        rebuild()
    }

    private fun scheduleRebuild() {
        if (rebuildPending) return
        rebuildPending = true
        postDelayed({
            rebuildPending = false
            rebuild()
        }, 250)
    }

    private fun rebuild() {
        val entries = LogBus.snapshot()
            .filter { filter == "ALL" || matches(it) }
            .takeLast(200)
        val sb = SpannableStringBuilder()
        for (e in entries) {
            val line = "${timeFmt.format(Date(e.timestampMs))}  " +
                e.category.name.padEnd(7) + "  ${e.message}\n"
            val start = sb.length
            sb.append(line)
            sb.setSpan(
                ForegroundColorSpan(if (e.isError) Ui.BAD else Ui.TEXT),
                start, sb.length, SpannableStringBuilder.SPAN_EXCLUSIVE_EXCLUSIVE)
        }
        if (entries.isEmpty()) sb.append("no log entries\n")
        logText.text = sb
        scroll.post { scroll.fullScroll(ScrollView.FOCUS_DOWN) }
    }

    private fun matches(e: LogBus.Entry): Boolean = when (filter) {
        "ERROR" -> e.isError
        else -> e.category.name == filter
    }

    fun detach() {
        LogBus.removeListener(listener)
    }
}
