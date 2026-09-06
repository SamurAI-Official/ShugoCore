// ShugoCoreService.kt - Foreground service for ShugoCore Android
package com.samurai.shugocore

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.Service
import android.content.Intent
import android.os.Binder
import android.os.Build
import android.os.Handler
import android.os.IBinder
import android.os.Looper
import android.util.Log
import androidx.core.app.NotificationCompat
import com.samurai.shugocore.inference.*
import com.samurai.shugocore.runtime.HumanInteractionBus
import com.samurai.shugocore.runtime.LogBus
import com.samurai.shugocore.runtime.ServerStats
import com.samurai.shugocore.runtime.SensorCapabilityManager
import com.samurai.shugocore.runtime.VisionProvider
import com.chaquo.python.Python
import com.chaquo.python.PyObject
import com.chaquo.python.android.AndroidPlatform
import java.io.File
import java.util.concurrent.Executors
import java.util.concurrent.ScheduledExecutorService
import java.util.concurrent.TimeUnit

class ShugoCoreService : Service() {
    private val TAG = "ShugoCoreService"
    private val CHANNEL_ID = "shugocore_channel"
    private val PREFS = "shugocore_prefs"
    private val CAP_KEYS = listOf("camera", "microphone", "gps", "bluetooth")
    private var python: Python? = null
    private var pyAgent: PyObject? = null
    private var llamaBridge: LlamaCppBridge? = null
    private var apiServer: LocalApiServer? = null
    private var thermalMonitor: ThermalMonitor? = null
    private var capabilityDetector: CapabilityDetector? = null
    private var capabilityManager: SensorCapabilityManager? = null
    private var visionProvider: VisionProvider? = null
    @Volatile private var agentRunning = false
    private val lastLogSeq = java.util.concurrent.atomic.AtomicInteger()
    private val lastLogSeqSeedDone = java.util.concurrent.atomic.AtomicBoolean()
    @Volatile private var lastCapsSignature = ""
    // Two-thread pool: the blocking Python bootstrap (initializeInference)
    // runs on one thread without starving the 1 Hz scheduled task on the
    // other. With a single-threaded pool the 3-minute Python init blocks
    // log polling, tick, and capability pushes until it finishes.
    private val executor: ScheduledExecutorService = Executors.newScheduledThreadPool(2)
    private val handler = Handler(Looper.getMainLooper())
    private val binder = LocalBinder()

    /** Backup (desktop) URL — the STOP-SERVER fallback the agent re-points to. */
    private val fallbackUrl: String?
        get() = getSharedPreferences(PREFS, MODE_PRIVATE)
            .getString("desktop_api_url", null)?.trim()?.takeIf { it.isNotEmpty() }

    
    inner class LocalBinder : Binder() {
        fun getService(): ShugoCoreService = this@ShugoCoreService
    }
    
    override fun onCreate() {
        super.onCreate()
        Log.i(TAG, "Service created")
        if (!Python.isStarted()) {
            Python.start(AndroidPlatform(this))
        }
        python = Python.getInstance()
        thermalMonitor = ThermalMonitor(this)
        capabilityDetector = CapabilityDetector(this)
        capabilityManager = SensorCapabilityManager(this)
        visionProvider = VisionProvider(this)
        createNotificationChannel()
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        Log.i(TAG, "Service started")
        startForeground(1, buildNotification("Initializing ShugoCore..."))
        executor.execute { initializeInference() }
        executor.scheduleAtFixedRate({
            try {
                // Control-plane housekeeping runs even while the agent is
                // paused: logs keep flowing and capability declarations stay
                // current, so the UI never shows stale state.
                pollAgentLogsIntoBus()
                pushCapabilitiesIfChanged()
                visionProvider?.sync()   // camera follows permission reality
                if (agentRunning) {
                    val config = thermalMonitor?.getInferenceConfig()
                    if (config?.shouldShutdown == true) {
                        Log.w(TAG, "Thermal emergency shutdown")
                        stopSelf()
                    } else if (config?.shouldPause != true) {
                        thermalMonitor?.let { tm ->
                            try {
                                // Serialize to JSON so Chaquopy doesn't hand Python
                                // a non-iterable LinkedHashMap proxy.
                                val json = org.json.JSONObject(tm.getTelemetryMap()).toString()
                                pyAgent?.callAttr("update_telemetry_json", json)
                            } catch (e: Exception) { Log.w(TAG, "telemetry push failed: ${e.message}") }
                        }
                        pyAgent?.callAttr("tick")
                    }
                }
            } catch (e: Exception) {
                Log.e(TAG, "Error in agent tick", e)
            }
        }, 0, 1000, TimeUnit.MILLISECONDS)
        return Service.START_STICKY
    }

    
    private fun initializeInference() {
        try {
            val caps = capabilityDetector?.detect()
            startLlamaIfModelAvailable(caps?.soc ?: "")
            if (apiServer == null) {
                Log.w(TAG, "No model found, using external/desktop backend")
            }
            val prefs = getSharedPreferences("shugocore_prefs", MODE_PRIVATE)
            val desktopApiUrl = prefs.getString("desktop_api_url", null)
            python?.let { py ->
                // Inject the app-private writable dir so Python copies of
                // semantic_memory.db / audit_chain.jsonl land somewhere real
                // (the Chaquopy process cwd is root "/", which is read-only).
                // IMPORTANT: always pass exactly 3 positional args to match
                // AndroidAgent.__init__(device_caps, api_url, data_dir) - the
                // no-desktop-URL case used to pass only 2, which silently bound
                // dataDir to api_url and left data_dir=None, crashing every
                // relative-path open (decision_engine.log, semantic_memory.db)
                // with "Read-only file system: '/'".
                val dataDir = filesDir.absolutePath
                val apiUrlArg = desktopApiUrl ?: ""
                val callArgs = arrayOf<Any>(caps?.soc ?: Build.MODEL, apiUrlArg, dataDir)
                pyAgent = py.getModule("shugocore_agent")
                    .callAttr("create_agent", *callArgs)
                agentRunning = true
                lastCapsSignature = ""   // force a capability declaration push
                pushPolicyToAgent()
                pushCapabilitiesIfChanged()
                HumanInteractionBus.setPublisher { json ->
                    try {
                        pyAgent?.callAttr("publish_human_observation_json", json)
                            ?.toString()
                    } catch (e: Exception) {
                        Log.w(TAG, "publish_human_observation failed: ${e.message}")
                        null
                    }
                }
                LogBus.log(LogBus.Category.AGENT,
                    "agent initialized (${caps?.soc ?: Build.MODEL})")
            }
            updateNotification("ShugoCore running")
        } catch (e: Exception) {
            Log.e(TAG, "Failed to initialize inference", e)
            LogBus.log(LogBus.Category.AGENT,
                "agent init failed: ${e.message ?: e.javaClass.simpleName}", isError = true)
            updateNotification("Error: ${e.message}")
        }
    }

    
    /**
     * Starts the on-device llama.cpp stack (bridge + loopback API server) when a
     * model file is available. Safe to call repeatedly — no-ops while running.
     * Returns the active model path, or null when nothing is loaded. Must be
     * called off the main thread (model loading blocks for seconds).
     */
    private fun startLlamaIfModelAvailable(soc: String): String? {
        if (apiServer != null) return llamaBridge?.modelPath
        val modelPath = findModelFile() ?: return null
        llamaBridge?.close()
        val bridge = LlamaCppBridge(modelPath).apply {
            nGpuLayers = CapabilityDetector.getGpuLayers(soc)
            // nThreads is already set by LlamaCppBridge.detectPerformanceCores()
            // to the device's *performance* core count. Do NOT override it with
            // availableProcessors() — on big.LITTLE (Exynos 1380: 4x A78 + 4x
            // A55) that lands half the threadpool on the efficiency cluster and
            // the decode barrier stalls the fast cores behind the slow ones.
        }
        if (!bridge.initialize()) {
            Log.e(TAG, "Failed to load on-device model: $modelPath")
            bridge.close()
            return null
        }
        llamaBridge = bridge
        apiServer = LocalApiServer(bridge, modelName = File(modelPath).nameWithoutExtension)
        apiServer?.start()
        Log.i(TAG, "On-device model active: $modelPath")
        return modelPath
    }

    private fun findModelFile(): String? {
        // The user-selected model (from the Models dialog) always wins.
        val prefs = getSharedPreferences("shugocore_prefs", MODE_PRIVATE)
        prefs.getString("selected_model", null)?.let { p ->
            val f = File(p)
            if (f.exists() && f.name.endsWith(".gguf")) return p
        }
        val modelDirs = listOf(
            filesDir.resolve("models"),
            externalCacheDir?.resolve("models"),
            // /data/local/tmp is the standard adb push target. Apps can read
            // individual files there by path but SELinux blocks listFiles()
            // (returns empty), so we stage any .gguf found into app-private
            // files/models/ where both listing and future loads work.
            File("/data/local/tmp")
        )
        Log.i(TAG, "findModelFile: searching $modelDirs")
        modelDirs.filterNotNull().forEach { dir ->
            if (dir.exists()) {
                val files = dir.listFiles()?.toList() ?: emptyList()
                Log.i(TAG, "findModelFile: $dir exists, ${files.size} files")
                files.forEach { file ->
                    Log.i(TAG, "findModelFile:   ${file.name}")
                    if (file.name.endsWith(".gguf")) {
                        return stageModel(file)
                    }
                }
            } else {
                Log.i(TAG, "findModelFile: $dir does NOT exist")
            }
        }
        // Last resort: the app can read /data/local/tmp by path even though
        // listFiles() is blocked by SELinux. Probe the canonical dev model.
        val canonicalDev = File("/data/local/tmp/Qwen2.5-0.5B-Instruct-Q4_K_M.gguf")
        if (canonicalDev.canRead()) {
            Log.i(TAG, "findModelFile: probing canonical dev model $canonicalDev")
            return stageModel(canonicalDev)
        }
        Log.w(TAG, "findModelFile: no .gguf found in any search dir")
        return null
    }

    /**
     * Copy a GGUF into app-private files/models/ and return its new path.
     * Staging lets the app list/load the model without relying on world-
     * readable shared storage. Skipped when the source already lives in
     * files/models/.
     */
    private fun stageModel(src: File): String {
        val destDir = filesDir.resolve("models")
        if (!destDir.exists()) destDir.mkdirs()
        val dest = File(destDir, src.name)
        if (src.absolutePath == dest.absolutePath) return dest.absolutePath
        if (dest.exists() && dest.length() == src.length()) {
            Log.i(TAG, "findModelFile: already staged at $dest")
            return dest.absolutePath
        }
        Log.i(TAG, "findModelFile: staging ${src.absolutePath} -> $dest")
        try {
            src.inputStream().use { input -> dest.outputStream().use { output -> input.copyTo(output) } }
            Log.i(TAG, "findModelFile: staged ${dest.length()} bytes")
        } catch (e: Exception) {
            Log.e(TAG, "findModelFile: staging failed", e)
            // Fall back to direct read from /data/local/tmp — works because
            // SELinux allows file-by-path reads there even if listing is denied.
            return src.absolutePath
        }
        return dest.absolutePath
    }
    
    private fun createNotificationChannel() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            val channel = NotificationChannel(
                CHANNEL_ID, "ShugoCore Agent",
                NotificationManager.IMPORTANCE_LOW
            ).apply { description = "ShugoCore AI Agent running in background" }
            val manager = getSystemService(NotificationManager::class.java)
            manager?.createNotificationChannel(channel)
        }
    }
    
    private fun buildNotification(text: String): Notification {
        return NotificationCompat.Builder(this, CHANNEL_ID)
            .setContentTitle("ShugoCore")
            .setContentText(text)
            .setSmallIcon(R.drawable.ic_shugocore)
            .build()
    }
    
    private fun updateNotification(text: String) {
        val manager = getSystemService(NotificationManager::class.java)
        manager?.notify(1, buildNotification(text))
    }
    
    fun getAgentStatus(): Map<String, Any> {
        return try {
            val json = pyAgent?.callAttr("get_status_json")?.toString() ?: return emptyMap()
            val obj = org.json.JSONObject(json)
            // Flatten to a Map. Nested dicts (capabilities, policy) stay as
            // JSONObject — the UI reads them via Ui.sub() which handles it.
            val map = mutableMapOf<String, Any>()
            for (key in obj.keys()) {
                val v = obj.opt(key)
                map[key] = when (v) {
                    is org.json.JSONObject -> v.toMapCompat()
                    is org.json.JSONArray -> (0 until v.length()).map { v.opt(it) }
                    org.json.JSONObject.NULL -> ""
                    else -> v
                }
            }
            map
        } catch (e: Exception) {
            Log.e(TAG, "get_status_json failed: ${e.message}")
            emptyMap()
        }
    }

    /** Convert a JSONObject into a Map for the Ui helpers. */
    private fun org.json.JSONObject.toMapCompat(): Map<String, Any> {
        val m = mutableMapOf<String, Any>()
        for (k in this.keys()) {
            val v = this.opt(k)
            m[k] = when (v) {
                is org.json.JSONObject -> v.toMapCompat()
                is org.json.JSONArray -> (0 until v.length()).map { v.opt(it) }
                org.json.JSONObject.NULL -> ""
                else -> v
            }
        }
        return m
    }

    // -- control-plane binder API ---------------------------------------------

    fun isAgentRunning(): Boolean = agentRunning

    fun serverRunning(): Boolean = apiServer?.isRunning == true

    fun inferenceReady(): Boolean = llamaBridge?.isReady == true

    fun serverStats(): ServerStats = apiServer?.statsSnapshot() ?: ServerStats()

    fun fallbackServerUrl(): String? = fallbackUrl

    /**
     * START/STOP SERVER.
     *
     * STOP halts the on-device inference stack (API server + llama bridge).
     * If a backup (desktop) URL is configured the agent keeps running against
     * it (backend re-pointed via AndroidAgent.set_backend_url); without a
     * backup the agent stops too — a tick loop with no backend is not a
     * working node.
     *
     * START (re)starts the inference stack; when the agent was stopped
     * together with the server (no backup configured) it comes back as well.
     */
    fun setServerRunning(running: Boolean, onDone: ((String) -> Unit)? = null) {
        executor.execute {
            var message: String
            var isError = false
            if (running) {
                val path = try {
                    startLlamaIfModelAvailable(capabilityDetector?.detect()?.soc ?: "")
                } catch (e: Exception) {
                    Log.e(TAG, "setServerRunning(start) failed", e)
                    null
                }
                if (path != null) {
                    // Only resurrect the agent alongside the server if it
                    // actually exists — a zygote flag with no pyAgent is the
                    // exact zombie we eliminated in the agent bootstrap.
                    if (!agentRunning && pyAgent == null) {
                        LogBus.log(LogBus.Category.AGENT,
                            "server started, but no agent process — tap Start agent",
                            isError = true)
                    } else if (!agentRunning) {
                        agentRunning = true
                        LogBus.log(LogBus.Category.AGENT, "agent resumed with local server")
                    }
                    message = "Server started: ${File(path).name} @ 127.0.0.1:11434"
                } else {
                    message = "No model available — download one in the model library"
                    isError = true
                }
            } else {
                apiServer?.stop()
                apiServer = null
                llamaBridge?.close()
                llamaBridge = null
                val backup = fallbackUrl
                if (backup != null) {
                    try { pyAgent?.callAttr("set_backend_url", backup) }
                    catch (e: Exception) { Log.w(TAG, "set_backend_url failed: ${e.message}") }
                    message = "Server stopped — agent continues on backup $backup"
                } else {
                    agentRunning = false
                    message = "Server stopped — agent stopped (no backup URL configured)"
                    isError = true
                }
            }
            LogBus.log(LogBus.Category.MODEL, message, isError = isError)
            updateNotification(
                if (agentRunning || serverRunning()) "ShugoCore running"
                else "ShugoCore stopped")
            onDone?.let { cb -> handler.post { cb(message) } }
        }
    }

    /**
     * START/STOP AGENT: pause/resume the 1 Hz tick loop (node stays up).
     * START also guarantees a backend: with no backup URL and no local server
     * it brings the inference stack up first (best effort) — a tick loop
     * without any backend is not a working agent.
     */
    fun setAgentRunning(running: Boolean, onDone: ((String) -> Unit)? = null) {
        executor.execute {
            val message: String
            if (running) {
                if (fallbackUrl == null && !serverRunning()) {
                    val path = try {
                        startLlamaIfModelAvailable(capabilityDetector?.detect()?.soc ?: "")
                    } catch (e: Exception) {
                        Log.e(TAG, "setAgentRunning backend bring-up failed", e)
                        null
                    }
                    if (path == null) {
                        val m = "No backend: start the server or set a backup URL"
                        LogBus.log(LogBus.Category.AGENT, m, isError = true)
                        onDone?.let { cb -> handler.post { cb(m) } }
                        return@execute
                    }
                }
                agentRunning = true
                message = "Agent started"
            } else {
                agentRunning = false
                message = "Agent stopped"
            }
            LogBus.log(LogBus.Category.AGENT, message)
            updateNotification(
                if (agentRunning) "ShugoCore running" else "ShugoCore agent paused")
            onDone?.let { cb -> handler.post { cb(message) } }
        }
    }

    /** MODEL TEST result (primitives only), rendered on the SERVER tab. */
    @Volatile private var modelProbe: Map<String, Any> = emptyMap()

    /** MODEL TEST: one controlled decision round-trip through the real
     *  backend; the result is stored and shown in the node snapshot. */
    fun runModelProbe(onDone: ((String) -> Unit)? = null) {
        val agent = pyAgent
        if (agent == null) {
            modelProbe = mapOf("ok" to false, "error_class" to "no_response",
                               "raw" to "agent not ready")
            onDone?.let { cb -> handler.post { cb("agent not ready") } }
            return
        }
        executor.execute {
            var message: String
            try {
                val json = agent.callAttr("probe_model_json")?.toString() ?: "{}"
                val obj = org.json.JSONObject(json)
                val map = LinkedHashMap<String, Any>()
                for (key in listOf("ok", "error_class", "parse_valid", "action_type",
                                   "confidence", "latency_ms", "chars", "model", "raw")) {
                    when (val v = obj.opt(key)) {
                        null -> map[key] = if (key == "ok" || key == "parse_valid") false else ""
                        is Boolean, is Int, is Long, is Double, is String -> map[key] = v
                        else -> map[key] = v.toString()
                    }
                }
                modelProbe = map
                message = if (obj.optBoolean("ok")) "probe ok"
                          else "probe: ${obj.optString("error_class")}"
            } catch (e: Exception) {
                Log.w(TAG, "probe_model failed: ${e.message}")
                modelProbe = mapOf("ok" to false, "error_class" to "no_response",
                                   "raw" to (e.message ?: "probe failed"))
                message = "probe failed"
            }
            LogBus.log(LogBus.Category.MODEL, "MODEL TEST: $message")
            onDone?.let { cb -> handler.post { cb(message) } }
        }
    }

    /** Full node snapshot for the control-plane UI (cheap, main-thread safe). */
    fun getNodeSnapshot(): Map<String, Any> {
        val thermal = thermalMonitor?.getThermalInfo()
        return mapOf(
            "agent_status" to getAgentStatus(),
            "agent_running" to agentRunning,
            "server_running" to serverRunning(),
            "inference_ready" to inferenceReady(),
            "server_stats" to serverStats().let {
                mapOf("requests" to it.requests, "tokens" to it.tokens,
                      "last_latency_ms" to it.lastLatencyMs)
            },
            "model" to (activeModelName() ?: ""),
            "fallback_url" to (fallbackUrl ?: ""),
            "thermal" to mapOf(
                "battery" to (thermal?.batteryLevel ?: 0),
                "charging" to (thermal?.isCharging ?: false),
                "cpu_temp" to (thermal?.cpuTemp ?: -1f),
                "state" to (thermal?.state?.name ?: "UNKNOWN")),
            "capabilities" to capabilitySnapshotMaps(),
            "model_probe" to modelProbe,
            "interaction" to HumanInteractionBus.presenceSnapshot().let {
                    (fresh, ageMs) ->
                mapOf("fresh" to fresh,
                      "last_age_s" to (if (ageMs >= 0) ageMs / 1000 else -1L),
                      "observations" to HumanInteractionBus.acceptedCount())
            },
        )
    }

    private fun capabilitySnapshotMaps(): List<Map<String, Any>> =
        capabilityManager?.snapshot()?.map { c ->
            mapOf("id" to c.id, "permission" to c.permission.name,
                  "hardware" to c.hardware, "stream" to c.stream.name,
                  "detail" to c.detail)
        } ?: emptyList()

    /** Push SECURITY-tab persisted state (authority + network) to the agent. */
    @JvmOverloads
    fun pushPolicyToAgent(agentCaps: Map<String, Boolean>? = null,
                          internet: Boolean? = null, lan: Boolean? = null) {
        val agent = pyAgent ?: return
        val prefs = getSharedPreferences(PREFS, MODE_PRIVATE)
        try {
            agent.callAttr("update_policy",
                agentCaps ?: CAP_KEYS.associateWith { prefs.getBoolean("agent_cap_$it", false) },
                internet ?: prefs.getBoolean("net_internet", false),
                lan ?: prefs.getBoolean("net_lan", true))
        } catch (e: Exception) {
            Log.w(TAG, "update_policy failed: ${e.message}")
        }
    }

    /** Push the Kotlin capability declarations to the agent when they change. */
    private fun pushCapabilitiesIfChanged() {
        val manager = capabilityManager ?: return
        val agent = pyAgent ?: return
        val signature = manager.signature()
        if (signature == lastCapsSignature) return
        try {
            // Cross the boundary as JSON: Kotlin Lists become non-iterable
            // ArrayList proxies on the Python side (same issue as the log
            // and telemetry paths, which also use JSON).
            val arr = org.json.JSONArray()
            for (decl in manager.snapshot()) arr.put(org.json.JSONObject(decl.declaration()))
            agent.callAttr("update_capabilities_json", arr.toString())
            lastCapsSignature = signature   // set only after a successful push
        } catch (e: Exception) {
            Log.w(TAG, "update_capabilities failed: ${e.message}")
        }
    }

    /** Drain new Python log entries into the Kotlin LogBus. */
    private fun pollAgentLogsIntoBus() {
        val agent = pyAgent ?: return
        try {
            if (!lastLogSeqSeedDone.get()) {
                // First poll: read seq head from status, seed, don't flood tab.
                val json = agent.callAttr("get_status_json")?.toString() ?: return
                val head = try {
                    org.json.JSONObject(json).optLong("last_log_seq", 0L)
                } catch (e: Exception) { 0L }
                lastLogSeq.set(head.toInt())
                lastLogSeqSeedDone.set(true)
                return
            }
            val json = agent.callAttr("recent_logs_json", lastLogSeq.get())?.toString()
                ?: return
            if (json == "[]") return
            val arr = org.json.JSONArray(json)
            for (i in 0 until arr.length()) {
                val m = arr.getJSONObject(i)
                val seq = m.getLong("seq")
                if (seq <= lastLogSeq.get()) continue
                lastLogSeq.set(seq.toInt())
                val category = when (m.optString("category").uppercase()) {
                    "MODEL" -> LogBus.Category.MODEL
                    "SENSOR" -> LogBus.Category.SENSOR
                    "POLICY" -> LogBus.Category.POLICY
                    "MEMORY" -> LogBus.Category.MEMORY
                    else -> LogBus.Category.AGENT
                }
                LogBus.log(category, m.optString("message"),
                    isError = m.optString("level") == "ERROR")
            }
        } catch (e: Exception) {
            Log.w(TAG, "pollAgentLogs failed: ${e.javaClass.simpleName}: ${e.message}")
        }
    }



    fun runSensorTestCycle(steps: Int = 5): Map<String, Any> {
        return try {
            pyAgent?.callAttr("sensor_test_cycle", steps)?.toJava(Map::class.java) as? Map<String, Any> ?: emptyMap()
        } catch (e: Exception) { emptyMap() }
    }

    fun getDeviceRecommendation(): String {
        return try {
            capabilityDetector?.detect()?.let { c ->
                "Recommend ${c.modelBudget} @ ${c.recommendedQuant}, ${c.ramGb}GB RAM, GPU=${CapabilityDetector.getGpuLayers(c.soc)} layers"
            } ?: "Capability detection unavailable"
        } catch (e: Exception) { "Capability detection failed: ${e.message}" }
    }

    /**
     * Hot-loads the selected/downloaded .gguf and starts the loopback API
     * server. The Python agent's default backend is http://127.0.0.1:11434 —
     * exactly where LocalApiServer binds — so the agent picks the model up on
     * its next generate call with no restart. [onLoaded] receives the active
     * model path, or null when nothing could be loaded.
     */
    fun loadOnDeviceModel(onLoaded: ((String?) -> Unit)? = null) {
        executor.execute {
            val path = try {
                startLlamaIfModelAvailable(capabilityDetector?.detect()?.soc ?: "")
            } catch (e: Exception) {
                Log.e(TAG, "loadOnDeviceModel failed", e)
                null
            }
            updateNotification(
                if (path != null) "On-device model: ${File(path).name}"
                else "ShugoCore running (external backend)"
            )
            onLoaded?.let { cb -> handler.post { cb(path) } }
        }
    }

    /** File name of the currently loaded on-device model, if any. */
    fun activeModelName(): String? = llamaBridge?.modelPath?.let { File(it).name }

    override fun onBind(intent: Intent?): IBinder = binder
    
    override fun onDestroy() {
        super.onDestroy()
        HumanInteractionBus.setPublisher(null)
        visionProvider?.stop()
        Log.i(TAG, "Service destroyed")
        LogBus.log(LogBus.Category.AGENT, "node service destroyed")
        agentRunning = false
        apiServer?.stop()
        llamaBridge?.close()
        executor.shutdown()
        pyAgent?.callAttr("cleanup")
        pyAgent = null
        python = null
    }
}