// BluetoothTransport.kt — v1.22 Bluetooth RFCOMM transport.
package com.samurai.shugocore.runtime
import android.bluetooth.BluetoothAdapter
import android.bluetooth.BluetoothDevice
import android.bluetooth.BluetoothServerSocket
import android.bluetooth.BluetoothSocket
import android.content.Context
import android.util.Log
import org.json.JSONObject
import java.io.BufferedReader
import java.io.InputStreamReader
import java.io.OutputStreamWriter
import java.util.UUID
import java.util.concurrent.ConcurrentHashMap
import java.util.concurrent.CopyOnWriteArrayList

data class BluetoothMessage(val json: JSONObject, val deviceId: String, val receivedMs: Long)
class BluetoothTransport(private val context: Context, private val serviceUuid: UUID) {
    companion object {
        private const val TAG = "BluetoothTransport"
        private const val SDP_NAME = "ShugoCore Mesh"
    }
    interface Listener {
        fun onMessageReceived(msg: BluetoothMessage)
        fun onDeviceConnected(deviceId: String)
        fun onDeviceDisconnected(deviceId: String)
    }
    private var listener: Listener? = null
    private val bluetoothAdapter: BluetoothAdapter? = BluetoothAdapter.getDefaultAdapter()
    private var serverSocket: BluetoothServerSocket? = null
    private val connectedSockets = ConcurrentHashMap<String, ConnectedPeer>()
    private val acceptThreads = CopyOnWriteArrayList<Thread>()
    fun setListener(l: Listener) { listener = l }
    fun startServer(): Boolean {
        val adapter = bluetoothAdapter ?: return false
        if (!adapter.isEnabled) return false
        try {
            serverSocket = adapter.listenUsingRfcommWithServiceRecord(SDP_NAME, serviceUuid)
            val thread = Thread(this::acceptLoop, "bt-mesh-accept")
            thread.isDaemon = true; thread.start(); acceptThreads.add(thread)
            Log.i(TAG, "Bluetooth server started")
            return true
        } catch (e: Exception) { Log.e(TAG, "start failed", e); return false }
    }
    fun connectToDevice(device: BluetoothDevice) {
        val id = device.address
        if (connectedSockets.containsKey(id)) return
        // v1.26 fix: socket.connect() is blocking (5-10 s).
        // Run on a background thread to avoid the foreground-service ANR.
        Thread {
            try {
                val socket = device.createRfcommSocketToServiceRecord(serviceUuid)
                socket.connect()
                val peer = ConnectedPeer(id, socket); connectedSockets[id] = peer
                peer.startReader(); listener?.onDeviceConnected(id)
                Log.i(TAG, "Connected to $id")
            } catch (e: Exception) { Log.e(TAG, "connect failed to $id", e) }
        }.start()
    }
    fun sendMessage(deviceId: String, json: JSONObject): Boolean {
        return connectedSockets[deviceId]?.send(json) ?: false
    }
    fun broadcastMessage(json: JSONObject): Int {
        var count = 0
        for ((id, peer) in connectedSockets) { if (peer.send(json)) count++ }
        return count
    }
    fun disconnectDevice(deviceId: String) {
        connectedSockets.remove(deviceId)?.close()
        listener?.onDeviceDisconnected(deviceId)
    }
    fun stop() {
        acceptThreads.forEach { it.interrupt() }; acceptThreads.clear()
        serverSocket?.close(); serverSocket = null
        for ((id, peer) in connectedSockets.toMap()) { peer.close(); connectedSockets.remove(id) }
        Log.i(TAG, "Bluetooth transport stopped")
    }
    fun connectedDevices(): Set<String> = connectedSockets.keys.toSet()
    fun isConnected(deviceId: String): Boolean = connectedSockets.containsKey(deviceId)
    private fun acceptLoop() {
        try {
            while (!Thread.currentThread().isInterrupted) {
                val socket = serverSocket?.accept() ?: break
                val id = socket.remoteDevice.address
                val peer = ConnectedPeer(id, socket); connectedSockets[id] = peer
                peer.startReader(); listener?.onDeviceConnected(id)
            }
        } catch (e: java.io.IOException) {
            if (e !is java.io.InterruptedIOException) Log.e(TAG, "accept error", e)
        }
    }
    inner class ConnectedPeer(val deviceId: String, private val socket: BluetoothSocket) {
        private val reader = Thread(this::readLoop, "bt-mesh-read-$deviceId").apply { isDaemon = true }
        private val writerLock = Any(); private var closed = false
        fun startReader() { reader.start() }
        fun send(json: JSONObject): Boolean {
            if (closed) return false
            try {
                synchronized(writerLock) {
                    val writer = OutputStreamWriter(socket.outputStream)
                    writer.write(json.toString() + "\\n"); writer.flush()
                }
                return true
            } catch (e: Exception) { Log.e(TAG, "send fail $deviceId", e); close(); return false }
        }
        fun close() { closed = true; try { socket.close() } catch (_: Exception) {} }
        private fun readLoop() {
            try {
                val rdr = BufferedReader(InputStreamReader(socket.inputStream), 4096)
                var line: String?
                while (!Thread.currentThread().isInterrupted && !closed) {
                    line = rdr.readLine() ?: break
                    listener?.onMessageReceived(BluetoothMessage(JSONObject(line), deviceId, System.currentTimeMillis()))
                }
            } catch (e: Exception) { if (!closed) Log.e(TAG, "read error $deviceId: ${e.message}") }
            finally { close() }
        }
    }
}
