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
    private var python: Python? = null
    private var pyAgent: PyObject? = null
    private var llamaBridge: LlamaCppBridge? = null
    private var apiServer: LocalApiServer? = null
    private var thermalMonitor: ThermalMonitor? = null
    private var capabilityDetector: CapabilityDetector? = null
    private val executor: ScheduledExecutorService = Executors.newScheduledThreadPool(1)
    private val handler = Handler(Looper.getMainLooper())
    private val binder = LocalBinder()
    
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
        createNotificationChannel()
    }
    
    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        Log.i(TAG, "Service started")
        startForeground(1, buildNotification("Initializing ShugoCore..."))
        executor.execute { initializeInference() }
        executor.scheduleAtFixedRate({
            try {
                val config = thermalMonitor?.getInferenceConfig()
                if (config?.shouldShutdown == true) {
                    Log.w(TAG, "Thermal emergency shutdown")
                    stopSelf()
                } else if (config?.shouldPause != true) {
                    thermalMonitor?.let { tm ->
                        try { pyAgent?.callAttr("update_telemetry", tm.getTelemetryMap()) }
                        catch (e: Exception) { Log.w(TAG, "telemetry push failed: ${e.message}") }
                    }
                    pyAgent?.callAttr("tick")
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
                val callArgs = if (desktopApiUrl != null) {
                    arrayOf<Any>(caps?.soc ?: Build.MODEL, desktopApiUrl)
                } else {
                    arrayOf<Any>(caps?.soc ?: Build.MODEL)
                }
                pyAgent = py.getModule("shugocore_agent")
                    .callAttr("create_agent", *callArgs)
            }
            updateNotification("ShugoCore running")
        } catch (e: Exception) {
            Log.e(TAG, "Failed to initialize inference", e)
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
            nThreads = Runtime.getRuntime().availableProcessors() / 2
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
            externalCacheDir?.resolve("models")
        )
        modelDirs.filterNotNull().forEach { dir ->
            if (dir.exists()) {
                dir.listFiles()?.forEach { file ->
                    if (file.name.endsWith(".gguf")) return file.absolutePath
                }
            }
        }
        return null
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
            pyAgent?.callAttr("get_status")?.toJava(Map::class.java) as? Map<String, Any> ?: emptyMap()
        } catch (e: Exception) { emptyMap() }
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
        Log.i(TAG, "Service destroyed")
        apiServer?.stop()
        llamaBridge?.close()
        executor.shutdown()
        pyAgent?.callAttr("cleanup")
        pyAgent = null
        python = null
    }
}