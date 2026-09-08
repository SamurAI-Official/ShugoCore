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
from attention_layer import AttentionLayer  # noqa: E402


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
        self.assertEqual(status["pipeline_stages"], ["OBSERVE", "GATE", "RECORD"])
        self.assertEqual(status["last_evaluation"], "policy_block")
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
        self.assertEqual(len(PIPELINE_STAGES), 8)
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

    # -- cycle outcome contract (Phase 1) --------------------------------------

    def test_outcome_no_action_is_not_an_error(self):
        """A model that answers but proposes nothing executable records a
        healthy NO_ACTION cycle: no exception, no execution, next cycle."""
        agent = self._make()
        agent.engine = mock.MagicMock()
        agent.engine.execute_task.return_value = {
            "status": "error", "outcome": "no_viable_action",
            "message": "no viable action proposed by the model ensemble",
            "call_errors": {}, "stages": ["GATE", "DECIDE", "RECORD"]}
        agent.tick()
        status = agent.get_status()
        result = status["last_cycle_result"]
        self.assertEqual(result["outcome"], "NO_ACTION")
        self.assertEqual(status["last_evaluation"], "no_action")
        self.assertNotIn("EXECUTE", result["stages"])
        self.assertNotIn("EVALUATE", result["stages"])
        self.assertIn("RECORD", result["stages"])
        self.assertFalse(result["executed"])

    def test_outcome_backend_failure_distinguished_from_no_action(self):
        """Transport-class call errors map to BACKEND_FAILURE, not NO_ACTION."""
        agent = self._make()
        agent.engine = mock.MagicMock()
        agent.engine.execute_task.return_value = {
            "status": "error", "outcome": "no_viable_action",
            "message": "no viable action proposed by the model ensemble",
            "call_errors": {"qwen": "transport_error: URLError"},
            "stages": ["GATE", "DECIDE", "RECORD"]}
        agent.tick()
        result = agent.get_status()["last_cycle_result"]
        self.assertEqual(result["outcome"], "BACKEND_FAILURE")
        self.assertEqual(agent.get_status()["last_evaluation"], "backend_failure")

    def test_outcome_policy_block_from_decision_gate(self):
        agent = self._make()
        agent.engine = mock.MagicMock()
        agent.engine.execute_task.return_value = {
            "status": "refused", "outcome": "policy_block",
            "reason": "no external consent grant",
            "stages": ["GATE", "DECIDE", "RECORD"]}
        agent.tick()
        result = agent.get_status()["last_cycle_result"]
        self.assertEqual(result["outcome"], "POLICY_BLOCK")
        self.assertNotIn("EXECUTE", result["stages"])
        self.assertNotIn("EVALUATE", result["stages"])
        self.assertIn("DECIDE", result["stages"])
        self.assertIn("RECORD", result["stages"])

    def test_outcome_governor_block(self):
        agent = self._make()
        agent.engine = mock.MagicMock()
        agent.engine.execute_task.return_value = {
            "status": "refused", "outcome": "governor_block",
            "reason": "governor paused", "stages": ["GATE", "RECORD"]}
        agent.tick()
        result = agent.get_status()["last_cycle_result"]
        self.assertEqual(result["outcome"], "GOVERNOR_BLOCK")
        self.assertNotIn("DECIDE", result["stages"])
        self.assertIn("RECORD", result["stages"])

    def test_outcome_task_failure_includes_executed_stage(self):
        agent = self._make()
        agent.engine = mock.MagicMock()
        agent.engine.execute_task.return_value = {
            "status": "error", "outcome": "task_failure",
            "message": "ToolError",
            "stages": ["GATE", "DECIDE", "EXECUTE", "RECORD"]}
        agent.tick()
        result = agent.get_status()["last_cycle_result"]
        self.assertEqual(result["outcome"], "TASK_FAILURE")
        self.assertIn("EXECUTE", result["stages"])
        self.assertNotIn("EVALUATE", result["stages"])

    def test_outcome_in_flight_observes_only(self):
        """The in-flight cycle skips journaling; the running cycle records."""
        agent = self._make()
        agent.engine = mock.MagicMock()
        agent.engine.execute_task.return_value = {"status": "in_flight"}
        before = len(agent.memory.tier1.recent(50))
        agent.tick()
        result = agent.get_status()["last_cycle_result"]
        self.assertEqual(result["outcome"], "IN_FLIGHT")
        self.assertEqual(result["stages"], ["OBSERVE"])
        after = len(agent.memory.tier1.recent(50))
        self.assertLessEqual(after - before, 1)  # only android_observation

    def test_outcome_engine_failure_on_exception(self):
        agent = self._make()
        agent.engine = mock.MagicMock()
        agent.engine.execute_task.side_effect = RuntimeError("boom")
        agent.tick()
        status = agent.get_status()
        self.assertEqual(status["last_cycle_result"]["outcome"], "ENGINE_FAILURE")
        self.assertEqual(status["last_evaluation"], "engine_failure")

    def test_outcome_legacy_mock_mapping(self):
        """v1.9.0-style results without an outcome field still classify."""
        agent = self._make()
        agent.engine = mock.MagicMock()
        agent.engine.execute_task.return_value = {"status": "success"}
        agent.tick()
        self.assertEqual(
            agent.get_status()["last_cycle_result"]["outcome"], "SUCCESS")

    def test_real_engine_unreachable_backend_is_backend_failure(self):
        """No local server: the ensemble is unreachable -> BACKEND_FAILURE
        (transport class), not NO_ACTION and not a generic error."""
        agent = self._make()
        agent.tick()
        result = agent.get_status()["last_cycle_result"]
        self.assertEqual(result["outcome"], "BACKEND_FAILURE")

    # -- action schema + null-proposal accounting (Phase 3) --------------------

    def test_null_proposals_count_toward_fallback(self):
        """A well-formed {"action_type": null} must NOT reset the failure
        counter: a model that only ever says null must still reach the
        rule-based fallback instead of looping NO_ACTION forever."""
        agent = self._make()
        engine = agent.engine
        engine.subconscious.get_model_output = mock.MagicMock(return_value=(
            '{"action_type": null, "params": {}, "confidence": 0.0}'))
        for i in range(1, 4):
            engine.make_decision({"type": "t", "content": "x"})
            self.assertEqual(engine._model_failures, i)
        decision = engine.make_decision({"type": "t", "content": "x"})
        self.assertEqual(decision["action_type"], "record_observation")
        self.assertEqual(decision["proposal_source"], "rule_fallback")

    def test_record_observation_executes_without_consent(self):
        """The internal observation action is non-side-effecting: it passes
        the gate with NO consent grant and lands a real event in Tier 1."""
        agent = self._make()
        engine = agent.engine
        engine.subconscious.get_model_output = mock.MagicMock(return_value=(
            '{"action_type": "record_observation", '
            '"params": {"text": "hello from the model"}, "confidence": 0.9}'))
        result = engine.execute_task({"type": "t", "content": "x"})
        self.assertEqual(result["status"], "success")
        self.assertTrue(result["recorded"])
        self.assertFalse(engine.consents.has_grant("record_observation"))
        events = [e["type"] for e in agent.memory.tier1.recent(10)]
        self.assertIn("observation_recorded", events)
        self.assertIn("tool_execution", events)

    def test_no_viable_detail_reports_null_proposal(self):
        agent = self._make()
        engine = agent.engine
        engine.subconscious.get_model_output = mock.MagicMock(return_value=(
            '{"action_type": null, "params": {}, "confidence": 0.0}'))
        engine.execute_task({"type": "t", "content": "x"})
        events = [e for e in agent.memory.tier1.recent(5)
                  if e["type"] == "no_viable_action"]
        self.assertTrue(events)
        self.assertIn("null proposal", events[-1]["payload"]["detail"])

    def test_decision_prompt_generated_from_action_schema(self):
        """The model-facing protocol is generated from the engine's real
        executor set — it can never drift from what policy/execution support."""
        agent = self._make()
        schema = agent.engine.available_action_types()
        self.assertIn("record_observation", schema)
        self.assertIn("multi_step_process", schema)
        self.assertNotIn("robot_navigate", schema)  # no handler registered
        from subconscious import _build_decision_prompt
        prompt = _build_decision_prompt("TASK", schema)
        self.assertIn("record_observation", prompt)
        self.assertIn("multi_step_process", prompt)
        self.assertNotIn("robot_navigate", prompt)

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


# -- v1.28 visual-audio binding (speech-source attribution) -------------

class TestVisualAudioBinding(unittest.TestCase):
    """The attention layer must distinguish speech produced by a
    visible-and-attending person (an instruction) from audio with no visible
    talker (TV / music / ambient) — the core 'is the person I see the one
    talking?' question."""

    def _layer(self, now=1000.0):
        clock = mock.Mock(side_effect=lambda: now)
        return AttentionLayer(clock=clock), clock

    def test_no_signals_yields_none(self):
        layer, _ = self._layer()
        self.assertEqual(layer.speech_source()["source"], "none")

    def test_face_silent_is_person_present_silent(self):
        layer, _ = self._layer()
        layer.stamp_face(1)
        self.assertEqual(layer.speech_source()["source"],
                         "person_present_silent")

    def test_speech_no_face_is_unattributed(self):
        layer, clock = self._layer()
        layer.stamp_speech("hello there")
        clock.side_effect = lambda: 1001.0
        src = layer.speech_source()
        self.assertEqual(src["source"], "unattributed_audio")
        self.assertFalse(src["face_fresh"])

    def test_face_plus_gaze_plus_speech_is_verified_person(self):
        layer, clock = self._layer()
        layer.stamp_face(1, gaze_toward_camera=True)
        clock.side_effect = lambda: 1001.0
        layer.stamp_speech("read the instruction")
        src = layer.speech_source()
        self.assertEqual(src["source"], "verified_person")
        self.assertGreaterEqual(src["confidence"], 0.8)

    def test_speech_with_face_but_gaze_away_is_unattributed(self):
        layer, clock = self._layer()
        layer.stamp_face(1, gaze_toward_camera=False)
        clock.side_effect = lambda: 1001.0
        layer.stamp_speech("tv dialogue")
        src = layer.speech_source()
        self.assertEqual(src["source"], "unattributed_audio")
        self.assertTrue(src["face_fresh"])

    def test_stale_speech_decays_to_none(self):
        layer, clock = self._layer()
        layer.stamp_speech("hello")
        clock.side_effect = lambda: 1001.0 + 20.0  # 20 s later
        self.assertEqual(layer.speech_source()["source"], "none")

    def test_snapshot_exposes_speech_source(self):
        layer, clock = self._layer()
        layer.stamp_face(1, gaze_toward_camera=True)
        clock.side_effect = lambda: 1001.0
        layer.stamp_speech("hello shugo")
        snap = layer.snapshot()
        self.assertEqual(snap["speech_source"], "verified_person")
        self.assertIn("speech_source_conf", snap)


class TestRemoteBinding(unittest.TestCase):
    """update_mesh_peers + observation fusion: a peripheral's carried
    remote facts reach the agent's observation as remote_binding — and are
    never assumed when absent."""

    def setUp(self):
        for name in _CLEAN_FILES:
            try:
                os.remove(name)
            except FileNotFoundError:
                pass

    def tearDown(self):
        for name in _CLEAN_FILES:
            try:
                os.remove(name)
            except FileNotFoundError:
                pass

    def test_update_mesh_peers_merges_remote_binding(self):
        agent = create_agent(device_caps="SM-X518U",
                             api_url="http://127.0.0.1:11434")
        try:
            snap = ('{"mesh_peer_count":1,"mesh_peers":['
                    '{"device_id":"A51","name":"A51","role":"peripheral",'
                    '"camera":true,"remote_face_present":true,'
                    '"remote_voice_active":true,'
                    '"remote_speech_source":"verified_person"}]}')
            agent.update_mesh_peers(snap)
            obs = agent._get_observation()
            self.assertEqual(obs.get("mesh_peer_count"), 1)
            rb = obs.get("remote_binding")
            self.assertIsNotNone(rb)
            self.assertTrue(rb.get("face_present"))
            self.assertTrue(rb.get("voice_active"))
            self.assertEqual(rb.get("speech_source"), "verified_person")
        finally:
            agent.cleanup()

    def test_no_mesh_means_no_remote_binding(self):
        agent = create_agent(device_caps="Exynos-1380",
                             api_url="http://127.0.0.1:11434")
        try:
            agent.telemetry = {}
            obs = agent._get_observation()
            self.assertNotIn("remote_binding", obs)
        finally:
            agent.cleanup()

    def test_remote_face_without_voice_is_silent(self):
        agent = create_agent(api_url="http://127.0.0.1:11434")
        try:
            snap = ('{"mesh_peer_count":1,"mesh_peers":[{"device_id":"A51",'
                    '"camera":true,"remote_face_present":true,'
                    '"remote_voice_active":false}]}')
            agent.update_mesh_peers(snap)
            rb = agent._get_observation().get("remote_binding")
            self.assertEqual(rb.get("speech_source"), "person_present_silent")
        finally:
            agent.cleanup()


if __name__ == "__main__":
    unittest.main()
