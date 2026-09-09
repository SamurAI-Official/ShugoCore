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
        /** v1.27: base streaming interval. Scaled up under thermal pressure. */
        private const val STREAM_INTERVAL_MS = 200L
        private const val STREAM_INTERVAL_MS_HOT = 500L
        private const val HEARTBEAT_INTERVAL_MS = 2_000L
        const val EXTRA_PRIMARY_ID = "primary_id"
        const val EXTRA_STREAM_INTERVAL_MS = "stream_interval_ms"
        var isRunning = false; private set
        /** v1.27: exposed so the primary can show peripheral push freshness. */
        @Volatile var lastSensorPushMs: Long = 0L
        val MESH_SERVICE_UUID = UUID.fromString("00001101-0000-1000-8000-00805F9B34FB")
    }
    private val executor = Executors.newSingleThreadScheduledExecutor()
    private var streamTask: ScheduledFuture<*>? = null
    private var heartbeatTask: ScheduledFuture<*>? = null
    // v1.29: shared mesh manager (one RFCOMM transport per process) — the
    // publisher no longer owns a BluetoothTransport of its own.
    private var mesh: DeviceMeshManager? = null
    private var primaryDeviceId: String? = null
    private var streamIntervalMs: Long = STREAM_INTERVAL_MS
    private var connected = false

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
        intent?.getLongExtra(EXTRA_STREAM_INTERVAL_MS, -1L)?.let {
            if (it > 0) streamIntervalMs = it
        }
        // v1.27: scale interval down if the device is already warm.
        streamIntervalMs = selectInterval(streamIntervalMs)
        // v1.29: ONE shared RFCOMM transport per process. SPS used to build
        // its own BluetoothTransport on MESH_SERVICE_UUID, giving the
        // peripheral TWO listeners on the same SDP record — the primary's
        // connection landed on whichever socket accepted first, and when it
        // hit SPS's socket the mesh manager never saw the device_announce
        // (peers stuck at role=primary, no sensor data). The publisher now
        // feeds the shared DeviceMeshManager transport instead.
        val mgr = DeviceMeshManager.getOrCreate(applicationContext)
        mesh = mgr
        mgr.start()
        // v1.27: announce ourselves as a sensor agent so the primary knows our
        // role + capabilities immediately (previously the peripheral never sent
        // device_announce, so the primary could not distinguish sensor agents).
        announceSelf()
        primaryDeviceId?.let { addr ->
            try {
                val btAdapter = android.bluetooth.BluetoothAdapter.getDefaultAdapter()
                val device = btAdapter?.getRemoteDevice(addr)
                if (device != null) {
                    mgr.connectToPeer(device)
                    Log.i(TAG, "peripheral connecting to primary at $addr")
                } else {
                    Log.w(TAG, "primary device not found in paired list: $addr")
                }
            } catch (e: Exception) {
                Log.e(TAG, "failed to resolve primary device $addr", e)
            }
        }
        Log.i(TAG, "peripheral streaming via shared mesh transport ($MESH_SERVICE_UUID)")
        startStreaming(); startHeartbeat(); return START_STICKY
    }

    /** v1.27: pick a streaming interval based on thermal state (best-effort). */
    private fun selectInterval(requested: Long): Long {
        return try {
            val temps = listOf(
                "/sys/class/thermal/thermal_zone0/temp",
                "/sys/class/thermal/thermal_zone10/temp",
                "/sys/class/thermal/thermal_zone20/temp",
            )
            val temp = temps.asSequence()
                .map { runCatching { java.io.File(it).readText().trim().toFloat() / 1000f }.getOrNull() }
                .firstOrNull { it != null } ?: return requested
            // >70°C → back off; else honour the requested (fast) rate.
            if (temp > 70f) maxOf(requested, STREAM_INTERVAL_MS_HOT) else requested
        } catch (e: Exception) {
            requested
        }
    }

    /** v1.27: announce this device as a peripheral sensor agent. */
    private fun announceSelf() {
        val msg = JSONObject().apply {
            put("type", "device_announce")
            put("role", "peripheral")
            put("device_id", android.os.Build.MODEL)
            put("device_name", android.os.Build.MODEL)
            put("capabilities", JSONObject().apply {
                put("camera",
                    packageManager.hasSystemFeature(android.content.pm.PackageManager.FEATURE_CAMERA_ANY))
                put("microphone",
                    packageManager.hasSystemFeature(android.content.pm.PackageManager.FEATURE_MICROPHONE))
                put("compute", true)
            })
        }
        // post to the next loop tick so the shared transport is fully up
        executor.execute {
            runCatching { mesh?.broadcastToPeers(msg) }
                .onFailure { Log.w(TAG, "sensor announce failed: ${it.message}") }
        }
    }

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onDestroy() {
        streamTask?.cancel(true); heartbeatTask?.cancel(true); isRunning = false
        Log.i(TAG, "sensor publisher stopped"); super.onDestroy()
    }
    private fun startStreaming() {
        streamTask = executor.scheduleAtFixedRate({
            val mgr = mesh ?: return@scheduleAtFixedRate
            // v1.27: merge camera + mic into one message per interval (one
            // serialize + one RFCOMM write instead of two) and stamp the push
            // time so the primary can show peripheral freshness.
            // v1.28: stream REAL local perception signals (not placeholders)
            // including the local visual-audio binding verdict, so the primary
            // can attribute what IT cannot see with its own sensors.
            // v1.29: stream the FULL scene taxonomy (classifyScene adds
            // instruction_directed — vocative "Shugo…" detection — and
            // person_talking, which computeSpeechSource's older 4-state
            // verdict cannot express).
            val (src, srcConf) = PerceptionState.classifyScene()
            val faceCount = PerceptionState.visualPresence.value ?: -1
            val personPresent = PerceptionState.visualPresence.fresh(8_000) && faceCount > 0
            val gazeDir = if (PerceptionState.gazeTowardCamera) "toward_camera" else "away"
            val voiceActive = PerceptionState.voiceDetected || PerceptionState.humanSpeech
            // v1.29: the peripheral's recognized WORDS cross the mesh so the
            // primary can classify scenes ("person giving instructions" vs
            // "TV noise") and feed remote instructions into its own pipeline.
            // Final transcripts only — partials would fragment the primary's
            // conversation memory. Words stay in the interaction bus on the
            // primary; journals record metadata only.
            val localTranscript = PerceptionState.transcription.value?.take(400) ?: ""
            val transcriptFresh = localTranscript.isNotEmpty() &&
                (System.currentTimeMillis() - PerceptionState.transcription.tsMs) < 15_000
            val msg = JSONObject().apply {
                put("type", "sensor/batch")
                put("device_id", android.os.Build.MODEL)
                put("camera", JSONObject().apply {
                    put("status", if (personPresent) "active" else "idle")
                    put("payload", JSONObject().apply {
                        put("face_count", faceCount)
                        put("person_present", personPresent)
                        put("gaze_direction", gazeDir)
                        put("speech_source", src)
                        put("speech_source_confidence", srcConf)
                    })
                })
                put("mic", JSONObject().apply {
                    put("status", if (voiceActive) "active" else "idle")
                    put("payload", JSONObject().apply {
                        put("voice_active", voiceActive)
                        put("speech_source", src)
                        put("speech_source_confidence", srcConf)
                        put("transcript", if (transcriptFresh) localTranscript else "")
                    })
                })
            }
            val sentTo = mgr.broadcastToPeers(msg)
            if (sentTo > 0) lastSensorPushMs = System.currentTimeMillis()
        }, 0, streamIntervalMs, TimeUnit.MILLISECONDS)
    }

    /** v1.27: periodic heartbeat so the primary can detect a silent peripheral. */
    private fun startHeartbeat() {
        heartbeatTask = executor.scheduleAtFixedRate({
            val mgr = mesh ?: return@scheduleAtFixedRate
            val msg = JSONObject().apply {
                put("type", "heartbeat")
                put("device_id", android.os.Build.MODEL)
                put("uptime_ms", android.os.SystemClock.elapsedRealtime())
            }
            mgr.broadcastToPeers(msg)
        }, HEARTBEAT_INTERVAL_MS, HEARTBEAT_INTERVAL_MS, TimeUnit.MILLISECONDS)
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