"""Sensor engagement test cycle for the Android agent.

Exercises the phone-telemetry -> Tier 1 memory path of AndroidAgent without
any network or Android runtime: a telemetry snapshot is injected via
update_telemetry() and the backend is mocked so execute_task is deterministic.

Runnable on desktop with /usr/local/bin/python3 (chromadb falls back to the
in-process stub VectorDB, exactly as the desktop simulation does).
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from android_inference import AndroidBackend  # noqa: E402
from shugocore_agent import create_agent  # noqa: E402


_TELEMETRY = {
    "battery_level": 78,
    "is_charging": True,
    "cpu_temp_c": 42.5,
    "mem_total_mb": 6144,
    "mem_avail_mb": 1024,
    "accel_x": 0.05,
    "accel_y": -0.12,
    "accel_z": 9.78,
    "thermal_state": "NONE",
    "timestamp_ms": 1700000000000,
}

_CLEAN_FILES = (
    "semantic_memory.db", "semantic_memory.db-shm", "semantic_memory.db-wal",
    "audit_chain.jsonl", "episodic_journal.jsonl", "agent_ledger.jsonl",
)


class TestSensorEngagement(unittest.TestCase):
    def setUp(self):
        self.agents = []

    def tearDown(self):
        for agent in self.agents:
            try:
                agent.cleanup()
            except Exception:
                pass
        for name in _CLEAN_FILES:
            try:
                os.remove(name)
            except FileNotFoundError:
                pass

    def _make(self):
        agent = create_agent(device_caps="Exynos-1380",
                             api_url="http://127.0.0.1:11434")
        self.agents.append(agent)
        return agent

    def test_android_backend_constructs_with_api_url(self):
        # AndroidBackend.__init__ takes `api_url`, stored as self.base_url.
        backend = AndroidBackend(
            api_url="http://127.0.0.1:11434",
            model_name="shugocore-local",
            device_caps={"soc": "Exynos-1380"},
        )
        self.assertEqual(backend.model_name, "shugocore-local")
        self.assertEqual(backend.base_url, "http://127.0.0.1:11434")

    def test_factory_builds_agent_with_caps(self):
        agent = self._make()
        self.assertEqual(agent.device_caps, "Exynos-1380")
        status = agent.get_status()
        self.assertEqual(status["device_caps"], "Exynos-1380")
        self.assertEqual(status["tick_count"], 0)
        self.assertFalse(status["telemetry_received"])

    def test_telemetry_feeds_observation(self):
        agent = self._make()
        agent.update_telemetry(dict(_TELEMETRY))
        obs = agent._get_observation()
        self.assertEqual(obs["battery"], 78)
        self.assertTrue(obs["battery_plugged"])
        self.assertEqual(obs["cpu_temp_c"], 42.5)
        # memory_usage_mb = total - avail = 6144 - 1024 = 5120
        self.assertEqual(obs["memory_usage_mb"], 5120)
        self.assertEqual(obs["memory_avail_mb"], 1024)
        self.assertEqual(obs["accel"], [0.05, -0.12, 9.78])
        self.assertEqual(obs["thermal_state"], "NONE")

    def test_stub_fallback_when_no_telemetry(self):
        agent = self._make()
        obs = agent._get_observation()
        self.assertEqual(obs["battery"], 100)  # stub default
        self.assertEqual(obs["memory_usage_mb"], 0)
        self.assertFalse(obs["battery_plugged"])

    def test_sensor_test_cycle_records_observations_and_memory(self):
        agent = self._make()
        with mock.patch.object(AndroidBackend, "generate", return_value="ok"):
            agent.update_telemetry(dict(_TELEMETRY))
            report = agent.sensor_test_cycle(steps=5)
        self.assertEqual(report["steps"], 5)
        self.assertEqual(report["tick_count"], 5)
        self.assertEqual(len(report["observations"]), 5)
        self.assertTrue(all(o["battery"] == 78 for o in report["observations"]))
        self.assertTrue(all(o["accel"] == [0.05, -0.12, 9.78]
                            for o in report["observations"]))
        self.assertGreaterEqual(report["tier1_entries"], 5)
        self.assertTrue(report["telemetry_received"])

    def test_sensor_test_cycle_is_deterministic_for_fixed_telemetry(self):
        a1 = self._make()
        a2 = self._make()
        self.agents = [a1, a2]
        for agent in (a1, a2):
            agent.update_telemetry(dict(_TELEMETRY))
        r1 = a1.sensor_test_cycle(steps=3)
        r2 = a2.sensor_test_cycle(steps=3)
        self.assertEqual(r1["tick_count"], r2["tick_count"])
        self.assertEqual(len(r1["observations"]), len(r2["observations"]))


if __name__ == "__main__":
    unittest.main()
