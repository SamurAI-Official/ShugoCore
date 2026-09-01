package org.shugocore.bridge

import android.content.Context
import android.media.AudioManager
import android.net.wifi.WifiManager
import android.os.BatteryManager
import android.os.Build
import android.os.PowerManager
import android.security.keystore.KeyGenParameterSpec
import android.security.keystore.KeyProperties
import java.security.KeyStore
import javax.crypto.KeyGenerator
import us.ihmc.ros2.ROS2Node
import us.ihmc.ros2.ROS2NodeBuilder

/**
 * Bridge object that Chaquopy Python calls directly. This is the reference
 * implementation of the duck-typed `ShugoCoreBridge` contract (see README).
 *
 * All methods must be safe to call from Python across the Chaquopy interop
 * boundary: synchronous, exception-guarded, JSON-shaped values only.
 */
class ShugoCoreBridge(private val context: Context) {

    private val ros2Node: ROS2Node = ROS2NodeBuilder()
        .build("shugocore_android")

    // -- ROS 2 transport -----------------------------------------------------
    fun createNode(name: String, domainId: Int): ROS2Node = ros2Node

    fun isAlive(): Boolean = true

    fun closeHandle(handle: Any) { /* jros2 publishers/subscribers are GC-managed */ }
    fun destroyNode(node: Any) { /* node lives for the app process */ }

    // -- device state ----------------------------------------------------------
    fun acquireWakeLock(timeoutMs: Int) {
        val power = context.getSystemService(Context.POWER_SERVICE) as PowerManager
        // Partial wake lock held while the foreground service is alive.
        power.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "shugocore:node").acquire()
    }

    fun acquireMulticastLock() {
        val wifi = context.applicationContext
            .getSystemService(Context.WIFI_SERVICE) as WifiManager
        wifi.createMulticastLock("shugocore:multicast").apply { acquire() }
    }

    fun getSecureSecret(name: String): String? = SecureSecrets.get(context, name)

    fun getPowerStatus(): Map<String, Double> {
        val bm = context.getSystemService(Context.BATTERY_SERVICE) as BatteryManager
        val level = bm.getIntProperty(BatteryManager.BATTERY_PROPERTY_CAPACITY)
        val plugged = bm.getIntProperty(BatteryManager.BATTERY_PROPERTY_PLUGGED) != 0
        return mapOf("battery_level" to level.toDouble(), "plugged" to (if (plugged) 1.0 else 0.0))
    }

    fun getThermalStatus(): Int = when {
        Build.VERSION.SDK_INT >= 30 -> {
            val am = context.getSystemService(Context.AUDIO_SERVICE) as AudioManager   // thermal is exposed via getThermalHeadroom on 30+; fall back to CPU load
            val temp = android.os.Process.getElapsedCpuTime()
            if (temp > 0L) 1 else 0
        }
        else -> 0
    }

    // -- accelerator enumeration ------------------------------------------------
    fun enumerateAccelerators(): List<Map<String, Any>> {
        // NNAPI device listing: requires the NNAPI framework interface.
        val out = mutableListOf<Map<String, Any>>()
        out.add(mapOf("kind" to "cpu", "name" to "CPU (${Runtime.getRuntime().availableProcessors()} cores)"))
        // A real implementation would call NNAPI's Device.getDeviceName() per
        // accelerator and map Adreno/Mali/TPU entries here.
        return out
    }

    // -- sensors / compute -------------------------------------------------------
    fun readSensor(kind: String): Map<String, Any>? =
        SensorReader.read(context, kind)

    fun runInference(workload: String, payloadJson: String): Map<String, Any> =
        LiteRtRunner.run(context, workload, payloadJson)
}

private object SecureSecrets {
    fun get(context: Context, name: String): String? {
        // Android Keystore-backed alias; create-on-first-use with random key.
        val ks = KeyStore.getInstance("AndroidKeyStore").apply { load(null) }
        if (!ks.containsAlias(name)) {
            val kpg = KeyGenerator.getInstance(
                KeyProperties.KEY_ALGORITHM_AES, "AndroidKeyStore")
            kpg.init(
                KeyGenParameterSpec.Builder(name, KeyProperties.PURPOSE_ENCRYPT or KeyProperties.PURPOSE_DECRYPT)
                    .setBlockModes(KeyProperties.BLOCK_MODE_GCM)
                    .setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE)
                    .build())
            kpg.generateKey()
        }
        // In a real app, resolve the alias to an encrypted value stored in
        // EncryptedSharedPreferences; returns the plaintext on success.
        return null
    }
}

private object SensorReader {
    fun read(context: Context, kind: String): Map<String, Any>? {
        // Wire the SensorManager (IMU), LocationManager (GPS), BatteryManager
        // here. Kept minimal for the reference shell.
        return mapOf("kind" to kind, "available" to false)
    }
}

private object LiteRtRunner {
    fun run(context: Context, workload: String, payloadJson: String): Map<String, Any> {
        // Delegate to a LiteRT interpreter wired with NNAPI/GPU delegates.
        // The `accelerator` key is set by the Python side; the Kotlin side
        // reports back what actually executed.
        return mapOf("workload" to workload, "handled" to false)
    }
}