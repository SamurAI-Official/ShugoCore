// TabBar.kt — bottom SERVER | AGENT | SENSORS | SECURITY | LOG bar.
package com.samurai.shugocore.ui

import android.content.Context
import android.graphics.Typeface
import android.widget.Button
import android.widget.LinearLayout

class TabBar(context: Context, tabs: List<String>, private val onSelect: (Int) -> Unit) :
    LinearLayout(context) {

    private val buttons = mutableListOf<Button>()
    private var selected = -1

    init {
        orientation = LinearLayout.HORIZONTAL
        setBackgroundColor(Ui.PANEL_BG)
        setPadding(0, Ui.dp(context, 6), 0, Ui.dp(context, 6))
        tabs.forEachIndexed { index, title ->
            val b = Button(context).apply {
                text = title
                isAllCaps = false
                textSize = 12f
                layoutParams = LinearLayout.LayoutParams(0, LayoutParams.WRAP_CONTENT, 1f)
                    .apply { setMargins(Ui.dp(context, 2), 0, Ui.dp(context, 2), 0) }
                setOnClickListener {
                    // v1.18: the shade must FOLLOW the click — previously only
                    // onSelect ran and the highlight stayed on tab 0 forever.
                    if (index != selected) setSelected(index)
                    onSelect(index)
                }
            }
            buttons.add(b)
            addView(b)
        }
        setSelected(0)
    }

    fun setSelected(index: Int) {
        selected = index
        buttons.forEachIndexed { i, b ->
            val active = i == selected
            b.setTextColor(if (active) Ui.TEXT else Ui.DIM)
            b.setTypeface(b.typeface, if (active) Typeface.BOLD else Typeface.NORMAL)
            b.setBackgroundColor(if (active) 0x33E94560 else 0x00000000)
        }
    }
}
