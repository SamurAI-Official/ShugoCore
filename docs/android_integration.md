# Android Compute Node Integration

ShugoCore treats Android devices as first-class ROS 2 compute nodes in the
service of a host ShugoCore engine. A phone can contribute sensors, on-device
ML (LiteRT/NNAPI), teleop input, or - with an offline model launcher - run a
full agent entirely on-device.

This document is the contract both sides implement. The host side lives in
`mobile_nodes.py`; the device side lives in `android_node.py` with transports
in `android_bridge.py` and lifecycle/power handling in `android_runtime.py`.

## Architecture

A single Android app process hosts two cooperating stacks:

- **Kotlin app shell** - foreground service, WakeLock, MulticastLock, Android
  Keystore access, and the `jros2` (Fast-DDS) native transport.
- **Chaquopy (CPython)** - the unmodified ShugoCore stdlib-only core plus the
  node runtime, telemetry, and accelerator policy.

The phone joins the ROS 2 graph over Fast-DDS (`ROS_DOMAIN_ID`). Because
jros2 rides Fast-DDS while the iPhone/swift-ros2 stack rides Zenoh, **the
wire is DDS-only** - iOS is deliberately not a compute node.

```
+---------------------- Android app (one process) ----------------------+
|  Kotlin shell : ForegroundService + WakeLock + MulticastLock + Keystore|
|    +-- jros2 (Fast-DDS native) : ROS2Node / pub / sub -------------+  |
|    +-- Chaquopy (CPython) : ShugoCore node runtime + acceleration -+  |
+-----------------------------+------------------------------------------+
                              | Fast-DDS wire (RTPS, ROS_DOMAIN_ID)
                              v
  ROS 2 graph : host ShugoCore engine + robots + MoveIt2 + Gazebo
```

## Roles

```
python3 android_node.py --role <role> --device-id <id> [--domain N]
```

| Role | What it does | ROS 2 surface |
|------|--------------|---------------|
| `sensor_node` | publishes phone sensors + heartbeats | `/shugocore/mobile/{id}/{sensor}` |
| `compute_node` | answers compute requests (LiteRT/NNAPI via Kotlin) | `.../compute_request` + `.../compute_result` |
| `operator_node` | clamps teleop axes and relays to the host gate | `.../teleop` -> `/shugocore/teleop_relay` |
| `full_agent` | complete engine on an offline local model | heartbeats + gated task execution |

Teleop NEVER writes actuation topics directly: it lands on
`/shugocore/teleop_relay`, which the host engine gates like any other action.
## Transports

`BaseROS2Interface` implementations (all interchangeable by the handler/engine):

| Transport | Where | When |
|----------|-------|------|
| `JavaBridgeROS2Interface` | Chaquopy app (in-process jros2/Fast-DDS) | **primary** - one process, native DDS, no extra Python deps |
| `RosBridgeInterface` | Termux (JSON over WebSocket to a `rosbridge_suite` server) | fallback/dev - requires optional `websocket-client` |
| `StubROS2Interface` | anywhere | fully-offline development/tests |

## Wire contract (topics)

A paired device may surface data ONLY on
`/shugocore/mobile/{device_id}/{tail}` (enforced host-side by the topic ACL).

| tail | Type | Direction | Purpose |
|------|------|-----------|---------|
| `heartbeat` | JSON | device -> host | liveness (2s) |
| `battery` `imu` `gps` `camera` | JSON | device -> host | sensor payloads |
| `teleop` | JSON twist-like | device -> host | operator intent |
| `compute_request` | JSON | host -> device | compute offload |
| `compute_result` | JSON | device -> host | correlated result (`request_id`) |

Reference topics used by the host relay: `/shugocore/teleop_relay` (Twist, gated).

## Pairing flow (host-side)

1. Operator pairs: `registry.pair(device_id, manifest)` (TTL 12h default, audited).
2. Host subscribes per contract topic via `manager.subscribe_device(device_id)`.

3. Device publishes heartbeats; `alive()` confirms liveness (30s timeout).
4. Lost nodes escalate `mobile_node_lost` to the fallback controller (pause).

## Security model

With DDS Security unavailable on every stack today, trust is established at the
application layer:

- **Pairing = consent**: only operator-allowlisted device_ids are accepted. Sensor
  ingestion needs no per-call approval; compute offload (which leaves the host and
  runs on a personal device(is consent-gated + approval-gated by the engine like any
   other side effect (it is in `MOBILE_ACTION_TYPES`).
- **Topic ACL**: actuation topics are unreachable by construction - inbound data on
  any other topic is refused and audited. Repeated refusals escalate
   `mobile_sensor_anomaly`.
- **Sanitization**: every payload is bounded (oversize rejected, strings sanitized,
  containers capped, junk rejected) before it reaches memory or decisions.

- **Loopback-only model endpoints**: on-device inference must be localhost on an
  allowlisted port (`CapabilityRegistry.validate_model_endpoint`). This prevents
   launcher misconfiguration from exfiltrating prompts to arbitrary hosts.

## Offline AI launchers (full_agent role)

| Launcher | Where | API | Ports probed |
|----------|-------|-----|--------------|
| Ollama | Termux (community builds) | native Ollama HTTP | 11434 |
| llama.cpp `llama-server` | Termux | OpenAI-compatible | 8080, 8081 |
| LM Studio | desktop/emulator | OpenAI-compatible | 1234 |

The node probes loopback ports at startup (`detect_local_launcher()`, audited)and
builds the backend from the existing HTTP backend adapters - no new model code
needed. When no launcher responds, `full_agent` falls back to the deterministic
stub backend and everything (governance, memory, gating) still works offline.

## Accelerator usage (NPU / DSP / GPU / CPU)

`AccelerationPolicy` resolves workload classes (llm, vision, audio, general) onto
detected devices in strict preference order, always ending with CPU als fallback:

| Workload | Preferred ladder |
|----------|------------------|
| LLM | NPU > GPU > CPU |
| Vision | NPU > DSP > GPU > CPU |
| Audio | DSP > NPU > GPU > CPU |
| General | NPU > GPU > CPU |

- Android enumeration goes through the Kotlin bridge (`enumerateAccelerators()`), which
  reports NNAPI devices, SOC model, Vulkan/OpenCL availability from Build APIs.
- SoC cheat-sheet: Qualcomm Snapdragon -> Hexagon (NSP+HTB); Google Tensor -> TPU
  via NNAPI; MediaTek Dimensity -> APU (NeuroPilot); Exynos -> NPU.
- Failure semantics: a failing accelerator is demoted (audited)and the ladder falls
  through; thermal level 2 demotes everything except CPU; the runtime pauses on
  `device_thermal_high` streaks.

## Termux (rosbridge) fallback

Without the app shell, run the node as a plain Python process and bridge through
a `rosbridge_suite` server on the LAN:

```
python3 android_node.py --role sensor_node --device-id pixel8 --rosbridge-host 192.168.1.50
```

Requires `pip install websocket-client` (only optional Python dep in this whole
integration; all other functionality is stdlib-only).

## Android app-shell checklist (Kotlin + Chaquopy)

- Gradle: AGP 8.0+, Java 17, `us.ihmc:jros2-android:1.5.1`;
  Chaquopy for the Python runtime; repository: the IHMC maven mirror +
  mavenCentral.

- Permissions: `INTERNET`, `ACCESS_NETWORK_STATE`,
  `CHANGE_WIFI_MULTICAST_STATE`; foreground-service for sustained operation;
  WakeLock + MulticastLock via the bridge (`acquireWakeLock`,
   `acquireMulticastLock`).
- Lifecycle: create node/pub/sub off-main-thread; close in `onDestroy`;
  ShugoCore `AndroidRuntime.on_create/on_pause/on_resume/on_destroy` maps
   app lifecycle to the governor/fallback machinery.

- Secrets: store API keys in Android Keystore;the bridge exposes
  `getSecureSecret(name)` and `SecureStoreSecretProvider` injections them into
   `SecretResolver` ons on_create - no env vars exist in the app sandbox.

- Battery/thermal: `getPowerStatus()` / `getThermalStatus()` bridge methods feed
  the monitor loop (`power_poll_interval`, default 30s); low battery or thermal
   streaks pause the engine via the fallback controller; plugged-in operation is
   recommended for permanent installs (camera streaming drains batteries.Battery/thermal monitors).
