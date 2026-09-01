"""
ROS 2 transport stress tests for the Android bridges.

Targets JavaBridgeROS2Interface (Chaquopy/jros2 in-process) and
RosBridgeInterface (rosbridge JSON-over-WebSocket for Termux): bridge
death, post-shutdown behavior, garbage drains, concurrent publishes,
rate limiting with emergency-stop bypass, round-trip payload fidelity,
reconnect, and malformed-packet tolerance. Cross-transport parity proves
both paths sanitize identically.

FINDINGS (documented, not fixed - stress-test phase):
- to_payload() only carries Twist faithfully; JointState/LaserScan/
  JointTrajectory degrade to {"data": "<repr>"} because those classes
  lack to_dict(). message_from_payload() can rebuild them, so the
  encode/decode contract is asymmetric.
"""

import json
import os
import sys
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from android_bridge import (
    JavaBridgeROS2Interface,
    RosBridgeInterface,
    message_from_payload,
    to_payload,
)
from ros2_interface import (
    JointState,
    JointTrajectory,
    LaserScan,
    StubROS2Interface,
    Twist,
    Vector3,
)
from tests.test_android import _FakeJBridge


class _WSFake:
    """Minimal WebSocket stand-in for rosbridge tests."""

    def __init__(self, sink, fail_sends=False):
        self.sent = sink          # shared list of sent packets
        self.fail_sends = fail_sends
        self.closed = False
        self.inbox = []           # packets delivered by recv()
        self._timeout = None

    def send(self, data):
        if self.fail_sends:
            raise OSError("socket closed")
        self.sent.append(json.loads(data))

    def recv(self):
        if not self.inbox:
            raise TimeoutError("recv timeout")
        return json.dumps(self.inbox.pop(0))

    def settimeout(self, t):
        self._timeout = t

    def close(self):
        self.closed = True


class TestJavaBridgeTransport(unittest.TestCase):
    def setUp(self):
        self.bridge = _FakeJBridge()
        self.ros = JavaBridgeROS2Interface(self.bridge)

    def test_bridge_death_flips_availability_and_publish_noops(self):
        self.ros.create_publisher("/cmd_vel", "Twist")
        self.assertTrue(self.ros.is_available())
        self.bridge.alive = False
        self.assertFalse(self.ros.is_available())
        self.assertFalse(self.ros.check_connection())
        self.ros.publish("/cmd_vel", Twist(linear=Vector3(1, 0, 0),
                                           angular=Vector3(0, 0, 0)))
        self.assertEqual(self.bridge.published, [])

    def test_publish_after_shutdown_is_noop(self):
        self.ros.create_publisher("/cmd_vel", "Twist")
        self.ros.shutdown()
        self.ros.publish("/cmd_vel", Twist(linear=Vector3(1, 0, 0),
                                           angular=Vector3(0, 0, 0)))
        self.assertEqual(self.ros.get_published_messages(), [])
        self.ros.spin_once(0.01)              # must not raise
        self.ros.shutdown()                   # idempotent

    def test_garbage_drain_payloads_ignored(self):
        got = []
        self.ros.create_subscriber("/telemetry", "Twist", got.append)
        handle = next(iter(self.bridge.subscribers))
        inbox = self.bridge.subscribers[handle]["inbox"]
        inbox.append("not json at all")
        inbox.append(json.dumps({"linear": {"x": 1.0, "y": 0, "z": 0},
                                 "angular": {"x": 0, "y": 0, "z": 0}}))
        inbox.append(None)
        self.ros.spin_once(0.05)
        self.assertEqual(len(got), 2)         # garbage skipped, rest delivered
        self.assertIsInstance(got[0], Twist)

    def test_drain_bridge_exception_does_not_kill_spin(self):
        got = []
        self.ros.create_subscriber("/telemetry", "Twist", got.append)

        def boom(handle):
            raise RuntimeError("binder dead")

        self.bridge.drainMessages = boom
        self.ros.spin_once(0.05)              # must not raise
        self.assertEqual(got, [])

    def test_subscriber_callback_exception_isolated(self):
        def bad(_msg):
            raise ValueError("handler exploded")

        got = []
        self.ros.create_subscriber("/a", "Twist", bad)
        self.ros.create_subscriber("/b", "Twist", got.append)
        for entry in self.bridge.subscribers.values():
            if entry["topic"] == "/a":
                entry["inbox"].append(json.dumps(
                    {"linear": {"x": 0, "y": 0, "z": 0},
                     "angular": {"x": 0, "y": 0, "z": 0}}))
        self.ros.spin_once(0.05)
        self.assertEqual(got, [])             # /b untouched, no raise

    def test_unregistered_topic_publish_ignored(self):
        self.ros.publish("/ghost", Twist(linear=Vector3(1, 0, 0),
                                         angular=Vector3(0, 0, 0)))
        self.assertEqual(self.bridge.published, [])

    def test_invalid_trajectory_raises_through(self):
        self.ros.create_publisher("/traj", "JointTrajectory")
        with self.assertRaises(ValueError):
            self.ros.publish("/traj", JointTrajectory(joint_names=[], points=[]))

    def test_concurrent_publishes_no_corruption(self):
        self.ros.create_publisher("/cmd_vel", "Twist")
        errors = []

        def blast(tid):
            try:
                for i in range(40):
                    self.ros.publish("/cmd_vel", Twist(
                        linear=Vector3(float(tid), 0, 0),
                        angular=Vector3(0, 0, float(i))))
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=blast, args=(t,))
                   for t in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10.0)
        self.assertEqual(errors, [])
        logged = self.ros.get_published_messages("/cmd_vel")
        self.assertLessEqual(len(logged), 512)     # bounded introspection log
        for item in logged:
            self.assertIn("linear", item["message"].to_dict())

    def test_emergency_stop_bypasses_rate_limit(self):
        self.ros.create_publisher("/cmd_vel", "Twist")
        stop = Twist(linear=Vector3(0, 0, 0), angular=Vector3(0, 0, 0))
        for _ in range(5):
            self.ros.publish("/cmd_vel", stop)
        self.assertEqual(len(self.ros.get_published_messages("/cmd_vel")), 5)


class TestRosBridgeTransport(unittest.TestCase):
    def _make(self, fail_sends=False):
        sent = []
        ws = _WSFake(sent, fail_sends=fail_sends)
        ros = RosBridgeInterface(ws_factory=lambda url, timeout: ws)
        return ros, ws, sent

    def test_publish_advertises_once_and_sends_payload(self):
        ros, ws, sent = self._make()
        ros.create_publisher("/cmd_vel", "Twist")
        ros.publish("/cmd_vel", Twist(linear=Vector3(1, 0, 0),
                                      angular=Vector3(0, 0, 0.5)))
        time.sleep(0.05)   # clear the 30 Hz rate-limit window
        ros.publish("/cmd_vel", Twist(linear=Vector3(2, 0, 0),
                                      angular=Vector3(0, 0, 0.5)))
        ops = [s["op"] for s in sent]
        self.assertEqual(ops.count("advertise"), 1)
        self.assertEqual(ops.count("publish"), 2)
        self.assertEqual(sent[-1]["msg"]["linear"]["x"], 2.0)

    def test_connection_drop_reconnects_on_next_use(self):
        sent = []
        made = []

        def factory(url, timeout):
            ws = _WSFake(sent)
            made.append(ws)
            return ws

        ros = RosBridgeInterface(ws_factory=factory)
        ros.create_publisher("/cmd_vel", "Twist")
        stop = Twist(linear=Vector3(0, 0, 0), angular=Vector3(0, 0, 0))
        ros.publish("/cmd_vel", stop)             # connects ws #1
        made[0].fail_sends = True
        ros.publish("/cmd_vel", stop)             # send fails -> socket dropped
        ros.publish("/cmd_vel", stop)             # reconnects via ws #2
        self.assertEqual(len(made), 2)
        self.assertTrue(made[0].closed)
        self.assertEqual(sent[0]["op"], "advertise")
        self.assertEqual(sent[-1]["op"], "publish")

    def test_connect_failure_is_silent_noop(self):
        ros = RosBridgeInterface(
            ws_factory=lambda url, timeout: (_ for _ in ()).throw(OSError("refused")))
        ros.create_publisher("/cmd_vel", "Twist")
        ros.publish("/cmd_vel", Twist(linear=Vector3(1, 0, 0),
                                      angular=Vector3(0, 0, 0)))
        self.assertFalse(ros.is_available())

    def test_spin_ignores_garbage_and_non_publish_ops(self):
        ros, ws, sent = self._make()
        got = []
        ros.create_subscriber("/telemetry", "Twist", got.append)
        # _WSFake.recv json-serializes each inbox item, so feed dicts for
        # structured packets (double-encoded strings would parse back as
        # str, which is itself one of the malformed cases below).
        ws.inbox = ["garbage {{{", {"op": "status"},
                    {"op": "publish", "topic": "/telemetry",
                     "msg": {"linear": {"x": 0.5, "y": 0, "z": 0},
                             "angular": {"x": 0, "y": 0, "z": 0}}},
                    {"op": "publish", "topic": "/other", "msg": {}}]
        for _ in range(4):
            ros.spin_once(0.05)
        self.assertEqual(len(got), 1)
        self.assertIsInstance(got[0], Twist)
        self.assertEqual(got[0].linear.x, 0.5)

    def test_spin_without_connection_is_noop(self):
        ros, ws, sent = self._make()
        ros.spin_once(0.01)                       # never connected: no raise
        self.assertEqual(ws.inbox, [])

    def test_shutdown_unsubscribes_and_closes(self):
        ros, ws, sent = self._make()
        ros.create_subscriber("/t", "Twist", lambda m: None)
        ros.shutdown()
        self.assertEqual(sent[-1]["op"], "unsubscribe")
        self.assertTrue(ws.closed)
        self.assertFalse(ros.is_available())
        ros.shutdown()                            # idempotent

    def test_publish_after_shutdown_is_noop(self):
        ros, ws, sent = self._make()
        ros.create_publisher("/cmd_vel", "Twist")
        ros.shutdown()
        before = len(sent)
        ros.publish("/cmd_vel", Twist(linear=Vector3(1, 0, 0),
                                      angular=Vector3(0, 0, 0)))
        self.assertEqual(sent, [])                # nothing sent after shutdown


class TestPayloadFidelity(unittest.TestCase):
    def test_twist_round_trip(self):
        twist = Twist(linear=Vector3(1.5, -2.0, 0.0),
                      angular=Vector3(0.0, 0.1, -0.3))
        rebuilt = message_from_payload(to_payload(twist))
        self.assertIsInstance(rebuilt, Twist)
        self.assertEqual(to_payload(rebuilt), to_payload(twist))

    def test_non_twist_messages_degrade_to_string_payload(self):
        # FINDING: only Twist has to_dict(); everything else stringifies.
        for msg in (JointState(name=["j1"], position=[0.1]),
                    LaserScan(ranges=[0.5, 1.5]),
                    JointTrajectory(joint_names=["j1"], points=[])):
            payload = to_payload(msg)
            self.assertEqual(set(payload.keys()), {"data"}, type(msg).__name__)

    def test_nan_inf_twist_sanitized_on_publish_java(self):
        bridge = _FakeJBridge()
        ros = JavaBridgeROS2Interface(bridge)
        ros.create_publisher("/cmd_vel", "Twist")
        dirty = Twist(linear=Vector3(float("nan"), 0, float("inf")),
                      angular=Vector3(0, 0, 1e9))
        ros.publish("/cmd_vel", dirty)
        topic, payload = bridge.published[0]
        self.assertEqual(payload["linear"]["x"], 0.0)
        self.assertEqual(payload["linear"]["z"], 0.0)
        self.assertEqual(payload["angular"]["z"], 100.0)
        self.assertEqual(dirty.linear.x, 0.0)     # sanitized in place

    def test_malformed_payloads_degrade_never_raise(self):
        passthrough = [{}, {"name": "solo"}, [1, 2, 3], "raw string", None]
        for payload in passthrough:
            self.assertEqual(message_from_payload(payload), payload)
        # Twist-shaped but garbage values degrade back to the dict.
        degraded = message_from_payload(
            {"linear": {"x": "abc", "y": 0, "z": 0}, "angular": {}})
        self.assertEqual(degraded,
                         {"linear": {"x": "abc", "y": 0, "z": 0},
                          "angular": {}})
        rebuilt = message_from_payload(
            {"linear": {"x": 1.0, "y": 0, "z": 0},
             "angular": {"x": 0, "y": 0, "z": 2.0}})
        self.assertIsInstance(rebuilt, Twist)
        self.assertEqual(rebuilt.linear.x, 1.0)


class TestCrossTransportParity(unittest.TestCase):
    def _sanitized_java(self):
        bridge = _FakeJBridge()
        ros = JavaBridgeROS2Interface(bridge)
        ros.create_publisher("/cmd_vel", "Twist")
        ros.publish("/cmd_vel", Twist(linear=Vector3(float("nan"), 0, 500.0),
                                      angular=Vector3(0, 0, -500.0)))
        return bridge.published[0][1]

    def test_java_and_rosbridge_produce_identical_payloads(self):
        j_payload = self._sanitized_java()

        sent = []
        ros = RosBridgeInterface(ws_factory=lambda url, timeout: _WSFake(sent))
        ros.create_publisher("/cmd_vel", "Twist")
        ros.publish("/cmd_vel", Twist(linear=Vector3(float("nan"), 0, 500.0),
                                      angular=Vector3(0, 0, -500.0)))
        r_payload = sent[-1]["msg"]
        self.assertEqual(j_payload, r_payload)
        self.assertEqual(j_payload["linear"]["z"], 100.0)
        self.assertEqual(j_payload["angular"]["z"], -100.0)

    def test_stub_transport_sanitizes_identically(self):
        j_payload = self._sanitized_java()
        stub = StubROS2Interface()
        stub.create_publisher("/cmd_vel", "Twist")
        stub.publish("/cmd_vel", Twist(linear=Vector3(float("nan"), 0, 500.0),
                                       angular=Vector3(0, 0, -500.0)))
        s_msg = stub.get_published_messages("/cmd_vel")[0]["message"]
        self.assertEqual(to_payload(s_msg), j_payload)


class TestRateLimitingAndBounds(unittest.TestCase):
    def test_rate_limiter_suppresses_burst_java(self):
        bridge = _FakeJBridge()
        ros = JavaBridgeROS2Interface(bridge, rate_limit_hz=50.0)
        ros.create_publisher("/cmd_vel", "Twist")
        for i in range(200):
            ros.publish("/cmd_vel", Twist(linear=Vector3(float(i), 0, 0),
                                          angular=Vector3(0, 0, 0)))
        self.assertGreater(len(bridge.published), 0)
        self.assertLess(len(bridge.published), 200)

    def test_rate_limiter_suppresses_burst_rosbridge(self):
        sent = []
        ros = RosBridgeInterface(ws_factory=lambda url, timeout: _WSFake(sent),
                                 rate_limit_hz=50.0)
        ros.create_publisher("/cmd_vel", "Twist")
        for i in range(200):
            ros.publish("/cmd_vel", Twist(linear=Vector3(float(i), 0, 0),
                                          angular=Vector3(0, 0, 0)))
        publishes = [s for s in sent if s["op"] == "publish"]
        self.assertGreater(len(publishes), 0)
        self.assertLess(len(publishes), 200)

    def test_published_log_bounded_java(self):
        bridge = _FakeJBridge()
        ros = JavaBridgeROS2Interface(bridge)
        ros.create_publisher("/hb", "Twist")
        stop = Twist(linear=Vector3(0, 0, 0), angular=Vector3(0, 0, 0))
        for _ in range(700):   # emergency-stop bypasses the limiter
            ros.publish("/hb", stop)
        self.assertLessEqual(len(ros.get_published_messages()), 512)

    def test_subscription_count_survives_bridge_garbage(self):
        bridge = _FakeJBridge()
        ros = JavaBridgeROS2Interface(bridge)
        ros.create_publisher("/t", "Twist")
        self.assertEqual(ros.get_subscription_count("/t"), 1)
        bridge.getSubscriptionCount = lambda h: "not-a-number"
        self.assertEqual(ros.get_subscription_count("/t"), 0)
        self.assertEqual(ros.get_subscription_count("/unknown"), 0)

    def test_concurrent_rosbridge_publishes_no_crash(self):
        sent = []
        ros = RosBridgeInterface(ws_factory=lambda url, timeout: _WSFake(sent))
        ros.create_publisher("/cmd_vel", "Twist")
        stop = Twist(linear=Vector3(0, 0, 0), angular=Vector3(0, 0, 0))
        errors = []

        def blast():
            try:
                for _ in range(50):
                    ros.publish("/cmd_vel", stop)
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=blast) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10.0)
        self.assertEqual(errors, [])
        self.assertLessEqual(len(ros.get_published_messages()), 512)


if __name__ == "__main__":
    unittest.main()
