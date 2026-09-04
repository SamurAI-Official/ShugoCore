"""
Tests for the Android integration: bridge transports, runtime layer, and
the on-device node - all against pure-Python fake bridge objects (no
Android runtime required).
"""

import json
import time
import unittest
from unittest import mock

from android_bridge import (
    JavaBridgeROS2Interface,
    RosBridgeInterface,
    message_from_payload,
    ros_type_of,
    to_payload,
)
from android_runtime import AndroidRuntime, SecureStoreSecretProvider
from android_node import (
    AndroidShugoCoreNode,
    NodeConfig,
    detect_local_launcher,
    mobile_topic,
)
from acceleration import AccelerationPolicy
from fallbacks import FallbackController
from policy import CapabilityRegistry
from ros2_interface import StubROS2Interface, Twist, Vector3
from state_machine import ExecutionGovernor

from android_inference import AndroidBackend


class _FakeJBridge:
    """Pure-Python stand-in for the Kotlin jros2 bridge object."""

    def __init__(self):
        self.alive = True
        self.nodes = []
        self.publishers = {}
        self.published = []      # (topic, payload_dict)
        self.subscribers = {}    # handle -> {"topic": ..., "inbox": []}
        self.closed = []
        self.destroyed = []
        self._next = 0
        self.power_status = {"battery_level": 80, "plugged": False}
        self.thermal_status = 0

    def _handle(self):
        self._next += 1
        return f"h{self._next}"

    def createNode(self, name, domain_id):
        node = {"name": name, "domain_id": domain_id}
        self.nodes.append(node)
        return node

    def createPublisher(self, node, topic, msg_type, qos):
        handle = self._handle()
        self.publishers[handle] = {"topic": topic, "msg_type": msg_type}
        return handle

    def publish(self, handle, json_payload):
        entry = self.publishers.get(handle)
        if entry is None or not self.alive:
            return False
        self.published.append((entry["topic"], json.loads(json_payload)))
        return True

    def createSubscriber(self, node, topic, msg_type, qos):
        handle = self._handle()
        self.subscribers[handle] = {"topic": topic, "inbox": []}
        return handle

    def drainMessages(self, handle):
        return self.subscribers.get(handle, {}).get("inbox", [])

    def getSubscriptionCount(self, handle):
        return 1 if handle in self.publishers else 0

    def closeHandle(self, handle):
        self.closed.append(handle)

    def destroyNode(self, node):
        self.destroyed.append(node)

    def isAlive(self):
        return self.alive

    def acquireWakeLock(self, timeout_ms):
        self.wake_lock = timeout_ms

    def acquireMulticastLock(self):
        self.multicast_lock = True

    def getSecureSecret(self, name):
        return {"llm_api_key": "device-secret-123"}.get(name)

    def getPowerStatus(self):
        return self.power_status

    def getThermalStatus(self):
        return self.thermal_status

    def readSensor(self, kind):
        return {"kind": kind, "value": 42}

    def runInference(self, workload, payload_json):
        return {"workload": workload, "detections": 3}


class TestPayloadHelpers(unittest.TestCase):
    def test_ros_type_mapping(self):
        self.assertEqual(ros_type_of("Twist"), "geometry_msgs/Twist")
        self.assertEqual(ros_type_of("JointState"), "sensor_msgs/JointState")
        self.assertEqual(ros_type_of("unknown"), "std_msgs/String")

    def test_twist_round_trip(self):
        twist = Twist(linear=Vector3(1.0, 0.0, 0.0), angular=Vector3(0.0, 0.0, 0.5))
        rebuilt = message_from_payload(to_payload(twist))
        self.assertIsInstance(rebuilt, Twist)
        self.assertAlmostEqual(rebuilt.linear.x, 1.0)
        self.assertAlmostEqual(rebuilt.angular.z, 0.5)

    def test_malformed_payload_passthrough(self):
        payload = {"linear": "not-a-dict", "angular": {"z": 1}}
        self.assertEqual(message_from_payload(payload), payload)

    def test_joint_state_reconstruction(self):
        rebuilt = message_from_payload({"name": ["j1"], "position": [0.1],
                                        "velocity": [], "effort": []})
        self.assertEqual(rebuilt.name, ["j1"])
        self.assertAlmostEqual(rebuilt.position[0], 0.1)


class TestJavaBridgeTransport(unittest.TestCase):
    def _interface(self, bridge=None):
        bridge = bridge or _FakeJBridge()
        return JavaBridgeROS2Interface(bridge, domain_id=42,
                                       rate_limit_hz=100.0), bridge

    def test_publish_reaches_bridge_as_json(self):
        iface, bridge = self._interface()
        iface.create_publisher("/cmd_vel", "Twist")
        iface.publish("/cmd_vel", Twist(linear=Vector3(0.5, 0.0, 0.0)))
        topic, payload = bridge.published[0]
        self.assertEqual(topic, "/cmd_vel")
        self.assertAlmostEqual(payload["linear"]["x"], 0.5)
        iface.shutdown()

    def test_rate_limit_and_estop_bypass(self):
        iface, _ = self._interface()
        iface.create_publisher("/cmd_vel", "Twist")
        iface.publish("/cmd_vel", Twist(linear=Vector3(1.0, 0.0, 0.0)))
        iface.publish("/cmd_vel", Twist(linear=Vector3(1.0, 0.0, 0.0)))  # limited
        self.assertEqual(len(iface.get_published_messages()), 1)
        iface.publish("/cmd_vel", Twist())  # e-stop bypasses rate limiting
        self.assertEqual(len(iface.get_published_messages()), 2)
        iface.shutdown()

    def test_spin_once_dispatches_reconstructed_messages(self):
        iface, bridge = self._interface()
        received = []
        iface.create_subscriber("/scan_data", "std", received.append)
        handle = list(bridge.subscribers.keys())[0]
        bridge.subscribers[handle]["inbox"].append(json.dumps(
            {"linear": {"x": 0.1, "y": 0.0, "z": 0.0},
             "angular": {"x": 0.0, "y": 0.0, "z": 0.2}}))
        bridge.subscribers[handle]["inbox"].append("not-json")  # skipped
        iface.spin_once(timeout=0.0)
        self.assertEqual(len(received), 1)
        self.assertAlmostEqual(received[0].angular.z, 0.2)
        iface.shutdown()

    def test_shutdown_closes_handles_and_node(self):
        iface, bridge = self._interface()
        iface.create_publisher("/t", "Twist")
        iface.create_subscriber("/s", "std", lambda m: None)
        iface.shutdown()
        self.assertEqual(len(bridge.closed), 2)
        self.assertEqual(len(bridge.destroyed), 1)
        self.assertFalse(iface.is_available())

    def test_connection_health_follows_bridge(self):
        iface, bridge = self._interface()
        self.assertTrue(iface.check_connection())
        bridge.alive = False
        self.assertFalse(iface.check_connection())
        iface.shutdown()


class TestRosBridgeTransport(unittest.TestCase):
    def _fake_ws(self):
        class _FakeWS:
            def __init__(self):
                self.sent = []
                self.closed = False
                self.inbox = []

            def send(self, data):
                self.sent.append(json.loads(data))

            def settimeout(self, t):
                self.timeout = t

            def recv(self):
                if self.inbox:
                    return self.inbox.pop(0)
                raise TimeoutError("no message")

            def close(self):
                self.closed = True

        return _FakeWS()

    def test_advertise_and_publish_ops(self):
        ws = self._fake_ws()
        iface = RosBridgeInterface(ws_factory=lambda url, timeout: ws)
        iface.create_publisher("/cmd_vel", "Twist")
        iface.publish("/cmd_vel", Twist(linear=Vector3(0.3, 0.0, 0.0)))
        ops = ws.sent
        self.assertEqual(ops[0]["op"], "advertise")
        self.assertEqual(ops[0]["type"], "geometry_msgs/Twist")
        self.assertEqual(ops[1]["op"], "publish")
        self.assertAlmostEqual(ops[1]["msg"]["linear"]["x"], 0.3)
        iface.shutdown()

    def test_subscribe_and_dispatch(self):
        ws = self._fake_ws()
        iface = RosBridgeInterface(ws_factory=lambda url, timeout: ws)
        received = []
        iface.create_subscriber("/topic", "std", received.append)
        self.assertEqual(ws.sent[0]["op"], "subscribe")
        ws.inbox.append(json.dumps(
            {"op": "publish", "topic": "/topic",
             "msg": {"linear": {"x": 1.0, "y": 0, "z": 0},
                     "angular": {"x": 0, "y": 0, "z": 0}}}))
        iface.spin_once(timeout=0.05)
        self.assertEqual(len(received), 1)
        self.assertAlmostEqual(received[0].linear.x, 1.0)
        iface.shutdown()

    def test_shutdown_sends_unsubscribe(self):
        ws = self._fake_ws()
        iface = RosBridgeInterface(ws_factory=lambda url, timeout: ws)
        iface.create_subscriber("/topic", "std", lambda m: None)
        iface.shutdown()
        self.assertTrue(any(op["op"] == "unsubscribe" for op in ws.sent))
        self.assertTrue(ws.closed)


class TestAndroidRuntime(unittest.TestCase):
    def _runtime(self, bridge, **kwargs):
        governor = ExecutionGovernor()
        fallbacks = FallbackController(governor=governor)
        runtime = AndroidRuntime(bridge, fallbacks=fallbacks,
                                 acceleration=AccelerationPolicy(),
                                 secret_names=["llm_api_key"],
                                 power_poll_interval=5.0, **kwargs)
        return runtime, fallbacks

    def test_on_create_binds_secrets_and_locks(self):
        bridge = _FakeJBridge()
        runtime, _ = self._runtime(bridge)
        from security import SecretResolver
        secrets = SecretResolver()
        runtime.secrets = secrets
        runtime.on_create()
        self.assertEqual(secrets.get("llm_api_key"), "device-secret-123")
        self.assertTrue(bridge.multicast_lock)
        self.assertEqual(bridge.wake_lock, 0)
        self.assertEqual(runtime.state, "started")
        runtime.on_destroy()

    def test_on_pause_reports_and_on_resume_resumes(self):
        bridge = _FakeJBridge()
        runtime, fallbacks = self._runtime(bridge)
        runtime.on_create()
        runtime.on_pause()
        self.assertEqual(fallbacks.mode, "paused")
        runtime.on_resume()
        self.assertEqual(fallbacks.mode, "normal")
        runtime.on_destroy()

    def test_power_low_reports_fallback(self):
        bridge = _FakeJBridge()
        bridge.power_status = {"battery_level": 8, "plugged": False}
        runtime, fallbacks = self._runtime(bridge)
        runtime.on_create()
        runtime._check_power()
        self.assertEqual(fallbacks.mode, "paused")
        runtime.on_destroy()

    def test_power_low_skipped_when_plugged(self):
        bridge = _FakeJBridge()
        bridge.power_status = {"battery_level": 8, "plugged": True}
        runtime, fallbacks = self._runtime(bridge)
        runtime.on_create()
        runtime._check_power()
        self.assertEqual(fallbacks.mode, "normal")
        runtime.on_destroy()

    def test_thermal_demotes_and_pauses_on_streak(self):
        bridge = _FakeJBridge()
        bridge.thermal_status = 2
        runtime, fallbacks = self._runtime(bridge)
        runtime.on_create()
        runtime._check_thermal()
        runtime._check_thermal()  # streak of 2 -> pause
        self.assertEqual(fallbacks.mode, "paused")
        self.assertEqual(runtime.acceleration.thermal_level, 2)
        runtime.on_destroy()

    def test_missing_contract_methods_tolerated(self):
        class _MinimalBridge:
            def isAlive(self):
                return True
        runtime, fallbacks = self._runtime(_MinimalBridge())
        runtime.on_create()  # no locks, no secrets, no monitoring data
        runtime._check_power()
        runtime._check_thermal()
        self.assertEqual(fallbacks.mode, "normal")
        runtime.on_destroy()


class TestAndroidNode(unittest.TestCase):
    def _config(self, bridge=None, role="sensor_node", **kwargs):
        caps = CapabilityRegistry({"mobile_devices_allowlist": ["pixel8"]})
        return NodeConfig(device_id="pixel8", role=role, bridge=bridge,
                          capabilities=caps, **kwargs)

    def test_mobile_topic_format(self):
        self.assertEqual(mobile_topic("pixel8", "imu"),
                         "/shugocore/mobile/pixel8/imu")

    def test_invalid_role_rejected(self):
        with self.assertRaises(ValueError):
            NodeConfig(device_id="d", role="ninjutsu")

    def test_sensor_role_loop_publishes_sensors_and_heartbeat(self):
        bridge = _FakeJBridge()
        node = AndroidShugoCoreNode(self._config(bridge))
        node.start()
        time.sleep(0.25)  # allow a couple of loop iterations at 10Hz default
        node.stop()
        topics = {t for t, _ in bridge.published}
        self.assertTrue(any("/heartbeat" in t for t in topics))
        self.assertTrue(any("/battery" in t or "/imu" in t or "/gps" in t
                            for t in topics))

    def test_compute_role_executes_via_bridge(self):
        bridge = _FakeJBridge()
        node = AndroidShugoCoreNode(self._config(bridge, role="compute_node"))
        node.start()
        time.sleep(0.15)
        # Deliver a compute request through the bridge inbox.
        handle = next(h for h, s in bridge.subscribers.items()
                      if s["topic"].endswith("/compute_request"))
        bridge.subscribers[handle]["inbox"].append(json.dumps(
            {"request_id": "r-123", "workload": "vision", "payload": {"img": [1]}}))
        deadline = time.time() + 2.0
        results = []
        while time.time() < deadline and not results:
            topics = [(t, p) for t, p in bridge.published
                      if t.endswith("/compute_result")]
            if topics:
                results = [p for t, p in topics if p.get("request_id") == "r-123"]
                break
            time.sleep(0.05)
        node.stop()
        self.assertTrue(results, "compute result never published")
        self.assertEqual(results[0]["detections"], 3)
        self.assertEqual(results[0]["accelerator"], "cpu")  # stub host: CPU ladder

    def test_operator_teleop_is_clamped_and_relayed(self):
        bridge = _FakeJBridge()
        node = AndroidShugoCoreNode(self._config(bridge, role="operator_node"))
        node.start()
        time.sleep(0.15)
        handle = next(h for h, s in bridge.subscribers.items()
                      if s["topic"].endswith("/teleop"))
        bridge.subscribers[handle]["inbox"].append(json.dumps(
            {"linear": {"x": 99.0, "y": 0.0, "z": 5.0}, "angular": {"z": 42.0}}))
        deadline = time.time() + 2.0
        relayed = []
        while time.time() < deadline and not relayed:
            relayed = [p for t, p in bridge.published
                       if t == "/shugocore/teleop_relay"]
            time.sleep(0.05)
        node.stop()
        self.assertTrue(relayed, "teleop never relayed")
        # z-linear is forced to 0; both clamped to +/-1.0 / +/-1.0 defaults
        self.assertLessEqual(abs(relayed[0]["linear"]["x"]), 1.0)
        self.assertEqual(relayed[0]["linear"]["z"], 0.0)
        self.assertLessEqual(abs(relayed[0]["angular"]["z"]), 1.0)

    def test_full_agent_boot_with_stub_fallback(self):
        bridge = _FakeJBridge()
        with mock.patch("android_node.detect_local_launcher", return_value=None):
            node = AndroidShugoCoreNode(self._config(bridge, role="full_agent"))
            node.start()
            time.sleep(0.1)
            result = node.run_autonomous_task(
                {"type": "research", "description": "offline self check"})
            node.stop()
            try:
                node.engine.shutdown()
            except Exception:
                pass
        self.assertIsNotNone(node.engine)
        self.assertIn("status", result)

    def test_run_autonomous_task_before_assembly(self):
        bridge = _FakeJBridge()
        node = AndroidShugoCoreNode(self._config(bridge, role="sensor_node"))
        result = node.run_autonomous_task({"type": "research"})
        self.assertEqual(result["status"], "error")
        node.stop()


class TestLauncherDetection(unittest.TestCase):
    def test_first_responder_wins(self):
        probes = {"http://127.0.0.1:11434/api/tags": False,
                  "http://127.0.0.1:8080/health": True}
        found = detect_local_launcher(prober=lambda url: probes.get(url, False))
        self.assertEqual(found["launcher"], "llama.cpp")
        self.assertEqual(found["api"], "openai")

    def test_none_when_all_down(self):
        found = detect_local_launcher(prober=lambda url: False)
        self.assertIsNone(found)


class AndroidInferenceTestCase(unittest.TestCase):
    """Tests for the on-device LocalApiServer-backed model backend."""

    def test_android_backend_imports(self):
        self.assertIsNotNone(AndroidBackend)

    def test_android_backend_has_required_methods(self):
        self.assertTrue(hasattr(AndroidBackend, "generate"))
        self.assertTrue(hasattr(AndroidBackend, "chat"))
        self.assertTrue(hasattr(AndroidBackend, "list_models"))
        self.assertTrue(hasattr(AndroidBackend, "get_health"))

    def test_android_backend_initialization(self):
        backend = AndroidBackend(
            api_url="http://127.0.0.1:11434",
            model_name="test-model",
            device_caps={"soc": "Snapdragon", "ram": 8},
        )
        self.assertEqual(backend.model_name, "test-model")


if __name__ == "__main__":
    unittest.main()



