"""
Tests for the host-side mobile fleet layer: pairing registry, topic ACL,
sensor ingestion sanitization, compute broker, execution handler, and the
engine consent gate for compute offload.
"""

import time
import unittest

from audit import AuditChain
from fallbacks import FallbackController
from memory_system import MemoryManager
from mobile_nodes import (
    MobileComputeBroker,
    MobileExecutionHandler,
    MobileNodeManager,
    MobileNodeRegistry,
    parse_mobile_topic,
)
from policy import CapabilityRegistry
from ros2_interface import StubROS2Interface
from state_machine import ExecutionGovernor


class _Harness:
    """Shared wiring: stub ROS, registry, manager, broker, fallbacks."""

    def __init__(self, allow=("pixel8",)):
        self.governor = ExecutionGovernor()
        self.fallbacks = FallbackController(governor=self.governor)
        self.audit = AuditChain("/tmp/test_mobile_audit.jsonl")
        self.registry = MobileNodeRegistry(audit=self.audit,
                                           heartbeat_timeout=0.2)
        self.caps = CapabilityRegistry({
            "mobile_devices_allowlist": list(allow)})
        self.ros2 = StubROS2Interface(rate_limit_hz=500.0)
        self.manager = MobileNodeManager(self.ros2, self.registry, self.caps,
                                         fallbacks=self.fallbacks,
                                         audit=self.audit)
        self.broker = MobileComputeBroker(self.ros2, self.registry, self.caps,
                                          audit=self.audit)


class TestTopicParsing(unittest.TestCase):
    def test_valid(self):
        self.assertEqual(parse_mobile_topic("/shugocore/mobile/pixel8/imu"),
                         ("pixel8", "imu"))

    def test_non_mobile_namespace(self):
        self.assertIsNone(parse_mobile_topic("/cmd_vel"))
        self.assertIsNone(parse_mobile_topic("/shugocore/other/imu"))
        self.assertIsNone(parse_mobile_topic("/shugocore/mobile/onlydevice"))


class TestRegistry(unittest.TestCase):
    def test_pair_and_unpair(self):
        h = _Harness()
        self.registry = h.registry
        h.registry.pair("pixel8", {"sensors": ["camera"]})
        self.assertTrue(h.registry.is_paired("pixel8"))
        self.assertTrue(h.registry.unpair("pixel8"))
        self.assertFalse(h.registry.is_paired("pixel8"))

    def test_pairing_ttl_expiry(self):
        h = _Harness()
        h.registry.pair("pixel8")
        # Force expiry by rewinding the clock.
        with h.registry._lock:
            h.registry._paired["pixel8"]["expires_at"] = time.time() - 1
        self.assertFalse(h.registry.is_paired("pixel8"))
        self.assertIn("pixel8", h.registry.expired())

    def test_heartbeat_liveness(self):
        h = _Harness()
        h.registry.pair("pixel8")
        self.assertTrue(h.registry.alive("pixel8"))
        h.registry._last_heartbeat["pixel8"] = time.monotonic() - 1.0
        self.assertFalse(h.registry.alive("pixel8"))
        self.assertTrue(h.registry.heartbeat("pixel8"))
        self.assertTrue(h.registry.alive("pixel8"))

    def test_heartbeat_ignored_for_unpaired(self):
        h = _Harness()
        self.assertFalse(h.registry.heartbeat("stranger"))

    def test_list_nodes_excludes_expired(self):
        h = _Harness()
        h.registry.pair("pixel8")
        h.registry.pair("tab4")
        with h.registry._lock:
            h.registry._paired["tab4"]["expires_at"] = time.time() - 1
        ids = [n["device_id"] for n in h.registry.list_nodes()]
        self.assertEqual(ids, ["pixel8"])


class TestTopicACL(unittest.TestCase):
    def test_unpaired_device_refused(self):
        caps = CapabilityRegistry()
        ok, reason = caps.validate_mobile_topic("stranger", "imu")
        self.assertFalse(ok)
        self.assertIn("allowlist", reason)

    def test_actuation_topic_refused(self):
        caps = CapabilityRegistry({"mobile_devices_allowlist": ["pixel8"]})
        ok, reason = caps.validate_mobile_topic("pixel8", "cmd_vel")
        self.assertFalse(ok)
        self.assertIn("outside the mobile contract", reason)

    def test_contract_topic_allowed(self):
        caps = CapabilityRegistry({"mobile_devices_allowlist": ["pixel8"]})
        ok, _ = caps.validate_mobile_topic("pixel8", "imu")
        self.assertTrue(ok)


class TestManagerIngestion(unittest.TestCase):
    def test_ingest_happy_path_and_snapshot(self):
        h = _Harness()
        h.registry.pair("pixel8")
        record = h.manager.ingest("pixel8", "imu", {"ax": 0.1, "ay": -0.2})
        self.assertIsNotNone(record)
        snap = h.manager.get_sensor_snapshot("pixel8")
        self.assertIn("imu", snap)
        self.assertEqual(snap["imu"]["data"]["ax"], 0.1)

    def test_unpaired_ingest_refused(self):
        h = _Harness()
        self.assertIsNone(h.manager.ingest("stranger", "imu", {"ax": 1}))

    def test_acl_refusal_escalates_after_repeats(self):
        h = _Harness()
        h.registry.pair("pixel8")
        for _ in range(3):
            result = h.manager.ingest("pixel8", "cmd_vel", {"x": 1})
            self.assertIsNone(result)  # actuation data never accepted
        self.assertEqual(h.fallbacks.mode, "paused")
        self.assertIn("mobile_sensor_anomaly", h.fallbacks.status()["violations"])

    def test_oversize_payload_refused(self):
        h = _Harness()
        h.registry.pair("pixel8")
        huge = {"blob": "x" * 10000}
        self.assertIsNone(h.manager.ingest("pixel8", "camera", huge))

    def test_heartbeat_updates_liveness(self):
        h = _Harness()
        h.registry.pair("pixel8")
        h.registry._last_heartbeat["pixel8"] = time.monotonic() - 1.0
        h.manager.ingest("pixel8", "heartbeat", {"seq": 1})
        self.assertTrue(h.registry.alive("pixel8"))

    def test_check_liveness_reports_lost_nodes(self):
        h = _Harness()
        h.registry.pair("pixel8")
        h.registry._last_heartbeat["pixel8"] = time.monotonic() - 1.0
        lost = h.manager.check_liveness()
        self.assertEqual(lost, ["pixel8"])
        self.assertEqual(h.fallbacks.mode, "paused")


class TestComputeBroker(unittest.TestCase):
    def test_unpaired_refused(self):
        h = _Harness()
        result = h.broker.request_compute("stranger", "vision", {})
        self.assertEqual(result["status"], "refused")

    def test_dead_device_refused(self):
        h = _Harness()
        h.registry.pair("pixel8")
        h.registry._last_heartbeat["pixel8"] = time.monotonic() - 1.0
        result = h.broker.request_compute("pixel8", "vision", {})
        self.assertEqual(result["status"], "refused")
        self.assertIn("heartbeat", result["reason"])

    def test_happy_path_with_correlated_result(self):
        h = _Harness()
        h.registry.pair("pixel8")
        result_box = {}

        def _offload():
            result_box["r"] = h.broker.request_compute(
                "pixel8", "vision", {"img": [1, 2]}, timeout=2.0)

        import threading
        worker = threading.Thread(target=_offload)
        worker.start()
        # Simulate the device: deliver a correlated result once published.
        deadline = time.time() + 2.0
        delivered = False
        while time.time() < deadline and not delivered:
            published = h.ros2.get_published_messages(
                "/shugocore/mobile/pixel8/compute_request")
            for entry in published:
                request = entry["message"]
                h.ros2._subscribers["/shugocore/mobile/pixel8/compute_result"](
                    {"request_id": request["request_id"],
                     "device_id": "pixel8", "detections": 7})
                delivered = True
                break
            time.sleep(0.01)
        worker.join(timeout=3.0)
        self.assertTrue(delivered)
        self.assertEqual(result_box["r"]["status"], "success")
        self.assertEqual(result_box["r"]["result"]["detections"], 7)

    def test_timeout_fails_closed(self):
        h = _Harness()
        h.registry.pair("pixel8")
        start = time.monotonic()
        result = h.broker.request_compute("pixel8", "vision", {}, timeout=0.1)
        self.assertEqual(result["status"], "error")
        self.assertIn("timeout", result["reason"])
        self.assertLess(time.monotonic() - start, 2.0)


class TestExecutionHandler(unittest.TestCase):
    def test_list_nodes(self):
        h = _Harness()
        h.registry.pair("pixel8", {"sensors": ["camera"]})
        handler = MobileExecutionHandler(h.manager, h.broker)
        result = handler.handle({"action_type": "mobile_list_nodes", "params": {}})
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["nodes"][0]["device_id"], "pixel8")

    def test_node_status_refused_for_unpaired(self):
        h = _Harness()
        handler = MobileExecutionHandler(h.manager, h.broker)
        result = handler.handle({"action_type": "mobile_node_status",
                                 "params": {"device_id": "stranger"}})
        self.assertEqual(result["status"], "refused")

    def test_unknown_action_refused(self):
        h = _Harness()
        handler = MobileExecutionHandler(h.manager, h.broker)
        result = handler.handle({"action_type": "mobile_launch_missile"})
        self.assertEqual(result["status"], "refused")


class TestEngineConsentGating(unittest.TestCase):
    """Compute offload is consent-gated like every other side effect."""

    def _engine(self):
        from decision_engine import DecisionEngine
        h = _Harness()
        h.registry.pair("pixel8")
        broker = h.broker
        handler = MobileExecutionHandler(h.manager, broker)
        engine = DecisionEngine(
            models=[{"id": "stub", "name": "stub", "backend": "stub"}],
            vector_db_config={"type": "chroma", "collection_name": "test_mobile"},
            audit_path="/tmp/test_mobile_engine_audit.jsonl",
            capabilities=h.caps,
            mobile_handler=handler)
        return engine, h

    def test_compute_request_refused_without_consent(self):
        engine, h = self._engine()
        allowed, reason = engine._gate_decision({
            "action_type": "mobile_request_compute",
            "params": {"device_id": "pixel8", "workload": "vision"}})
        self.assertFalse(allowed)
        self.assertIn("consent", reason)
        try:
            engine.shutdown()
        except Exception:
            pass

    def test_compute_request_allowed_with_consent_and_approval(self):
        engine, h = self._engine()
        engine.consents.grant("mobile_request_compute", granted_by="operator",
                              ttl_seconds=300)
        engine.approvals.attach_operator(lambda request: True)
        allowed, reason = engine._gate_decision({
            "action_type": "mobile_request_compute",
            "params": {"device_id": "pixel8", "workload": "vision"}})
        self.assertTrue(allowed, reason)
        try:
            engine.shutdown()
        except Exception:
            pass

    def test_read_actions_not_consent_gated(self):
        engine, h = self._engine()
        allowed, reason = engine._gate_decision({
            "action_type": "mobile_list_nodes", "params": {}})
        self.assertTrue(allowed, reason)
        try:
            engine.shutdown()
        except Exception:
            pass

    def test_known_action_types_include_mobile_and_robotics(self):
        from decision_engine import _KNOWN_ACTION_TYPES
        self.assertIn("mobile_request_compute", _KNOWN_ACTION_TYPES)
        self.assertIn("mobile_list_nodes", _KNOWN_ACTION_TYPES)
        self.assertIn("robot_navigate", _KNOWN_ACTION_TYPES)


if __name__ == "__main__":
    unittest.main()


