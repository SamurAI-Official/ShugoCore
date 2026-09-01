# ShugoCore Android reference client (Kotlin + Chaquopy + jros2)

This directory contains a minimal, reference-only Android app skeleton that
implements the exact bridge contract `android_bridge.py` and `android_runtime.py`
expect. It is NOT built in CI (no Android toolchain here) - the Python-side
stub tests in `tests/test_android.py` are the source of truth for the contract and
this shell mirrors them one-to-one.

Files:

- `settings.gradle.kts` - IHMC jros2 repository + Chaquopy.

- `app/build.gradle.kts` - jros2-android + Chaquopy + permissions packaging.



## Bridge contract (implement `ShugoCoreBridge` in Kotlin)

```kotlin
interface ShugoCoreBridge {
    // ROS 2 transport (jros2)
    fun createNode(name: String, domainId: Int): Any
    fun createPublisher(node: Any, topic: String, msgType: String, qos: Int): Any
    fun publish(pubHandle: Any, jsonPayload: String): Boolean
    fun createSubscriber(node: Any, topic: String, msgType: String, qos: Int): Any
    fun drainMessages(subHandle: Any): List<String>
    fun getSubscriptionCount(pubHandle: Any): Int
    fun closeHandle(handle: Any)
    fun destroyNode(node: Any)
    fun isAlive(): Boolean

    // Device state (AndroidRuntime)
    fun acquireWakeLock(timeoutMs: Int)
    fun acquireMulticastLock()
    fun getSecureSecret(name: String): String?
    fun getPowerStatus(): Map<String, Any>   // battery_level, plugged
    fun getThermalStatus(): Int                       // 0..2

    // Accelerator enumeration (AccelerationPolicy)
    fun enumerateAccelerators(): List<Map<String, Any>>  // kind, name, details

    // Compute (compute_node role)
    fun runInference(workload: String, payloadJson: String): Map<String, Any>

    // Sensors (sensor_node role)
    fun readSensor(kind: String): Map<String, Any>?
}
```

See `app/src/main/java/org/shugocore/bridge/kt` for the reference implementation
against jros2 + Android APIs (NNAPI accelerator listing, Keystore secrets,
battery/thermal).