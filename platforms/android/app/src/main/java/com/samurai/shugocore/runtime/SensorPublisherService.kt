// SensorPublisherService.kt — v1.23 peripheral mode foreground service.
package com.samurai.shugocore.runtime

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.Service
import android.content.Intent
import android.os.Build
import android.os.IBinder
import android.util.Log
import org.json.JSONObject
import java.util.UUID
import java.util.concurrent.Executors
import java.util.concurrent.ScheduledFuture
import java.util.concurrent.TimeUnit

class SensorPublisherService : Service() {
    companion object {
        private const val TAG = "SensorPub"
        private const val CHANNEL_ID = "shugocore_sensor_pub"
        private const val STREAM_INTERVAL_MS = 1_000L
        const val EXTRA_PRIMARY_ID = "primary_id"
        var isRunning = false; private set
        val MESH_SERVICE_UUID = UUID.fromString("00001101-0000-1000-8000-00805F9B34FB")
    }
    private val executor = Executors.newSingleThreadScheduledExecutor()
    private var streamTask: ScheduledFuture<*>? = null
    private var transport: BluetoothTransport? = null
    private var primaryDeviceId: String? = null
    override fun onCreate() {
        super.onCreate(); createNotificationChannel()
        val n = Notification.Builder(this, CHANNEL_ID)
            .setContentTitle("ShugoCore Peripheral")
            .setContentText("Streaming sensors to primary")
            .setSmallIcon(android.R.drawable.ic_menu_camera)
            .setOngoing(true).build()
        startForeground(2, n); isRunning = true
        Log.i(TAG, "sensor publisher started")
    }
    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        primaryDeviceId = intent?.getStringExtra(EXTRA_PRIMARY_ID)
        // Create our own transport and start the RFCOMM server so the primary can connect
        transport = BluetoothTransport(this, MESH_SERVICE_UUID)
        transport?.startServer()
        Log.i(TAG, "peripheral RFCOMM server started on $MESH_SERVICE_UUID")
        startStreaming(); return START_STICKY
    }
    override fun onBind(intent: Intent?): IBinder? = null
    override fun onDestroy() {
        streamTask?.cancel(true); isRunning = false
        Log.i(TAG, "sensor publisher stopped"); super.onDestroy()
    }
    private fun startStreaming() {
        streamTask = executor.scheduleAtFixedRate({
            val p = primaryDeviceId ?: return@scheduleAtFixedRate
            val bt = transport ?: return@scheduleAtFixedRate
            val msg = JSONObject().apply {
                put("type", "sensor/camera")
                put("device_id", Build.MODEL)
                put("status", "active")
                put("payload", JSONObject().apply {
                    put("face_count", -1); put("person_present", false)
                })
            }
            bt.sendMessage(p, msg)
            val mic = JSONObject().apply {
                put("type", "sensor/mic")
                put("device_id", Build.MODEL)
                put("status", "idle")
                put("payload", JSONObject().apply { put("voice_active", false) })
            }
            bt.sendMessage(p, mic)
        }, 0, STREAM_INTERVAL_MS, TimeUnit.MILLISECONDS)
    }
    private fun createNotificationChannel() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            val c = NotificationChannel(CHANNEL_ID, "ShugoCore Sensor",
                NotificationManager.IMPORTANCE_LOW)
            c.description = "Peripheral sensor streaming"
            getSystemService(NotificationManager::class.java).createNotificationChannel(c)
        }
    }
}