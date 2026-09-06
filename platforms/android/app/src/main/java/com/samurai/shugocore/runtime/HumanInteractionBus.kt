// HumanInteractionBus.kt — device-side edge of the Human Interaction
// contract (v1.12 Interaction Foundation).
//
// Android-side human-interaction sources (UI touch today; camera, microphone
// and gaze providers later) post HumanObservation JSON here. The bus forwards
// it to the Python agent (publish_human_observation_json), keeps a bounded
// local record, and parses the agent's response for the presence lifecycle
// event (USER_PRESENT / USER_LEFT / USER_RETURNED). The NODE STATUS header's
// HUMAN row renders ONLY this real event stream — never a decorative dot.
//
// Provider rule: UI/hardware code posts observations; nothing in the
// decision core ever depends on this object.
package com.samurai.shugocore.runtime

import org.json.JSONObject

object HumanInteractionBus {

    private const val CAPACITY = 256

    /** An accepted observation renders HUMAN as present for this long. */
    private const val PRESENCE_WINDOW_MS = 60_000L

    data class Entry(
        val timestampMs: Long,
        val type: String,
        val source: String,
        val presenceEvent: String,
        val accepted: Boolean,
    )

    private val buffer = ArrayDeque<Entry>(CAPACITY)

    @Volatile
    private var publisher: ((String) -> String?)? = null

    /** Wire the Python edge (ShugoCoreService sets this when the agent
     * initializes; clears it in onDestroy). Returns the agent's JSON reply. */
    @Synchronized
    fun setPublisher(p: ((String) -> String?)?) {
        publisher = p
    }

    /** Post one human observation. Never throws: a dead or missing agent
     * still records the attempt locally and logs the rejection honestly. */
    fun post(type: String, source: String,
             payload: JSONObject = JSONObject(), confidence: Double = 1.0) {
        val json = JSONObject()
            .put("type", type)
            .put("source", source)
            .put("payload", payload)
            .put("confidence", confidence)
        val response = try {
            publisher?.invoke(json.toString())
        } catch (e: Exception) {
            null
        }
        var accepted = false
        var presenceEvent = ""
        if (response != null) {
            try {
                val parsed = JSONObject(response)
                accepted = parsed.optBoolean("accepted", false)
                presenceEvent = parsed.optString("presence_event", "")
            } catch (_: Exception) {
            }
        }
        val entry = Entry(System.currentTimeMillis(), type, source,
            presenceEvent, accepted)
        synchronized(buffer) {
            buffer.addLast(entry)
            while (buffer.size > CAPACITY) buffer.removeFirst()
        }
        LogBus.log(LogBus.Category.SENSOR,
            if (accepted) {
                "human observation: $type from $source" +
                    if (presenceEvent.isNotEmpty()) " ($presenceEvent)" else ""
            } else {
                "human observation rejected: $type from $source" +
                    if (response == null) " (no agent)" else ""
            },
            isError = !accepted)
    }

    /** (humanPresent, lastAcceptedAgeMs): header truth — present only when an
     * accepted observation arrived within [PRESENCE_WINDOW_MS]. age < 0 ==
     * never. */
    fun presenceSnapshot(): Pair<Boolean, Long> {
        val last = synchronized(buffer) { buffer.lastOrNull { it.accepted } }
            ?: return Pair(false, -1L)
        val age = System.currentTimeMillis() - last.timestampMs
        return Pair(age in 0..PRESENCE_WINDOW_MS, age)
    }

    fun acceptedCount(): Int =
        synchronized(buffer) { buffer.count { it.accepted } }
}
