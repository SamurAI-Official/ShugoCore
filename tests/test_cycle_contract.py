"""The cycle contract: the vocabulary, the trail, and which source decided.

`shugocore_agent` defines an eight-stage pipeline and a closed set of cycle
outcomes, and reports both per tick. Those two facts -- what ran, and what backed
the decision -- are what an operator reads to know whether the node is
orchestrating or idling, so they are pinned here rather than trusted.

The reason this file exists: `last_cycle_result["decision_source"]` was measured
returning the wrong source on 3 of 5 consecutive live cycles. `_decision_source`
is reset to "none" at the top of every tick, and the record read it *before* the
assignment that fills it ran -- so the field was permanently "none" while
`get_status()["decision_source"]`, which reads the attribute directly, was
correct. Two fields in one payload disagreeing is the shape this project keeps
having to fix, so the last class below asserts they agree.
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from shugocore_agent import (CYCLE_OUTCOMES, PIPELINE_STAGES,  # noqa: E402
                             _OUTCOME_TRAILS, create_agent)

# The minimal engine result the contract is defined over. It carries its own
# stage list, which is the case that matters: a result that reports its stages.
_NO_ACTION = {"status": "error", "outcome": "no_viable_action",
              "message": "no viable action proposed by the model ensemble",
              "call_errors": {}, "stages": ["GATE", "DECIDE", "RECORD"]}


def _result(**overrides):
    payload = dict(_NO_ACTION)
    payload.update(overrides)
    return payload


class TheVocabularyTestCase(unittest.TestCase):
    """A closed set, and a trail for every member of it."""

    def test_the_canonical_trails_cover_every_outcome(self):
        self.assertEqual(sorted(_OUTCOME_TRAILS), sorted(CYCLE_OUTCOMES),
                         "a cycle outcome with no canonical trail (or a trail "
                         "for an outcome that is not in the vocabulary) is a "
                         "contract the reader cannot rely on")

    def test_every_trail_starts_with_observe(self):
        for outcome, trail in _OUTCOME_TRAILS.items():
            self.assertTrue(trail, f"{outcome} has an empty trail")
            self.assertEqual(trail[0], "OBSERVE",
                             f"{outcome} does not begin at OBSERVE: {trail}")

    def test_every_trail_stage_is_a_pipeline_stage(self):
        for outcome, trail in _OUTCOME_TRAILS.items():
            for stage in trail:
                self.assertIn(stage, PIPELINE_STAGES,
                              f"{outcome} names a stage that is not in the "
                              f"pipeline: {stage}")



class TheReportedTrailTestCase(unittest.TestCase):
    """What the node reports as run is what actually ran -- or is marked."""

    def setUp(self):
        self.agent = create_agent(device_caps="desktop",
                                  api_url="http://127.0.0.1:11434")
        self.agent.engine = mock.MagicMock()

    def tearDown(self):
        try:
            self.agent.cleanup()
        except Exception:
            pass

    def test_a_reported_trail_is_used_verbatim(self):
        """The engine's own stages win over the canonical table."""
        self.agent.engine.execute_task.return_value = {
            "status": "refused", "outcome": "policy_block",
            "reason": "consent_required", "stages": ["GATE", "RECORD"]}
        self.agent.tick()
        stages = self.agent.get_status()["last_cycle_result"]["stages"]
        self.assertEqual(stages, ["OBSERVE", "GATE", "RECORD"])
        self.assertNotEqual(
            list(stages), list(_OUTCOME_TRAILS["POLICY_BLOCK"]),
            "the canonical POLICY_BLOCK trail includes DECIDE; a result that "
            "reports only GATE+RECORD must not be padded out to match it")

    def test_a_result_without_stages_falls_back_to_the_canonical_trail(self):
        """Documented inference, so it cannot be mistaken for observation.

        `_OUTCOME_TRAILS` is the minimum honest trail for an outcome, not a
        measurement: when a result carries no `stages` the node has nothing it
        saw, so it reports the canonical shape. Pinned so a future change makes
        that decision deliberately rather than by accident.
        """
        self.agent.engine.execute_task.return_value = _result(stages=None)
        self.agent.tick()
        stages = self.agent.get_status()["last_cycle_result"]["stages"]
        self.assertEqual(stages, list(_OUTCOME_TRAILS["NO_ACTION"]))

    def test_a_blocked_cycle_never_reports_success(self):
        for outcome, reason in (("policy_block", "no external consent grant"),
                                ("governor_block", "governor refused")):
            with self.subTest(outcome=outcome):
                self.agent.engine.execute_task.return_value = {
                    "status": "refused", "outcome": outcome, "reason": reason,
                    "stages": ["GATE", "RECORD"]}
                self.agent.tick()
                result = self.agent.get_status()["last_cycle_result"]
                self.assertNotEqual(result["outcome"], "SUCCESS")
                self.assertFalse(result["executed"])
                self.assertNotIn("EXECUTE", result["stages"])

    def test_the_engine_reports_a_trail_the_canonical_table_never_has(self):
        """VERIFY_ATTENTION is engine-only: proof the live trail is observed.

        No canonical trail contains it, so a run that stamps it can only have
        got it from the engine -- which is why the live path is a measurement
        and not a default.
        """
        for trail in _OUTCOME_TRAILS.values():
            self.assertNotIn("VERIFY_ATTENTION", trail)
        self.agent.engine.execute_task.return_value = {
            "status": "success", "outcome": "success",
            "stages": ["VERIFY_ATTENTION", "GATE", "RECORD"]}
        self.agent.tick()
        stages = self.agent.get_status()["last_cycle_result"]["stages"]
        self.assertIn("VERIFY_ATTENTION", stages)


class TheDecisionSourceTestCase(unittest.TestCase):
    """Which source decided *this* cycle -- in every field that reports it."""

    def setUp(self):
        self.agent = create_agent(device_caps="desktop",
                                  api_url="http://127.0.0.1:11434")
        self.agent.engine = mock.MagicMock()

    def tearDown(self):
        try:
            self.agent.cleanup()
        except Exception:
            pass

    def _succeeds_with(self, source):
        self.agent.engine.execute_task.return_value = {
            "status": "success", "outcome": "success", "recorded": True,
            "proposal_source": source, "action_type": "record_observation",
            "stages": ["GATE", "RECORD"]}

    def test_the_recorded_source_is_this_cycles_source(self):
        """The regression: this used to be permanently "none"."""
        self._succeeds_with("prism-ml/bonsai-27b")
        self.agent.tick()
        status = self.agent.get_status()
        self.assertEqual(status["last_cycle_result"]["decision_source"],
                         "prism-ml/bonsai-27b",
                         "the per-cycle record named a different source than "
                         "the cycle it describes")
        self.assertEqual(status["decision_source"], "prism-ml/bonsai-27b")

    def test_the_record_and_the_status_field_agree(self):
        """One source of truth: two fields in one payload must not disagree."""
        self._succeeds_with("zai-org/glm-4.6v-flash")
        self.agent.tick()
        status = self.agent.get_status()
        self.assertEqual(status["last_cycle_result"]["decision_source"],
                         status["decision_source"])

    def test_the_counters_name_the_same_source(self):
        self._succeeds_with("prism-ml/bonsai-27b")
        self.agent.tick()
        by_source = self.agent.get_status()["loop"]["by_source"]
        self.assertIn("prism-ml/bonsai-27b", by_source,
                      f"the accounting did not record the real source: {by_source}")

    def test_a_cycle_with_no_proposal_reports_none(self):
        self.agent.engine.execute_task.return_value = _result()
        self.agent.tick()
        status = self.agent.get_status()
        self.assertEqual(status["last_cycle_result"]["decision_source"], "none")
        self.assertEqual(status["decision_source"], "none")

    def test_the_source_is_not_the_previous_cycles(self):
        """Reset-then-read ordering: it must be this tick's, not a stale one."""
        self._succeeds_with("first-model")
        self.agent.tick()
        self._succeeds_with("second-model")
        self.agent.tick()
        status = self.agent.get_status()
        self.assertEqual(status["last_cycle_result"]["decision_source"],
                         "second-model")


if __name__ == "__main__":
    unittest.main()
