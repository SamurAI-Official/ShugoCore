// SensorCapabilityManager.kt — the capability acknowledgement system.
//
// The review's contract, one layer per step:
//   Android hardware -> permission -> capability declaration -> data stream
//     -> ShugoCore -> agent acknowledgement (Python side, update_capabilities).
//
// This manager owns the first four steps. The agent ACK lives on the Python
// side and is pushed by ShugoCoreService when the declaration set changes.
// Android permission != agent authority: authority is the SECURITY tab.
package com.samurai.shugocore.runtime

import android.Manifest
import android.content.Context
import android.content.pm.PackageManager
import android.hardware.Sensor
import android.hardware.SensorEvent
import android.hardware.SensorEventListener
import android.hardware.SensorManager
import android.location.LocationManager
import android.net.ConnectivityManager
import android.net.NetworkCapabilities
import android.os.Build
import androidx.core.content.ContextCompat

enum class PermState { GRANTED, DENIED, UNAVAILABLE }
enum class StreamState { ACTIVE, IDLE }

class SensorCapabilityManager(private val context: Context) {

    companion object {
        val IDS = listOf(
            "camera", "microphone", "gps", "imu",
            "gyroscope", "accelerometer", "bluetooth", "wifi",
        )

        fun labelFor(id: String): String = when (id) {
            "camera" -> "Camera"
            "microphone" -> "Microphone"
            "gps" -> "GPS"
            "imu" -> "IMU"
            "gyroscope" -> "Gyroscope"
            "accelerometer" -> "Accelerometer"
            "bluetooth" -> "Bluetooth"
            "wifi" -> "Wi-Fi"
            else -> id
        }

        /** Runtime permission the capability needs, if any. */
        fun runtimePermissionFor(id: String): String? = when (id) {
            "camera" -> Manifest.permission.CAMERA
            "microphone" -> Manifest.permission.RECORD_AUDIO
            "gps" -> Manifest.permission.ACCESS_FINE_LOCATION
            "bluetooth" ->
                if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S)
                    Manifest.permission.BLUETOOTH_CONNECT else null
            else -> null
        }
    }

    data class Capability(
        val id: String,
        val permission: PermState,
        val hardware: Boolean,
        val stream: StreamState,
        val detail: String,
    ) {
        /** The declaration pushed to the agent (update_capabilities). */
        fun declaration(): Map<String, Any> = mapOf(
            "name" to id,
            "state" to if (hardware) "available" else "unavailable",
            "permission" to permission.name.lowercase(),
            "stream" to if (stream == StreamState.ACTIVE) "active" else "idle",
            "detail" to detail,
        )

        fun signature(): String =
            "$id:${permission.name}:$hardware:${stream.name}:$detail"
    }

    private val sensorManager =
        context.getSystemService(Context.SENSOR_SERVICE) as SensorManager
    private var lastAccelMs = 0L
    private var lastGyroMs = 0L
    private var lastRotMs = 0L

    private val sensorListener = object : SensorEventListener {
        override fun onSensorChanged(event: SensorEvent?) {
            val now = System.currentTimeMillis()
            when (event?.sensor?.type) {
                Sensor.TYPE_ACCELEROMETER -> lastAccelMs = now
                Sensor.TYPE_GYROSCOPE -> lastGyroMs = now
                Sensor.TYPE_ROTATION_VECTOR -> lastRotMs = now
            }
        }

        override fun onAccuracyChanged(sensor: Sensor?, accuracy: Int) {}
    }

    init {
        listOf(
            Sensor.TYPE_ACCELEROMETER,
            Sensor.TYPE_GYROSCOPE,
            Sensor.TYPE_ROTATION_VECTOR,
        ).mapNotNull { sensorManager.getDefaultSensor(it) }.forEach {
            sensorManager.registerListener(sensorListener, it, SensorManager.SENSOR_DELAY_NORMAL)
        }
    }

    fun hasHardware(id: String): Boolean = when (id) {
        "camera" -> context.packageManager.hasSystemFeature(PackageManager.FEATURE_CAMERA_ANY)
        "microphone" -> context.packageManager.hasSystemFeature(PackageManager.FEATURE_MICROPHONE)
        "gps" -> context.packageManager.hasSystemFeature(PackageManager.FEATURE_LOCATION_GPS)
        "imu" -> sensorManager.getDefaultSensor(Sensor.TYPE_ACCELEROMETER) != null &&
            sensorManager.getDefaultSensor(Sensor.TYPE_GYROSCOPE) != null
        "gyroscope" -> sensorManager.getDefaultSensor(Sensor.TYPE_GYROSCOPE) != null
        "accelerometer" -> sensorManager.getDefaultSensor(Sensor.TYPE_ACCELEROMETER) != null
        "bluetooth" -> context.packageManager.hasSystemFeature(PackageManager.FEATURE_BLUETOOTH)
        "wifi" -> context.packageManager.hasSystemFeature(PackageManager.FEATURE_WIFI)
        else -> false
    }

    fun permissionState(id: String): PermState {
        val perm = runtimePermissionFor(id)
            ?: return if (hasHardware(id)) PermState.GRANTED else PermState.UNAVAILABLE
        val granted = ContextCompat.checkSelfPermission(context, perm) ==
            PackageManager.PERMISSION_GRANTED
        return if (granted) PermState.GRANTED else PermState.DENIED
    }

    /** A stream is ACTIVE only when data actually arrived recently (honest). */
    fun streamState(id: String): StreamState {
        val now = System.currentTimeMillis()
        val fresh = { last: Long -> now - last < 3_000L }
        return when (id) {
            "accelerometer" -> if (fresh(lastAccelMs)) StreamState.ACTIVE else StreamState.IDLE
            "gyroscope" -> if (fresh(lastGyroMs)) StreamState.ACTIVE else StreamState.IDLE
            "imu" -> if (fresh(lastAccelMs) || fresh(lastGyroMs) || fresh(lastRotMs))
                StreamState.ACTIVE else StreamState.IDLE
            "wifi" -> if (isWifiConnected()) StreamState.ACTIVE else StreamState.IDLE
            // camera: ACTIVE only while the vision provider truly analyzed a
            // frame within the freshness window; microphone: ACTIVE while the
            // hearing provider reads/recognizes audio; gps/bluetooth: idle
            // until their providers open one.
            "camera" -> if (fresh(PerceptionState.lastCameraFrameMs))
                StreamState.ACTIVE else StreamState.IDLE
            "microphone" -> if (fresh(PerceptionState.lastMicActivityMs))
                StreamState.ACTIVE else StreamState.IDLE
            else -> StreamState.IDLE
        }
    }

    fun detail(id: String): String = when (id) {
        "gps" -> try {
            val lm = context.getSystemService(Context.LOCATION_SERVICE) as LocationManager
            if (lm.isProviderEnabled(LocationManager.GPS_PROVIDER)) "gps provider on" else "gps off"
        } catch (_: Exception) { "" }
        "imu" -> "accel+gyro+rotvec"
        "accelerometer" -> "SENSOR_DELAY_NORMAL"
        "gyroscope" -> "SENSOR_DELAY_NORMAL"
        else -> ""
    }

    fun snapshot(): List<Capability> = IDS.map { id ->
        Capability(id, permissionState(id), hasHardware(id), streamState(id), detail(id))
    }

    fun signature(): String = snapshot().joinToString("|") { it.signature() }

    private fun isWifiConnected(): Boolean = try {
        val cm = context.getSystemService(Context.CONNECTIVITY_SERVICE) as ConnectivityManager
        val caps = cm.getNetworkCapabilities(cm.activeNetwork) ?: return false
        caps.hasTransport(NetworkCapabilities.TRANSPORT_WIFI) &&
            caps.hasCapability(NetworkCapabilities.NET_CAPABILITY_VALIDATED)
    } catch (_: Exception) {
        false
    }
}

