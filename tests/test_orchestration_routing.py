"""Multi-model orchestration: the pick follows measured capacity.

The README says models are "selected and aggregated". What that means in code is
specific, and it is the part of orchestration an operator depends on: every
selected model is asked, each proposal is scored by
``confidence x (weight x learned performance)``, and the best-scoring executable
proposal becomes the decision.

The property worth pinning is that *measured* performance decides -- not order,
not registration, not the first model in the list. These tests hold everything
constant and move one thing at a time: the same two proposals must pick
differently when a model's performance drops, and differently again when its
weight drops. If either fails, the node is not routing by capacity.
"""
import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from decision_engine import DecisionEngine  # noqa: E402
from model_manager import ModelManager  # noqa: E402

# Alpha is more confident; beta less. With equal weight and equal (fresh)
# performance, confidence alone must choose alpha.
ALPHA = json.dumps({"action_type": "record_observation",
                    "params": {"text": "alpha"}, "confidence": 0.9})
BETA = json.dumps({"action_type": "record_observation",
                   "params": {"text": "beta"}, "confidence": 0.4})


class RoutingByCapacityTestCase(unittest.TestCase):
    """Two models, one decision, and only one thing changed at a time."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="shugocore_routing_")
        self.engine = DecisionEngine(
            models=[{"id": "alpha", "type": "text", "weight": 1.0,
                     "backend": {"type": "stub"}},
                    {"id": "beta", "type": "text", "weight": 1.0,
                     "backend": {"type": "stub"}}],
            vector_db_config={"type": "chroma"},
            memory_db_path=os.path.join(self.tmp, "mem.db"),
            audit_path=os.path.join(self.tmp, "audit.jsonl"),
            log_dir=self.tmp,
        )
        self.engine.subconscious.get_model_output = (
            lambda model_id, task, backend=None, action_schema=None:
            {"alpha": ALPHA, "beta": BETA}.get(str(model_id), ""))

    def tearDown(self):
        try:
            self.engine.shutdown()
        except Exception:
            pass
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _decide(self):
        return self.engine.make_decision(
            {"type": "text", "content": "note the charger is warm"})

    def test_both_models_are_asked_and_both_outputs_are_kept(self):
        """Selection is not "ask one and stop"."""
        decision = self._decide()
        self.assertEqual(sorted(decision.get("model_outputs") or {}),
                         ["alpha", "beta"])

    def test_the_higher_confidence_proposal_wins_when_all_else_is_equal(self):
        decision = self._decide()
        self.assertEqual(decision["proposal_source"], "alpha")
        self.assertEqual(decision["params"].get("text"), "alpha")

    def test_the_aggregate_is_the_sum_of_the_proposal_scores(self):
        """`aggregated_output` is a *capacity* aggregate, not a confidence one.

        Its name suggests the outputs were combined; what it actually records is
        the sum of ``weight x learned performance`` for every model that returned
        a parseable proposal -- so on a healthy two-model node it reads 2.0
        regardless of how confident either was. Pinned as measured, because a
        reader would otherwise take 2.0 for a confidence score.
        """
        decision = self._decide()
        self.assertEqual(decision.get("aggregated_output"), 1.0 + 1.0)
        self.assertNotEqual(decision.get("aggregated_output"),
                            decision.get("confidence"))

    def test_learned_performance_alone_changes_the_pick(self):
        """The orchestration property: capacity, not registration order."""
        self.assertEqual(self._decide()["proposal_source"], "alpha")
        self.engine.model_manager.set_model_performance("alpha", 0.2)
        decision = self._decide()
        self.assertEqual(
            decision["proposal_source"], "beta",
            "a model whose measured performance dropped still won the decision")

    def test_weight_alone_changes_the_pick(self):
        self.assertEqual(self._decide()["proposal_source"], "alpha")
        for model in self.engine.model_manager.models:
            if model["id"] == "alpha":
                model["weight"] = 0.1
        decision = self._decide()
        self.assertEqual(decision["proposal_source"], "beta",
                         "the configured weight did not affect the decision")

    def test_a_model_whose_output_is_unusable_is_not_counted_as_a_proposal(self):
        """Unparseable output must not silently score."""
        self.engine.subconscious.get_model_output = (
            lambda model_id, task, backend=None, action_schema=None:
            ALPHA if str(model_id) == "alpha" else "not json at all")
        decision = self._decide()
        self.assertEqual(decision["proposal_source"], "alpha")
        # Only alpha proposed, so only alpha's score is in the aggregate.
        self.assertEqual(decision.get("aggregated_output"), 1.0)


class TheAggregationIsTheEnginesTestCase(unittest.TestCase):
    """One aggregation, and it is the engine's.

    `ModelManager.aggregate_outputs` is a second implementation (weighted voting)
    that nothing calls. Pinned so that wiring it in -- which would give the engine
    two ways to combine the same proposals -- has to be a deliberate change.
    """

    def test_a_decision_does_not_call_model_manager_aggregate_outputs(self):
        calls = []
        original = ModelManager.aggregate_outputs

        def spy(manager, outputs):
            calls.append(outputs)
            return original(manager, outputs)

        ModelManager.aggregate_outputs = spy
        tmp = tempfile.mkdtemp(prefix="shugocore_routing_unused_")
        try:
            engine = DecisionEngine(
                models=[{"id": "alpha", "type": "text", "weight": 1.0,
                         "backend": {"type": "stub"}}],
                vector_db_config={"type": "chroma"},
                memory_db_path=os.path.join(tmp, "mem.db"),
                audit_path=os.path.join(tmp, "audit.jsonl"),
                log_dir=tmp,
            )
            engine.subconscious.get_model_output = (
                lambda model_id, task, backend=None, action_schema=None: ALPHA)
            engine.make_decision({"type": "text", "content": "anything"})
            try:
                engine.shutdown()
            except Exception:
                pass
        finally:
            ModelManager.aggregate_outputs = original
            shutil.rmtree(tmp, ignore_errors=True)
        self.assertEqual(
            calls, [],
            "ModelManager.aggregate_outputs was called: there would now be two "
            "aggregations of the same proposals")


if __name__ == "__main__":
    unittest.main()
