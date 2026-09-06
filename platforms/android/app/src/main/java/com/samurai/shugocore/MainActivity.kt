// MainActivity.kt — host of the ShugoCore node control plane.
//
// Layout: NODE STATUS header (always visible) / current pane / tab bar.
// Five panes: SERVER | AGENT | SENSORS | SECURITY | LOG. The activity owns
// service binding, the 1 Hz refresh loop, runtime-permission plumbing and
// the persisted SECURITY state; the panes render snapshots.
package com.samurai.shugocore

import android.content.ComponentName
import android.content.Context
import android.content.Intent
import android.content.ServiceConnection
import android.content.SharedPreferences
import android.os.Bundle
import android.os.Build
import android.os.Handler
import android.os.IBinder
import android.os.Looper
import android.widget.FrameLayout
import android.widget.LinearLayout
import androidx.activity.result.ActivityResultLauncher
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AppCompatActivity
import com.samurai.shugocore.inference.ModelDownloader
import com.samurai.shugocore.runtime.ControlPlaneHost
import com.samurai.shugocore.runtime.HumanInteractionBus
import com.samurai.shugocore.runtime.LogBus
import com.samurai.shugocore.runtime.PermState
import com.samurai.shugocore.runtime.SecurityState
import com.samurai.shugocore.runtime.SensorCapabilityManager
import com.samurai.shugocore.ui.AgentPane
import com.samurai.shugocore.ui.LogPane
import com.samurai.shugocore.ui.NodeStatusHeader
import com.samurai.shugocore.ui.SecurityPane
import com.samurai.shugocore.ui.SensorsPane
import com.samurai.shugocore.ui.ServerPane
import com.samurai.shugocore.ui.TabBar

class MainActivity : AppCompatActivity(), ControlPlaneHost {

    private lateinit var header: NodeStatusHeader
    private lateinit var container: FrameLayout
    private lateinit var serverPane: ServerPane
    private lateinit var agentPane: AgentPane
    private lateinit var sensorsPane: SensorsPane
    private lateinit var securityPane: SecurityPane
    private lateinit var logPane: LogPane
    private var currentTab = 0

    private lateinit var modelDownloader: ModelDownloader
    private lateinit var capabilityManager: SensorCapabilityManager
    private lateinit var permissionLauncher: ActivityResultLauncher<Array<String>>

    private var service: ShugoCoreService? = null
    private var bound = false
    private val statusHandler = Handler(Looper.getMainLooper())
    private val refreshRunnable = object : Runnable {
        override fun run() {
            refreshUi()
            statusHandler.postDelayed(this, 1000)
        }
    }

    private val connection = object : ServiceConnection {
        override fun onServiceConnected(name: ComponentName?, binder: IBinder?) {
            service = (binder as ShugoCoreService.LocalBinder).getService()
            bound = true
            LogBus.log(LogBus.Category.AGENT, "UI bound to node service")
            statusHandler.post(refreshRunnable)
        }

        override fun onServiceDisconnected(name: ComponentName?) {
            bound = false
            service = null
            LogBus.log(LogBus.Category.AGENT, "node service disconnected", isError = true)
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        modelDownloader = ModelDownloader(this)
        capabilityManager = SensorCapabilityManager(this)

        permissionLauncher = registerForActivityResult(
            ActivityResultContracts.RequestMultiplePermissions()) { result ->
            for ((perm, granted) in result) {
                LogBus.log(LogBus.Category.SENSOR,
                    "permission ${perm.substringAfterLast('.')}: " +
                        if (granted) "GRANTED" else "DENIED", isError = !granted)
            }
        }

        val root = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setBackgroundColor(0xFF16213E.toInt())
        }
        header = NodeStatusHeader(this)
        root.addView(header, LinearLayout.LayoutParams(
            LinearLayout.LayoutParams.MATCH_PARENT,
            LinearLayout.LayoutParams.WRAP_CONTENT))

        container = FrameLayout(this)
        root.addView(container, LinearLayout.LayoutParams(
            LinearLayout.LayoutParams.MATCH_PARENT, 0, 1f))

        serverPane = ServerPane(this, this, modelDownloader)
        agentPane = AgentPane(this, this)
        sensorsPane = SensorsPane(this, this)
        securityPane = SecurityPane(this, this)
        logPane = LogPane(this)

        val tabs = listOf("SERVER", "AGENT", "SENSORS", "SECURITY", "LOG")
        val panes = listOf(serverPane, agentPane, sensorsPane, securityPane, logPane)
        root.addView(TabBar(this, tabs) { index ->
            currentTab = index
            container.removeAllViews()
            container.addView(panes[index])
        }, LinearLayout.LayoutParams(
            LinearLayout.LayoutParams.MATCH_PARENT,
            LinearLayout.LayoutParams.WRAP_CONTENT))

        setContentView(root)
        currentTab = 0
        container.addView(serverPane)
    }

    override fun onStart() {
        super.onStart()
        // The service is a foregroundServiceType="dataSync" service: on Android 14+
        // it MUST be started via startService() (which triggers onStartCommand →
        // startForeground + executor), not merely bound. Binding alone only calls
        // onCreate and the whole inference/agent stack never comes up.
        val intent = Intent(this, ShugoCoreService::class.java)
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            startForegroundService(intent)
        } else {
            startService(intent)
        }
        bindService(intent, connection, Context.BIND_AUTO_CREATE)
    }

    override fun onStop() {
        super.onStop()
        statusHandler.removeCallbacks(refreshRunnable)
        if (bound) {
            unbindService(connection)
            bound = false
        }
    }

    override fun onDestroy() {
        super.onDestroy()
        logPane.detach()
    }

    // -- ControlPlaneHost ------------------------------------------------------

    override fun service(): ShugoCoreService? = if (bound) service else null

    override fun prefs(): SharedPreferences =
        getSharedPreferences("shugocore_prefs", Context.MODE_PRIVATE)

    override fun securityState(): SecurityState {
        val caps = SensorCapabilityManager.IDS
            .filter { SensorCapabilityManager.runtimePermissionFor(it) != null }
            .associateWith { prefs().getBoolean("agent_cap_$it", false) }
        return SecurityState(
            agentCaps = caps,
            internet = prefs().getBoolean("net_internet", false),
            lan = prefs().getBoolean("net_lan", true))
    }

    override fun requestCapabilityPermission(capId: String) {
        val perm = SensorCapabilityManager.runtimePermissionFor(capId) ?: return
        if (capabilityManager.permissionState(capId) == PermState.GRANTED) return
        LogBus.log(LogBus.Category.SENSOR,
            "requesting ${SensorCapabilityManager.labelFor(capId)} permission")
        permissionLauncher.launch(arrayOf(perm))
    }

    override fun onServerStartClicked() {
        val svc = service()
        if (svc == null) {
            startNodeService()
            serverPane.status("Starting node service — the server auto-starts with a downloaded model")
            return
        }
        svc.setServerRunning(true) { msg -> serverPane.status(msg) }
    }

    override fun onServerStopClicked() {
        service()?.setServerRunning(false) { msg -> serverPane.status(msg) }
            ?: serverPane.status("Node service not running")
    }

    override fun onAgentStartClicked() {
        val svc = service()
        if (svc == null) {
            startNodeService()
            agentPane.status("Starting node service — agent ticks begin once the backend is up")
            return
        }
        svc.setAgentRunning(true) { msg -> agentPane.status(msg) }
    }

    override fun onAgentStopClicked() {
        service()?.setAgentRunning(false) { msg -> agentPane.status(msg) }
            ?: agentPane.status("Node service not running")
    }

    override fun onStopNodeClicked() {
        Intent(this, ShugoCoreService::class.java).also { stopService(it) }
        LogBus.log(LogBus.Category.AGENT, "node stopped by user")
    }

    override fun onAgentCapToggled(capId: String, enabled: Boolean) {
        prefs().edit().putBoolean("agent_cap_$capId", enabled).apply()
        LogBus.log(LogBus.Category.POLICY,
            "agent capability ${SensorCapabilityManager.labelFor(capId)}: " +
                if (enabled) "ENABLED" else "DISABLED")
        service()?.pushPolicyToAgent()
    }

    override fun onNetworkToggled(key: String, enabled: Boolean) {
        prefs().edit().putBoolean("net_$key", enabled).apply()
        LogBus.log(LogBus.Category.POLICY,
            "network $key: ${if (enabled) "ENABLED" else "DENIED"}")
        service()?.pushPolicyToAgent()
    }

    override fun onBackupUrlChanged(url: String) {
        prefs().edit().putString("desktop_api_url", url).apply()
    }

    override fun onModelProbeClicked() {
        LogBus.log(LogBus.Category.MODEL, "MODEL TEST: request sent")
        service()?.runModelProbe { msg -> serverPane.status(msg) }
    }

    private var lastHumanPingMs = 0L

    /**
     * The honest human-presence source for v1.12 (pre-camera): Android calls
     * this on every user interaction with the activity. Rate-limited to one
     * observation per 5s so scrolling cannot flood the interaction bus.
     */
    override fun onUserInteraction() {
        val now = System.currentTimeMillis()
        if (now - lastHumanPingMs < 5_000L) return
        lastHumanPingMs = now
        val payload = org.json.JSONObject().put("tab", currentTab)
        HumanInteractionBus.post("interaction", "ui_touch", payload)
    }

    private fun startNodeService() {
        Intent(this, ShugoCoreService::class.java).also {
            if (android.os.Build.VERSION.SDK_INT >= android.os.Build.VERSION_CODES.O) {
                startForegroundService(it)
            } else {
                startService(it)
            }
        }
        LogBus.log(LogBus.Category.AGENT, "node service starting…")
    }

    // -- 1 Hz refresh of the visible pane + the node header -------------------

    private fun refreshUi() {
        val svc = service()
        val snap = svc?.getNodeSnapshot()
        header.bind(snap)
        when (currentTab) {
            0 -> serverPane.bind(snap)
            1 -> agentPane.bind(snap)
            2 -> sensorsPane.bind(snap)
            3 -> securityPane.bind(snap)
            // LOG tab (4) refreshes itself via its LogBus listener
        }
    }
}
