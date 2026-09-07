// DeviceMeshManager.kt — v1.22 Bluetooth mesh peer registry.
//
// Uses the paired-device model: users pair devices via Android Settings
// (standard, reliable Bluetooth pairing). The app shows paired devices
// and connects via RFCOMM on demand. No discovery scanning.
package com.samurai.shugocore.runtime

import android.bluetooth.BluetoothAdapter
import android.bluetooth.BluetoothDevice
import android.content.Context
import android.util.Log
import org.json.JSONObject
import java.util.UUID
import java.util.concurrent.ConcurrentHashMap

data class MeshPeer(
    val deviceId: String,
    var name: String,
    var role: String = "primary",
    var capabilities: Set<String> = emptySet(),
    var lastSeenMs: Long = 0L,
    var online: Boolean = true,
    val sensorStatus: MutableMap<String, String> = ConcurrentHashMap(),
)

class DeviceMeshManager(private val context: Context) {
    companion object {
        private const val TAG = "DeviceMesh"
        val MESH_SERVICE_UUID = UUID.fromString("00001101-0000-1000-8000-00805F9B34FB")
    }
    var onPeerConnected: ((String) -> Unit)? = null
    var onPeerDisconnected: ((String) -> Unit)? = null
    var onPeerMessage: ((String, JSONObject) -> Unit)? = null
    private val transport = BluetoothTransport(context, MESH_SERVICE_UUID)
    private val peers = ConcurrentHashMap<String, MeshPeer>()
    private val bluetoothAdapter: BluetoothAdapter? = BluetoothAdapter.getDefaultAdapter()
    var role: String = "primary"

    fun start(): Boolean {
        transport.setListener(object : BluetoothTransport.Listener {
            override fun onMessageReceived(msg: BluetoothMessage) = handleMessage(msg.deviceId, msg.json)
            override fun onDeviceConnected(id: String) {
                peers.getOrPut(id) { MeshPeer(id, id) }.online = true
                onPeerConnected?.invoke(id)
            }
            override fun onDeviceDisconnected(id: String) {
                peers[id]?.online = false
                onPeerDisconnected?.invoke(id)
            }
        })
        return transport.startServer()
    }

    fun stop() { transport.stop(); peers.clear() }
    fun getPeers(): List<MeshPeer> = peers.values.toList()
    fun getPeerCount(): Int = peers.count { it.value.online }
    fun getPeer(deviceId: String): MeshPeer? = peers[deviceId]
    fun sendToPeer(deviceId: String, json: JSONObject) = transport.sendMessage(deviceId, json)
    fun broadcastToPeers(json: JSONObject) = transport.broadcastMessage(json)

    /** Returns paired Bluetooth devices (paired via Android Settings). */
    fun getPairedDevices(): List<BluetoothDevice> {
        return try {
            bluetoothAdapter?.bondedDevices?.toList() ?: emptyList()
        } catch (e: Exception) {
            Log.e(TAG, "getPairedDevices failed", e)
            emptyList()
        }
    }

    fun announceSelf(role: String = this.role) {
        val msg = JSONObject().apply {
            put("type", "device_announce")
            put("role", role)
            put("device_id", bluetoothAdapter?.address ?: "unknown")
            put("device_name", android.os.Build.MODEL)
            put("capabilities", JSONObject().apply {
                put("camera", context.packageManager.hasSystemFeature(android.content.pm.PackageManager.FEATURE_CAMERA_ANY))
                put("microphone", context.packageManager.hasSystemFeature(android.content.pm.PackageManager.FEATURE_MICROPHONE))
                put("compute", true)
            })
        }
        broadcastToPeers(msg)
    }

    private fun handleMessage(deviceId: String, json: JSONObject) {
        val type = json.optString("type")
        val peer = peers.getOrPut(deviceId) { MeshPeer(deviceId, deviceId) }
        peer.lastSeenMs = System.currentTimeMillis()
        peer.online = true
        when (type) {
            "device_announce" -> {
                peer.name = json.optString("device_name", deviceId)
                peer.role = json.optString("role", "primary")
                val caps = json.optJSONObject("capabilities")
                if (caps != null) {
                    peer.capabilities = caps.keys().asSequence().filter { caps.optBoolean(it) }.toSet()
                }
                announceSelf()
            }
            "sensor/camera" -> {
                peer.sensorStatus["camera"] = json.optString("status")
                json.optJSONObject("payload")?.let { PerceptionState.stampRemoteCamera(deviceId, it) }
            }
            "sensor/mic" -> {
                peer.sensorStatus["mic"] = json.optString("status")
                json.optJSONObject("payload")?.let { PerceptionState.stampRemoteMic(deviceId, it) }
            }
            "sensor/compute" -> peer.sensorStatus["compute"] = json.optString("status")
            "heartbeat" -> {}
        }
        onPeerMessage?.invoke(deviceId, json)
    }

    fun autoConnectPaired() {
        Thread {
            for (device in getPairedDevices()) {
                if (!peers.containsKey(device.address)) connectToPeer(device)
            }
        }.start()
    }

    fun connectToPeer(device: BluetoothDevice) {
        transport.connectToDevice(device)
    }

    fun disconnectPeer(deviceId: String) {
        transport.disconnectDevice(deviceId)
        peers.remove(deviceId)
    }
}
