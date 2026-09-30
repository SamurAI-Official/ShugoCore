// NodeState.kt — shared snapshots + the contract between panes and MainActivity.
package com.samurai.shugocore.runtime

import android.content.SharedPreferences
import com.samurai.shugocore.ShugoCoreService

/** Immutable stats snapshot from [com.samurai.shugocore.inference.LocalApiServer]. */
data class ServerStats(
    val requests: Long = 0,
    val tokens: Long = 0,
    val lastLatencyMs: Long = 0,
)

/** SECURITY-tab persisted state (fail-closed defaults). */
data class SecurityState(
    val agentCaps: Map<String, Boolean> = emptyMap(),
    val internet: Boolean = false,
    val lan: Boolean = true,
)

/**
 * Callbacks the five panes need from the hosting activity. Kept as an
 * interface so panes stay testable and MainActivity stays thin.
 */
interface ControlPlaneHost {
    fun service(): ShugoCoreService?
    fun prefs(): SharedPreferences
    fun securityState(): SecurityState

    fun requestCapabilityPermission(capId: String)
    fun onServerStartClicked()
    fun onServerStopClicked()
    fun onAgentStartClicked()
    fun onAgentStopClicked()
    fun onStopNodeClicked()
    fun onSpeakTestClicked()
    fun onAgentCapToggled(capId: String, enabled: Boolean)
    fun onNetworkToggled(key: String, enabled: Boolean)
    fun onBackupUrlChanged(url: String)
    fun onBackupTokenChanged(token: String)
    fun onModelProbeClicked()
    fun onCompanionModeToggle()

    // -- night vision policy (SENSORS pane) -----------------------------------
    /**
     * The vision policy as persisted: dark threshold, calibration logging, and
     * Camera2 exposure steps. Keys and defaults are the ones the service reads in
     * `applyVisionPolicy`, so the pane and the service cannot disagree about what
     * "unset" means.
     */
    fun visionPolicy(): Map<String, Any>

    /**
     * Persist one vision policy value and apply it now.
     *
     * `key` is the preference name; `value` is an Int or Boolean. The host writes
     * it and asks the service to apply it immediately rather than waiting for the
     * 30s housekeeping recheck, so an operator watching the camera sees the
     * change take effect.
     */
    fun onVisionPolicyChanged(key: String, value: Any)
}
