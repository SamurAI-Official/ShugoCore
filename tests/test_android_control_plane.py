"""Android node control plane (v1.9.0) — capability acknowledgement, stop-server
fallback semantics, network policy enforcement and status enrichment.

Exercises the AndroidAgent surface the 5-tab control plane is built on,
without any network or Android runtime, exactly like test_sensor_engagement.
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from shugocore_agent import PIPELINE_STAGES, create_agent  # noqa: E402


_CLEAN_FILES = (
    "semantic_memory.db", "semantic_memory.db-shm", "semantic_memory.db-wal",
    "audit_chain.jsonl", "episodic_journal.jsonl", "agent_ledger.jsonl",
)


class TestAndroidControlPlane(unittest.TestCase):
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

    def _make(self, api_url="http://127.0.0.1:11434"):
        agent = create_agent(device_caps="Exynos-1380", api_url=api_url)
        self.agents.append(agent)
        return agent

    # -- capability acknowledgement -----------------------------------------

    def test_capability_declaration_is_acked(self):
        agent = self._make()
        result = agent.update_capabilities([
            {"name": "Camera", "state": "available", "permission": "granted",
             "stream": "idle", "detail": ""},
            {"name": "accelerometer", "state": "available",
             "permission": "granted", "stream": "active", "detail": "100 Hz"},
        ])
        self.assertEqual(result["acked"], ["camera", "accelerometer"])
        caps = agent.get_capabilities()
        self.assertTrue(caps["camera"]["agent_ack"])
        self.assertEqual(caps["camera"]["permission"], "granted")
        self.assertEqual(caps["accelerometer"]["stream"], "active")

    def test_ack_is_not_authority(self):
        """Receiving a declaration never grants authority to use it."""
        agent = self._make()
        agent.update_capabilities([{"name": "camera", "permission": "granted",
                                    "stream": "idle"}])
        self.assertEqual(agent.agent_caps, {})
        self.assertFalse(agent.get_status()["policy"]["agent_caps"].get("camera"))

    def test_malformed_declarations_tolerated(self):
        agent = self._make()
        result = agent.update_capabilities([None, "junk", {}, {"name": " "},
                                            {"name": "gps", "stream": "idle"}])
        self.assertEqual(result["acked"], ["gps"])

    # -- STOP SERVER fallback semantics ---------------------------------------

    def test_stop_server_fallback_repoints_backend(self):
        """set_backend_url updates the agent URL, cached adapters and config."""
        agent = self._make()
        agent.update_policy(lan=True)
        agent.set_backend_url("http://192.168.1.50:8000/")
        self.assertEqual(agent.api_url, "http://192.168.1.50:8000")
        for backend in agent.engine._backend_cache.values():
            self.assertEqual(backend.base_url, "http://192.168.1.50:8000")
        for model in agent.engine.models:
            if model["backend"]["type"] == "android":
                self.assertEqual(model["backend"]["api_url"],
                                 "http://192.168.1.50:8000")

    def test_set_backend_url_ignores_empty(self):
        agent = self._make()
        agent.set_backend_url("")
        self.assertEqual(agent.api_url, "http://127.0.0.1:11434")

    # -- network policy (SECURITY tab) ----------------------------------------

    def test_localhost_always_allowed(self):
        agent = self._make(api_url="http://127.0.0.1:11434")
        agent.update_policy(internet=False, lan=False)
        allowed, scope = agent._backend_target_allowed()
        self.assertTrue(allowed)
        self.assertEqual(scope, "localhost")

    def test_lan_blocked_when_disabled(self):
        agent = self._make(api_url="http://192.168.1.50:8000")
        agent.update_policy(lan=False)
        allowed, scope = agent._backend_target_allowed()
        self.assertFalse(allowed)
        self.assertEqual(scope, "lan")

    def test_internet_blocked_by_default(self):
        agent = self._make(api_url="https://api.example.com/v1")
        agent.update_policy(internet=False, lan=True)
        allowed, scope = agent._backend_target_allowed()
        self.assertFalse(allowed)
        self.assertEqual(scope, "internet")

    def test_blocked_backend_refuses_before_engine(self):
        """Fail-closed: the tick stops at GATE and records a policy block."""
        agent = self._make(api_url="http://192.168.1.50:8000")
        agent.update_policy(lan=False)
        agent.engine = mock.MagicMock()
        agent.tick()
        status = agent.get_status()
        self.assertEqual(status["pipeline_stages"], ["OBSERVE", "GATE"])
        self.assertEqual(status["last_evaluation"], "refused")
        self.assertIn("network policy", status["last_decision"])
        agent.engine.execute_task.assert_not_called()
        event_types = [e["type"] for e in agent.memory.tier1.recent(5)]
        self.assertIn("policy_block", event_types)

    # -- status enrichment -----------------------------------------------------

    def test_status_surfaces_memory_tiers_and_pipeline(self):
        agent = self._make()
        agent.tick()
        status = agent.get_status()
        self.assertIn("tier0_entries", status)
        self.assertIn("tier1_entries", status)
        self.assertIn("tier2_facts", status)
        self.assertEqual(status["tier3"], "READ ONLY")
        self.assertEqual(status["pipeline_all"], list(PIPELINE_STAGES))
        self.assertEqual(len(PIPELINE_STAGES), 7)
        self.assertTrue(status["engine_ready"])

    def test_legacy_status_keys_preserved(self):
        """v1.8.1 consumers keep working: the original keys are intact."""
        agent = self._make()
        agent.update_telemetry({"battery_level": 50, "timestamp_ms": 1})
        agent.tick()
        status = agent.get_status()
        for key in ("tick_count", "device_caps", "engine",
                    "tier1_entries", "telemetry_received"):
            self.assertIn(key, status)
        self.assertEqual(status["device_caps"], "Exynos-1380")
        self.assertTrue(status["telemetry_received"])

    def test_stage_tracking_refuses_without_action(self):
        """Refusal: no EXECUTE/EVALUATE is claimed, but the engine journals
        the refusal (policy_block/governor_block), so RECORD is honest."""
        agent = self._make()
        agent.engine = mock.MagicMock()
        agent.engine.execute_task.return_value = {"status": "refused",
                                                  "reason": "gated"}
        agent.tick()
        stages = agent.get_status()["pipeline_stages"]
        self.assertNotIn("EXECUTE", stages)
        self.assertNotIn("EVALUATE", stages)
        self.assertIn("RECORD", stages)
        self.assertIn("DECIDE", stages)

    def test_stage_tracking_no_viable_action_still_records(self):
        """Designed semantics: no viable action -> RECORD -> next cycle."""
        agent = self._make()
        agent.engine = mock.MagicMock()
        agent.engine.execute_task.return_value = {
            "status": "error",
            "message": "no viable action proposed by the model ensemble"}
        agent.tick()
        status = agent.get_status()
        stages = status["pipeline_stages"]
        self.assertNotIn("EXECUTE", stages)
        self.assertNotIn("EVALUATE", stages)
        self.assertIn("RECORD", stages)
        self.assertIn("no viable action", status["last_decision"])

    def test_successful_cycle_records_all_stages(self):
        agent = self._make()
        agent.engine = mock.MagicMock()
        agent.engine.execute_task.return_value = {"status": "success"}
        agent.tick()
        stages = agent.get_status()["pipeline_stages"]
        for stage in ("OBSERVE", "GATE", "DECIDE", "EXECUTE", "EVALUATE", "RECORD"):
            self.assertIn(stage, stages)

    # -- log buffer --------------------------------------------------------------

    def test_recent_logs_seq_monotonic(self):
        agent = self._make()
        agent.log("AGENT", "first")
        seq = agent.recent_logs(0)[-1]["seq"]
        agent.log("SENSOR", "second")
        tail = agent.recent_logs(seq)
        self.assertEqual(len(tail), 1)
        self.assertEqual(tail[0]["message"], "second")
        self.assertEqual(tail[0]["category"], "SENSOR")

    def test_policy_update_logs_to_buffer(self):
        agent = self._make()
        agent.update_policy(agent_caps={"camera": True}, internet=False)
        entries = agent.recent_logs(0)
        self.assertTrue(any(e["category"] == "POLICY" for e in entries))


if __name__ == "__main__":
    unittest.main()
