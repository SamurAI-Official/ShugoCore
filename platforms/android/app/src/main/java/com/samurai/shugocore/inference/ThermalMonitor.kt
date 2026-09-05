// ThermalMonitor.kt
package com.samurai.shugocore.inference

import android.content.Context
import android.os.BatteryManager
import android.os.Build
import android.hardware.Sensor
import android.hardware.SensorEvent
import android.hardware.SensorEventListener
import android.hardware.SensorManager
import android.util.Log
import java.io.File

data class ThermalInfo(
    val state: ThermalState,
    val batteryLevel: Int,
    val isCharging: Boolean,
    val cpuTemp: Float?,
    val throttledGhz: Boolean
)

enum class ThermalState { NONE, MILD, MODERATE, SEVERE, CRITICAL }

data class InferenceConfig(
    val nGpuLayers: Int = 0,
    val nCtx: Int = 2048,
    val maxTokens: Int = 512,
    val throttled: Boolean = false,
    val shouldPause: Boolean = false,
    val shouldShutdown: Boolean = false
)

class ThermalMonitor(private val context: Context) {
    private val TAG = "ThermalMonitor"
    private val sensorManager: SensorManager =
        context.getSystemService(Context.SENSOR_SERVICE) as SensorManager
    private var accelX = 0.0f
    private var accelY = 0.0f
    private var accelZ = 0.0f
    private val accelListener = object : SensorEventListener {
        override fun onSensorChanged(e: SensorEvent?) {
            e?.values?.let { if (it.size >= 3) { accelX = it[0]; accelY = it[1]; accelZ = it[2] } }
        }
        override fun onAccuracyChanged(sensor: Sensor?, accuracy: Int) {}
    }

    init {
        sensorManager.getDefaultSensor(Sensor.TYPE_ACCELEROMETER)?.also {
            sensorManager.registerListener(accelListener, it, SensorManager.SENSOR_DELAY_NORMAL)
        }
    }

    fun getThermalInfo(): ThermalInfo {
        val batteryLevel = getBatteryLevel()
        val isCharging = isDeviceCharging()
        val cpuTemp = getCpuTemperature()
        val state = determineThermalState(batteryLevel, cpuTemp, isCharging)
        return ThermalInfo(state, batteryLevel, isCharging, cpuTemp,
            state.ordinal >= ThermalState.MODERATE.ordinal)
    }

    fun getInferenceConfig(): InferenceConfig = when (getThermalInfo().state) {
        ThermalState.NONE -> InferenceConfig(99, 2048, 512, false)
        ThermalState.MILD -> InferenceConfig(35, 2048, 512, true)
        ThermalState.MODERATE -> InferenceConfig(20, 1024, 256, true)
        ThermalState.SEVERE -> InferenceConfig(0, 512, 128, true, shouldPause = true)
        ThermalState.CRITICAL -> InferenceConfig(0, 128, 0, true, shouldPause = true, shouldShutdown = true)
    }

    /** Snapshot of phone sensors for the agent telemetry path (pyAgent.update_telemetry). */
    fun getTelemetryMap(): Map<String, Any> {
        val info = getThermalInfo()
        val am = context.getSystemService(Context.ACTIVITY_SERVICE) as android.app.ActivityManager
        val mem = android.app.ActivityManager.MemoryInfo()
        am.getMemoryInfo(mem)
        return mapOf(
            "battery_level" to info.batteryLevel,
            "is_charging" to info.isCharging,
            "cpu_temp_c" to (info.cpuTemp ?: 0f),
            "mem_total_mb" to (mem.totalMem / (1024L * 1024L)),
            "mem_avail_mb" to (mem.availMem / (1024L * 1024L)),
            "accel_x" to accelX.toDouble(),
            "accel_y" to accelY.toDouble(),
            "accel_z" to accelZ.toDouble(),
            "thermal_state" to info.state.name,
            "timestamp_ms" to System.currentTimeMillis(),
        )
    }

    private fun determineThermalState(battery: Int, temp: Float?, charging: Boolean) = when {
        battery < 10 || (temp != null && temp > 80f) -> ThermalState.CRITICAL
        battery < 15 || (temp != null && temp > 70f) -> ThermalState.SEVERE
        battery < 20 || (temp != null && temp > 60f) || (charging && temp != null && temp > 50f) -> ThermalState.MODERATE
        battery < 30 || (temp != null && temp > 50f) -> ThermalState.MILD
        else -> ThermalState.NONE
    }

    private fun getBatteryLevel(): Int {
        val bm = context.getSystemService(Context.BATTERY_SERVICE) as BatteryManager
        return if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.LOLLIPOP)
            bm.getIntProperty(BatteryManager.BATTERY_PROPERTY_CAPACITY)
        else 100
    }

    private fun isDeviceCharging(): Boolean {
        val filter = android.content.IntentFilter(android.content.Intent.ACTION_BATTERY_CHANGED)
        val status = context.registerReceiver(null, filter)
        val level = status?.getIntExtra(BatteryManager.EXTRA_STATUS, -1) ?: -1
        return level == BatteryManager.BATTERY_STATUS_CHARGING ||
               level == BatteryManager.BATTERY_STATUS_FULL
    }

    private fun getCpuTemperature(): Float? {
        val paths = listOf("/sys/class/thermal/thermal_zone0/temp",
                           "/sys/class/thermal/thermal_zone10/temp")
        paths.forEach { path ->
            val file = File(path)
            if (file.exists()) {
                return try { file.readText().trim().toFloat() / 1000f } catch (e: Exception) { null }
            }
        }
        return null
    }
}
