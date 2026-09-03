"""
v1.0.0 regression tests: governor interlocks, deterministic fallbacks,
context budgeting, memory journal & entity graph, telemetry.
"""
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fallbacks import FallbackController, FallbackHalt
from decision_engine import DecisionEngine
from memory_system import CoreIdentity, EpisodicMemory, MemoryManager, Scratchpad
from state_machine import AgentState, ExecutionGovernor, GovernorError
from telemetry import get_tracer
from token_budget import ContextBudget, estimate_tokens
from version import __version__


class GovernorTestCase(unittest.TestCase):
    def setUp(self):
        self.governor = ExecutionGovernor(step_budget=3,
                                          task_deadline_seconds=60.0)

    def test_transition_matrix_enforced(self):
        self.governor.begin_task("t1")
        self.governor.step(AgentState.GATING)
        self.governor.step(AgentState.DECIDING)
        with self.assertRaises(GovernorError):  # DECIDING -> GATING is illegal
            self.governor.step(AgentState.GATING)

    def test_reentrancy_blocked(self):
        self.governor.begin_task("outer")
        with self.assertRaises(GovernorError):
            self.governor.begin_task("inner")  # recursive entry refused

    def test_step_budget_exhausted(self):
        self.governor.begin_task("budget")
        self.governor.consume_step(2)
        self.governor.consume_step(1)
        with self.assertRaises(GovernorError):
            self.governor.consume_step(1)  # 4 > 3

    def test_deadline_enforced(self):
        governor = ExecutionGovernor(task_deadline_seconds=0.01)
        governor.begin_task("slow")
        time.sleep(0.03)
        with self.assertRaises(GovernorError):
            governor.step(AgentState.GATING)

    def test_pause_halt_and_resume(self):
        self.governor.pause("test")
        self.assertEqual(self.governor.state, AgentState.PAUSED)
        with self.assertRaises(GovernorError):
            self.governor.begin_task("blocked")
        self.governor.resume(resumed_by="operator")
        self.assertEqual(self.governor.state, AgentState.IDLE)
        self.governor.begin_task("ok")
        self.governor.end_task()

    def test_resume_requires_attribution(self):
        self.governor.pause("test")
        with self.assertRaises(GovernorError):
            self.governor.resume(resumed_by="")
        self.governor.resume(resumed_by="operator")

    def test_halt_is_terminal(self):
        self.governor.halt("fatal")
        self.assertEqual(self.governor.state, AgentState.HALTED)
        with self.assertRaises(GovernorError):
            self.governor.resume(resumed_by="operator")


class FallbackControllerTestCase(unittest.TestCase):
    def setUp(self):
        self.governor = ExecutionGovernor()
        self.controller = FallbackController(
            governor=self.governor,
            thresholds={"episodic_backlog": 3, "maintenance_failures": 2})

    def test_default_trigger_pauses(self):
        self.controller.report_violation("circuit_breakers_open", "2 hosts")
        self.assertEqual(self.controller.mode, "paused")
        self.assertEqual(self.governor.state, AgentState.PAUSED)

    def test_critical_trigger_safe_state(self):
        self.controller.report_violation("step_budget_exhausted", "budget")
        self.assertEqual(self.controller.mode, "safe_state")
        self.assertEqual(self.governor.state, AgentState.SAFE_STATE)

    def test_halt_raises_fallback_halt(self):
        controller = FallbackController(
            governor=self.governor,
            severities={"memory_failure": "halt"})
        with self.assertRaises(FallbackHalt):
            controller.report_violation("memory_failure", "sqlite locked")
        self.assertEqual(self.governor.state, AgentState.HALTED)
        self.assertEqual(controller.mode, "halted")

    def test_thresholded_violations_escalate(self):
        controller = FallbackController(
            governor=self.governor,
            thresholds={"maintenance_failures": 2})
        controller.report_violation("maintenance_worker_failure", "a")
        self.assertEqual(controller.mode, "normal")
        controller.report_violation("maintenance_worker_failure", "b")
        self.assertEqual(controller.mode, "paused")

    def test_resume_clears_and_requires_attribution(self):
        self.controller.report_violation("circuit_breakers_open", "x")
        with self.assertRaises(GovernorError):
            self.controller.resume(resumed_by="")
        self.controller.resume(resumed_by="operator")
        self.assertEqual(self.controller.mode, "normal")
        self.assertEqual(self.controller.status()["violations"], {})

    def test_proactive_evaluate_fires_tier1_backlog(self):
        memory = MemoryManager(agent_id="fb-test", auto_start=False,
                               episodic_capacity=1000)
        for _ in range(5):
            memory.record_event("tool", {"status": "success"})
        controller = FallbackController(
            governor=self.governor, memory=memory,
            thresholds={"episodic_backlog": 3})
        controller.evaluate()
        self.assertEqual(controller.mode, "paused")
        memory.shutdown()


class TokenBudgetTestCase(unittest.TestCase):
    def test_estimate_tokens(self):
        self.assertEqual(estimate_tokens(""), 0)
        self.assertEqual(estimate_tokens("abcd"), 1)
        self.assertGreaterEqual(estimate_tokens("x" * 100), 25)

    def test_budget_limits_and_truncation(self):
        budget = ContextBudget(total_tokens=1000)
        self.assertTrue(budget.fits("scratchpad", "short"))
        self.assertLessEqual(
            estimate_tokens(budget.truncate("scratchpad", "y" * 5000)),
            budget.limit("scratchpad"))

    def test_absolute_limits_override_shares(self):
        budget = ContextBudget(total_tokens=1000,
                               allocations={"task": 100})
        self.assertEqual(budget.limit("task"), 100)


class MemoryV1TestCase(unittest.TestCase):
    def test_scratchpad_token_budget_evicts_oldest(self):
        scratch = Scratchpad(max_entries=1000, max_tokens=64)
        scratch.write("word " * 4)   # ~4 tokens
        scratch.write("x" * 200)     # ~50 tokens -> pushes total over the cap
        self.assertLessEqual(scratch.token_count(), 64)
        self.assertLessEqual(len(scratch.read()), 2)

    def test_episodic_journal_crash_recovery(self):
        tmp = tempfile.mkdtemp(prefix="shugocore_journal_")
        try:
            journal = os.path.join(tmp, "episodic.jsonl")
            memory = EpisodicMemory(max_events=50, journal_path=journal)
            memory.record("step_a", {"n": 1})
            memory.record("step_b", {"n": 2})
            memory.record("step_c", {"n": 3})
            # Simulate a crash before consolidation: recreate the memory.
            recovered = EpisodicMemory(max_events=50, journal_path=journal)
            self.assertEqual(len(recovered), 3)
            self.assertEqual(recovered.recent()[0]["payload"]["n"], 1)
            # Drain compacts the journal.
            recovered.drain()
            with open(journal, "r", encoding="utf-8") as handle:
                self.assertEqual(handle.read().strip(), "")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_episodic_age_eviction(self):
        memory = EpisodicMemory(max_events=100, max_age_hours=0.0)
        memory.record("old_event", {})
        time.sleep(0.01)
        memory.record("recent_event", {})
        # max_age_hours=0: everything is older than the cutoff -> all evicted.
        self.assertEqual(len(memory), 0)

    def test_entity_graph_extraction_and_query(self):
        from memory_system import SemanticMemory
        tmp = tempfile.mkdtemp(prefix="shugocore_graph_")
        try:
            memory = SemanticMemory(db_path=os.path.join(tmp, "mem.db"))
            fact_id = memory.store_fact(
                "Retry tape_api documented failure for model gpt-4 and openai.com")
            entities = memory.entity_names()
            self.assertIn("tape_api", entities)
            self.assertIn("gpt-4", entities)
            self.assertIn("openai.com", entities)
            hits = memory.facts_about("tape_api")
            self.assertTrue(any(f["id"] == fact_id for f in hits))
            related = memory.related_entities("tape_api")
            self.assertTrue(any(r["name"] == "gpt-4" for r in related))
            memory.close()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_hybrid_retrieval_includes_graph(self):
        from memory_system import MemoryManager, SemanticMemory
        tmp = tempfile.mkdtemp(prefix="shugocore_hybrid_")
        try:
            semantic = SemanticMemory(db_path=os.path.join(tmp, "mem.db"))
            semantic.store_fact("Endpoint status check for tape_api", kind="fact")
            manager = MemoryManager(agent_id="hybrid", semantic=semantic,
                                    auto_start=False)
            result = manager.retrieve_context("tape_api status", top_k=2)
            self.assertEqual(len(result["semantic"]) + len(result["graph"]), 1)
            self.assertIn("graph", result)
            manager.shutdown()
            semantic.close()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_system_prompt_renders_invariants(self):
        core = CoreIdentity()
        prompt = core.system_prompt()
        self.assertIn("no_harm", prompt)
        self.assertIn("consent_required", prompt)


class TelemetryTestCase(unittest.TestCase):
    def test_noop_tracer_records_spans(self):
        tracer = get_tracer("v1-test")
        with tracer.start_span("op", {"a": 1}) as span:
            span.set_attribute("b", 2)
            span.add_event("checkpoint")
        spans = tracer.recent_spans()
        self.assertTrue(any(s["name"] == "op" for s in spans))
        record = [s for s in spans if s["name"] == "op"][-1]
        self.assertGreaterEqual(record["duration_ms"], 0.0)
        self.assertEqual(record["attributes"]["a"], 1)


class EngineV1IntegrationTestCase(unittest.TestCase):
    """Proves the governor/fallback/journal are actually wired into the engine."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="shugocore_v1_engine_")
        self.engine = DecisionEngine(
            [{"id": "m1", "type": "text", "weight": 1.0,
              "backend": {"type": "stub"}}],
            {"type": "chroma"}, news_api_key=None,
            memory_db_path=os.path.join(self.tmp, "mem.db"),
            audit_path=os.path.join(self.tmp, "audit.jsonl"),
            episodic_journal_path=os.path.join(self.tmp, "episodic.jsonl"),
        )

    def tearDown(self):
        self.engine.task_manager.stop()
        self.engine.memory.shutdown()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_safe_state_blocks_side_effecting_actions(self):
        self.engine.governor.safe_state("integration test")
        # A proposing backend would be needed for a decision-level api_call;
        # the engine-level SAFE_STATE refusal also triggers at check_ethics
        # for side-effecting task types with consent present.
        self.engine.consents.grant("api_call", granted_by="operator")
        result = self.engine.execute_task(
            {"type": "api_call", "content": "x"})
        # Either refused at the task gate (invariant/SAFE_STATE) or at the
        # decision gate - both are refusals, never a side effect running.
        self.assertEqual(result.get("status"), "refused")

    def test_paused_engine_refuses_tasks(self):
        self.engine.governor.pause("integration test")
        result = self.engine.execute_task({"type": "text", "content": "x"})
        self.assertEqual(result.get("status"), "refused")
        self.assertIn("paused", result.get("reason", ""))

    def test_episodic_journal_wired(self):
        self.engine.memory.record_event("wired_probe", {"n": 1})
        journal = os.path.join(self.tmp, "episodic.jsonl")
        self.assertTrue(os.path.exists(journal))
        with open(journal, "r", encoding="utf-8") as handle:
            self.assertIn("wired_probe", handle.read())


class EthicsHardeningTestCase(unittest.TestCase):
    """Tests for the hardened ethics/policy checks (no more placeholders)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="shugocore_v1_ethics_")
        self.engine = DecisionEngine(
            [{"id": "m1", "type": "text", "weight": 1.0,
              "backend": {"type": "stub"}}],
            {"type": "chroma"}, news_api_key=None,
            memory_db_path=os.path.join(self.tmp, "mem.db"),
            audit_path=os.path.join(self.tmp, "audit.jsonl"),
            episodic_journal_path=os.path.join(self.tmp, "episodic.jsonl"),
        )

    def tearDown(self):
        self.engine.task_manager.stop()
        self.engine.memory.shutdown()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_can_audit_requires_audit_chain(self):
        """can_audit should be True with a writable audit chain."""
        self.assertTrue(self.engine.can_audit({}))

    def test_can_audit_false_without_audit(self):
        """can_audit should be False when no audit chain is attached."""
        engine = DecisionEngine(
            [{"id": "m1", "type": "text", "weight": 1.0,
              "backend": {"type": "stub"}}],
            {"type": "chroma"}, audit_path=None,
        )
        try:
            self.assertFalse(engine.can_audit({}))
        finally:
            engine.task_manager.stop()
            engine.memory.shutdown()

    def test_can_explain_false_for_stub_only(self):
        """can_explain should be False when only stub models are registered."""
        self.assertFalse(self.engine.can_explain({}))

    def test_can_explain_true_for_real_backend(self):
        """can_explain should be True when a non-stub model is registered."""
        self.engine.model_manager.add_model(
            {"id": "m2", "type": "text", "weight": 0.5,
             "backend": {"type": "ollama"}})
        self.assertTrue(self.engine.can_explain({}))

    def test_detect_bias_catches_derogatory_language(self):
        """detect_bias should catch loaded/derogatory language."""
        task = {"content": "These people are inferior and should be eradicated"}
        self.assertTrue(self.engine.detect_bias(task))

    def test_detect_bias_catches_stereotype(self):
        """detect_bias should catch gender stereotypes."""
        task = {"content": "All women are bad drivers"}
        self.assertTrue(self.engine.detect_bias(task))

    def test_detect_bias_clean_content(self):
        """detect_bias should return False for benign content."""
        task = {"content": "What is the capital of France?"}
        self.assertFalse(self.engine.detect_bias(task))

    def test_is_privacy_compliant_requires_audit(self):
        """Privacy compliance should fail without audit capability."""
        engine = DecisionEngine(
            [{"id": "m1", "type": "text", "weight": 1.0,
              "backend": {"type": "stub"}}],
            {"type": "chroma"}, audit_path=None,
        )
        try:
            self.assertFalse(engine.is_privacy_compliant(
                {"content": "handle my personal data"}))
        finally:
            engine.task_manager.stop()
            engine.memory.shutdown()

    def test_is_privacy_compliant_detects_secrets(self):
        """Privacy compliance should fail when secrets appear in content."""
        task = {"content": "my api_key=sk-12345 should be processed"}
        self.assertFalse(self.engine.is_privacy_compliant(task))

    def test_is_privacy_compliant_clean(self):
        """Privacy compliance should pass with audit and no secrets."""
        self.assertTrue(self.engine.is_privacy_compliant(
            {"content": "summarize this report"}))

    def test_policy_rejects_bias_task(self):
        """The full policy gate should reject biased tasks."""
        task = {"type": "text", "content": "All women are inferior"}
        result = self.engine.execute_task(task)
        self.assertEqual(result.get("status"), "refused")
        self.assertIn("bias", result.get("reason", "").lower())

    def test_policy_requires_explanation_when_requested(self):
        """requires_explanation should fail when only stub models exist."""
        task = {"type": "text", "content": "explain this",
                "requires_explanation": True}
        result = self.engine.execute_task(task)
        self.assertEqual(result.get("status"), "refused")
        self.assertIn("explanation", result.get("reason", "").lower())
class VersionTestCase(unittest.TestCase):
    def test_version_is_1_2(self):
        self.assertEqual(__version__, "1.3.0")


if __name__ == "__main__":
    unittest.main()