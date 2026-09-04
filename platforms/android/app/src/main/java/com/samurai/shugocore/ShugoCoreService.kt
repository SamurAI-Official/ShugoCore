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
        capabilityDetector = CapabilityDetector()
        createNotificationChannel()
    }
    
    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        Log.i(TAG, "Service started")
        startForeground(1, buildNotification("Initializing ShugoCore..."))
        initializeInference()
        executor.scheduleAtFixedRate({
            try {
                val config = thermalMonitor?.getInferenceConfig()
                if (config?.shouldShutdown == true) {
                    Log.w(TAG, "Thermal emergency shutdown")
                    stopSelf()
                } else if (config?.shouldPause != true) {
                    pyAgent?.callAttr("tick")
                }
            } catch (e: Exception) {
                Log.e(TAG, "Error in agent tick", e)
            }
        }, 0, 100, TimeUnit.MILLISECONDS)
        return START_STICKY_REDELIVER_INTENT
    }
    
    private fun initializeInference() {
        try {
            val caps = capabilityDetector?.detect()
            val modelPath = findModelFile()
            if (modelPath != null) {
                llamaBridge = LlamaCppBridge(modelPath).apply {
                    nGpuLayers = CapabilityDetector.getGpuLayers(caps?.soc ?: "")
                    nThreads = Runtime.getRuntime().availableProcessors() / 2
                    initialize()
                }
                apiServer = LocalApiServer(llamaBridge!!).apply { start() }
            } else {
                Log.w(TAG, "No model found, using Python-only mode")
            }
            python?.let { py ->
                pyAgent = py.getModule("shugocore_agent")
                    .callAttr("create_agent", caps?.name)
            }
            updateNotification("ShugoCore running")
        } catch (e: Exception) {
            Log.e(TAG, "Failed to initialize inference", e)
            updateNotification("Error: ${e.message}")
        }
    }
    
    private fun findModelFile(): String? {
        val modelDirs = listOf(
            filesDir.resolve("models"),
            externalCacheDir?.resolve("models")
        )
        modelDirs.forEach { dir ->
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