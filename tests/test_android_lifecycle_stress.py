"""
Android lifecycle and background stress tests.

Targets AndroidRuntime, AndroidShugoCoreNode, and SecureStoreSecretProvider
with churn, concurrency, soak, bridge-death, and power/thermal edge cases.
All scenarios use the pure-Python _FakeJBridge from test_android.py (no
Android runtime required); the runtime monitor is driven deterministically
by calling _check_power/_check_thermal directly where a wall-clock wait
would otherwise be needed.
"""

import os
import sys
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__))))

from acceleration import AccelerationPolicy
from android_node import AndroidShugoCoreNode, NodeConfig
from android_runtime import AndroidRuntime, SecureStoreSecretProvider
from fallbacks import FallbackController
from policy import CapabilityRegistry
from state_machine import ExecutionGovernor
from tests.test_android import _FakeJBridge


def _make_runtime(bridge, **kw):
    """Runtime with a real governor/fallback stack and accelerated polling."""
    governor = ExecutionGovernor()
    fallbacks = FallbackController(governor=governor)
    acceleration = AccelerationPolicy()
    runtime = AndroidRuntime(bridge, fallbacks=fallbacks,
                             acceleration=acceleration,
                             power_poll_interval=5.0, **kw)
    return runtime, fallbacks, acceleration


class _SecretSink:
    """Minimal SecretResolver stand-in capturing overrides."""

    def __init__(self):
        self.overrides = {}

    def set(self, name, value):
        self.overrides[str(name)] = str(value)


class TestLifecycleChurn(unittest.TestCase):
    def setUp(self):
        self.bridge = _FakeJBridge()
        self.rt, self.fallbacks, self.acc = _make_runtime(self.bridge)

    def test_200_full_lifecycle_cycles(self):
        baseline = threading.active_count()
        for _ in range(200):
            rt, _, _ = _make_runtime(_FakeJBridge())
            rt.on_create()
            self.assertEqual(rt.state, "started")
            rt.on_pause()
            self.assertEqual(rt.state, "paused")
            rt.on_resume()
            self.assertEqual(rt.state, "started")
            rt.on_destroy()
            self.assertEqual(rt.state, "destroyed")
        self.assertEqual(threading.active_count(), baseline)

    def test_concurrent_pause_resume_race(self):
        self.rt.on_create()
        errors = []

        def hammer(tid):
            try:
                for i in range(25):
                    if (tid + i) % 2 == 0:
                        self.rt.on_pause()
                    else:
                        self.rt.on_resume()
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=hammer, args=(t,))
                   for t in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5.0)
        self.assertEqual(errors, [])
        self.rt.on_destroy()
        self.assertEqual(self.rt.state, "destroyed")

    def test_monitor_thread_recreated_across_restarts(self):
        for _ in range(5):
            rt, _, _ = _make_runtime(_FakeJBridge())
            rt.on_create()
            self.assertIsNotNone(rt._monitor_thread)
            self.assertTrue(rt._monitor_thread.is_alive())
            rt.on_destroy()
            self.assertIsNone(rt._monitor_thread)

    def test_lifecycle_hooks_ignore_invalid_transitions(self):
        self.rt.on_resume()          # resume before start: no-op
        self.rt.on_pause()           # pause before start: no-op
        self.assertEqual(self.rt.state, "new")
        self.rt.on_create()
        self.rt.on_pause()
        self.rt.on_pause()           # double pause: no-op
        self.assertEqual(self.rt.state, "paused")
        self.rt.on_create()          # create while paused: no-op (no 2nd monitor)
        self.assertEqual(self.rt.state, "paused")

    def test_double_create_does_not_spawn_second_monitor(self):
        self.rt.on_create()
        first = self.rt._monitor_thread
        self.rt.on_create()
        self.assertIs(self.rt._monitor_thread, first)
        self.rt.on_destroy()

    def test_create_after_destroy_raises(self):
        self.rt.on_create()
        self.rt.on_destroy()
        with self.assertRaises(RuntimeError):
            self.rt.on_create()


class TestMonitorResilience(unittest.TestCase):
    """The monitor must degrade, never crash, on bridge contract failures."""

    def setUp(self):
        self.bridge = _FakeJBridge()
        self.rt, self.fallbacks, self.acc = _make_runtime(self.bridge)

    def test_bridge_raises_on_power_and_thermal(self):
        def boom():
            raise RuntimeError("binder transaction failed")

        self.bridge.getPowerStatus = boom
        self.bridge.getThermalStatus = boom
        for _ in range(50):
            self.rt._check_power()
            self.rt._check_thermal()
        self.assertFalse(self.rt._power_low_reported)
        self.assertEqual(self.rt._thermal_streak, 0)

    def test_bridge_returns_garbage_types(self):
        for state in ("hot", 3.99, {"battery": 1}, [2], True, b"1"):
            self.bridge.power_status = state
            self.bridge.thermal_status = state
            self.rt._check_power()
            self.rt._check_thermal()
        self.rt.on_create()
        self.assertEqual(self.rt.state, "started")

    def test_monitor_loop_survives_intermittent_failures(self):
        calls = {"n": 0}
        real_power = self.bridge.getPowerStatus

        def flaky():
            calls["n"] += 1
            if calls["n"] % 2 == 0:
                raise OSError("bridge busy")
            return real_power()

        self.bridge.getPowerStatus = flaky
        for _ in range(30):
            self.rt._check_power()
            self.rt._check_thermal()
        self.assertEqual(calls["n"], 30)


class TestPowerReportingEdgeCases(unittest.TestCase):
    def setUp(self):
        self.bridge = _FakeJBridge()
        self.rt, self.fallbacks, _ = _make_runtime(self.bridge)
        self.rt.fallbacks.report_violation = mock.Mock(
            wraps=self.rt.fallbacks.report_violation)

    def _power(self, battery, plugged=False):
        self.bridge.power_status = {"battery_level": battery,
                                    "plugged": plugged}
        self.rt._check_power()

    def test_low_battery_reported_once(self):
        self._power(5)
        self._power(3)
        self.assertEqual(self.rt.fallbacks.report_violation.call_count, 1)
        self.assertTrue(self.rt._power_low_reported)

    def test_recovery_then_re_report(self):
        self._power(5)
        self._power(90)
        self.assertFalse(self.rt._power_low_reported)
        self._power(4)
        self.assertEqual(self.rt.fallbacks.report_violation.call_count, 2)

    def test_plugged_never_reports(self):
        self._power(0, plugged=True)
        self._power(1, plugged=True)
        self.assertEqual(self.rt.fallbacks.report_violation.call_count, 0)

    def test_boundary_threshold(self):
        self._power(15)   # exactly at threshold: not low
        self.assertEqual(self.rt.fallbacks.report_violation.call_count, 0)
        self._power(14)   # below threshold: low
        self.assertEqual(self.rt.fallbacks.report_violation.call_count, 1)

    def test_garbage_battery_values_skipped(self):
        for bad in (None, "abc", float("nan"), {"level": 5}, [10]):
            self._power(bad)
        self.assertEqual(self.rt.fallbacks.report_violation.call_count, 0)

    def test_extremes(self):
        self._power(0)
        self.assertEqual(self.rt.fallbacks.report_violation.call_count, 1)
        self._power(100)
        self._power(-3)
        self.assertEqual(self.rt.fallbacks.report_violation.call_count, 2)

    def test_missing_keys_default_to_healthy(self):
        self.bridge.power_status = {}
        self.rt._check_power()
        self.bridge.power_status = {"plugged": False}
        self.rt._check_power()
        self.assertEqual(self.rt.fallbacks.report_violation.call_count, 0)

    def test_non_dict_status_skipped(self):
        self.bridge.power_status = "80%"
        self.rt._check_power()
        self.assertEqual(self.rt.fallbacks.report_violation.call_count, 0)


class TestThermalStreakSemantics(unittest.TestCase):
    def setUp(self):
        self.bridge = _FakeJBridge()
        self.rt, self.fallbacks, self.acc = _make_runtime(self.bridge)
        self.rt.fallbacks.report_violation = mock.Mock(
            wraps=self.rt.fallbacks.report_violation)

    def _thermal(self, level):
        self.bridge.thermal_status = level
        self.rt._check_thermal()

    def test_streak_pauses_after_two_elevated_readings(self):
        self._thermal(2)
        self.assertEqual(self.rt.fallbacks.report_violation.call_count, 0)
        self.assertEqual(self.rt._thermal_streak, 1)
        self._thermal(2)
        self.assertEqual(self.rt.fallbacks.report_violation.call_count, 1)
        self.assertEqual(self.rt._thermal_streak, 0)  # reset after report

    def test_nominal_resets_streak(self):
        self._thermal(2)
        self._thermal(0)
        self._thermal(2)
        self.assertEqual(self.rt.fallbacks.report_violation.call_count, 0)
        self._thermal(2)
        self.assertEqual(self.rt.fallbacks.report_violation.call_count, 1)

    def test_light_thermal_demotes_gpu_but_never_pauses(self):
        for _ in range(3):
            self._thermal(1)
        self.assertEqual(self.rt.fallbacks.report_violation.call_count, 0)
        self.assertEqual(self.acc.describe()["thermal_level"], 1)
        names = [d.kind for d in self.acc.resolve("llm")]
        self.assertNotIn("gpu", names)
        self.assertIn("cpu", names)

    def test_elevated_thermal_cpu_only(self):
        self._thermal(2)
        names = [d.kind for d in self.acc.resolve("llm")]
        self.assertEqual(names, ["cpu"])

    def test_garbage_thermal_values(self):
        self._thermal("hot")                     # ValueError -> skipped
        self._thermal(None)
        self.assertEqual(self.acc.describe()["thermal_level"], 0)
        self._thermal(99)                        # clamped to 2
        self.assertEqual(self.acc.describe()["thermal_level"], 2)
        self._thermal(-5)                        # clamped to 0
        self.assertEqual(self.acc.describe()["thermal_level"], 0)
        self._thermal(1.9)                       # int() floors -> 1
        self.assertEqual(self.acc.describe()["thermal_level"], 1)

    def test_configurable_streak_of_one_pauses_immediately(self):
        bridge = _FakeJBridge()
        rt, fb, _ = _make_runtime(bridge, thermal_pause_streak=1)
        fb.report_violation = mock.Mock(wraps=fb.report_violation)
        bridge.thermal_status = 2
        rt._check_thermal()
        self.assertEqual(fb.report_violation.call_count, 1)

    def test_thermal_recovery_restores_ladder(self):
        self._thermal(2)
        self._thermal(2)
        self._thermal(0)
        self.assertEqual(self.acc.describe()["thermal_level"], 0)
        names = [d.kind for d in self.acc.resolve("llm")]
        self.assertIn("cpu", names)


class TestSecretProvider(unittest.TestCase):
    def setUp(self):
        self.bridge = _FakeJBridge()

    def test_bind_maps_success_and_missing(self):
        provider = SecureStoreSecretProvider(
            self.bridge, ["llm_api_key", "missing_key"])
        sink = _SecretSink()
        results = provider.bind(sink)
        self.assertEqual(results, {"llm_api_key": True, "missing_key": False})
        self.assertEqual(sink.overrides["llm_api_key"], "device-secret-123")

    def test_bridge_exception_marks_failure(self):
        def boom(name):
            raise RuntimeError("keystore locked")

        self.bridge.getSecureSecret = boom
        results = SecureStoreSecretProvider(
            self.bridge, ["llm_api_key"]).bind(_SecretSink())
        self.assertEqual(results, {"llm_api_key": False})

    def test_non_string_secret_coerced(self):
        self.bridge.getSecureSecret = lambda name: 12345
        sink = _SecretSink()
        results = SecureStoreSecretProvider(self.bridge, ["k"]).bind(sink)
        self.assertEqual(results, {"k": True})
        self.assertEqual(sink.overrides["k"], "12345")

    def test_empty_and_overlong_names(self):
        provider = SecureStoreSecretProvider(self.bridge, ["", "x" * 100])
        results = provider.bind(_SecretSink())
        self.assertEqual(results[""], False)
        self.assertIn("x" * 64, results)   # names truncated to 64 chars


class TestNodeStartStopCycles(unittest.TestCase):
    """stop() is terminal for a node (interface shutdown is one-way), so
    churn scenarios build a fresh node per cycle, matching app reality
    where a service restart constructs a new node instance."""

    def _config(self, bridge, **kw):
        caps = CapabilityRegistry({"mobile_devices_allowlist": ["pixel8"]})
        kw.setdefault("publish_hz", 50)
        return NodeConfig(device_id="pixel8", role="sensor_node",
                          bridge=bridge, capabilities=caps,
                          sensors=["imu"], **kw)

    def test_25_start_stop_cycles_no_thread_leak(self):
        baseline = threading.active_count()
        beats = 0
        for _ in range(25):
            bridge = _FakeJBridge()
            node = AndroidShugoCoreNode(self._config(bridge))
            node.start()
            time.sleep(0.03)
            node.stop()
            self.assertIsNone(node._worker)
            beats += sum(1 for t, _ in bridge.published
                         if t.endswith("/heartbeat"))
        self.assertEqual(threading.active_count(), baseline)
        self.assertGreaterEqual(beats, 25)

    def test_bridge_death_mid_run(self):
        bridge = _FakeJBridge()
        node = AndroidShugoCoreNode(self._config(bridge))
        node.start()
        time.sleep(0.1)
        bridge.alive = False
        time.sleep(0.2)
        node.stop()   # must not raise
        topics = {t for t, _ in bridge.published}
        self.assertTrue(all(t.startswith("/shugocore/mobile/") for t in topics))

    def test_flaky_sensor_bridge_keeps_heartbeating(self):
        bridge = _FakeJBridge()
        reads = {"n": 0}

        def flaky_read(kind):
            reads["n"] += 1
            if reads["n"] % 3 == 0:
                raise OSError("sensor hub reset")
            return {"kind": kind, "v": reads["n"]}

        bridge.readSensor = flaky_read
        node = AndroidShugoCoreNode(self._config(bridge))
        node.start()
        time.sleep(0.3)
        node.stop()
        beats = [p for t, p in bridge.published if t.endswith("/heartbeat")]
        self.assertGreaterEqual(len(beats), 1)
        imu = [t for t, _ in bridge.published if t.endswith("/imu")]
        self.assertGreater(len(imu), 0)

    def test_soak_sensor_node_5_seconds(self):
        bridge = _FakeJBridge()
        node = AndroidShugoCoreNode(self._config(bridge, publish_hz=20))
        node.start()
        time.sleep(5.0)
        node.stop()
        seqs = [p["seq"] for t, p in bridge.published
                if t.endswith("/heartbeat")]
        self.assertTrue(seqs, "no heartbeats in soak window")
        self.assertEqual(seqs, sorted(seqs))          # monotonic
        self.assertGreaterEqual(len(seqs), 2)         # immediate + ~2s cadence
        imu = [t for t, _ in bridge.published if t.endswith("/imu")]
        self.assertGreaterEqual(len(imu), 20)         # 20 Hz over 5 s
        self.assertLessEqual(len(imu), 220)           # bounded: no runaway
        self.assertTrue(os.path.exists(node.config.audit_path))


if __name__ == "__main__":
    unittest.main()
