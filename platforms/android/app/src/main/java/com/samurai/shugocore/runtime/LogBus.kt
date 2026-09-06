// LogBus.kt — central, thread-safe, bounded log buffer for the node control plane.
//
// Kotlin components post directly; Python agent entries are polled by
// ShugoCoreService (recent_logs(after_seq)) and funneled in here, so the
// LOG tab has a single source of truth. Categories map to the LOG tab's
// filter chips: MODEL / AGENT / SENSOR / POLICY / MEMORY (+ ERROR filter,
// which is any entry flagged isError).
package com.samurai.shugocore.runtime

import android.util.Log

object LogBus {
    enum class Category { MODEL, AGENT, SENSOR, POLICY, MEMORY }

    data class Entry(
        val timestampMs: Long,
        val category: Category,
        val message: String,
        val isError: Boolean = false,
    )

    private const val CAPACITY = 500
    private val buffer = ArrayDeque<Entry>(CAPACITY)
    private val listeners = mutableListOf<(Entry) -> Unit>()

    @Synchronized
    fun log(category: Category, message: String, isError: Boolean = false) {
        val entry = Entry(System.currentTimeMillis(), category, message.take(220), isError)
        if (isError) Log.w("ShugoCore/${category.name}", message)
        buffer.addLast(entry)
        while (buffer.size > CAPACITY) buffer.removeFirst()
        for (listener in listeners.toList()) runCatching { listener(entry) }
    }

    @Synchronized
    fun snapshot(): List<Entry> = buffer.toList()

    @Synchronized
    fun addListener(listener: (Entry) -> Unit) {
        listeners.add(listener)
    }

    @Synchronized
    fun removeListener(listener: (Entry) -> Unit) {
        listeners.removeAll { it === listener }
    }
}
