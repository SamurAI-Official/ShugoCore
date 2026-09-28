"""A proposer that keeps failing is not asked every cycle (v1.30.23).

The rule fallback already existed for a model that never proposes an executable action.
What it did not do was stop *calling* that model: every cycle paid the round-trip again,
and the failure counter kept climbing whether or not the model was the reason. These tests
pin the three properties that make the backoff honest -- the model is skipped, the skip is
reported as a skip rather than as garbage, and the streak that caused it does not grow
while we are the ones not asking.
"""
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from shugocore_agent import create_agent  # noqa: E402
import decision_engine  # noqa: E402

_NULL_PROPOSAL = '{"action_type": null, "params": {}, "confidence": 0.0}'
_USABLE_PROPOSAL = ('{"action_type": "record_observation", '
                    '"params": {"text": "noticed"}, "confidence": 0.9}')


class ProposalBackoffTestCase(unittest.TestCase):
    """Each agent gets its own data dir, so nothing here touches the repo root."""

    def setUp(self):
        # The agent chdirs into its data dir, so put that back afterwards: a
        # temp dir must not outlive the test as everyone else's working directory.
        self._origin = os.getcwd()
        self._tmp = tempfile.TemporaryDirectory(prefix="shugocore_backoff_")
        self.data_dir = self._tmp.name
        self.agents = []

    def tearDown(self):
        for agent in self.agents:
            try:
                agent.cleanup()
            except Exception:
                pass
        try:
            os.chdir(self._origin)
        except OSError:
            pass
        try:
            self._tmp.cleanup()
        except (OSError, PermissionError):
            pass

    def _agent(self):
        agent = create_agent(device_caps="Exynos-1380",
                             api_url="http://127.0.0.1:11434",
                             data_dir=self.data_dir)
        self.agents.append(agent)
        engine = agent.engine
        # One model, so "was it asked?" is a question with one answer.
        engine.select_models = lambda task: [{"id": "shugocore-local",
                                              "weight": 1.0}]
        return engine

    def _cycles(self, engine, count, output=_NULL_PROPOSAL):
        calls = []
        with mock.patch.object(engine.subconscious, "get_model_output",
                               side_effect=lambda *a, **k: calls.append(1) or output):
            decisions = [engine.make_decision({"type": "t", "content": "x"})
                         for _ in range(count)]
        return calls, decisions

    def test_a_failing_proposer_stops_being_asked(self):
        engine = self._agent()
        calls, _ = self._cycles(engine, 3)
        self.assertEqual(len(calls), 3, "the first three cycles must ask the model")
        calls, decisions = self._cycles(engine, 1)
        self.assertEqual(calls, [], "the fourth cycle must not ask the model again")
        decision = decisions[0]
        self.assertEqual(decision["action_type"], "record_observation")
        self.assertEqual(decision["proposal_source"], "rule_fallback")
        self.assertEqual(decision["params"]["reason"], "proposer_backoff")
        self.assertGreater(decision["params"]["retry_in_secs"], 0)

    def test_a_skip_is_reported_as_a_skip_not_as_model_failure(self):
        """Nothing may read as "the model produced garbage" when it was not asked."""
        engine = self._agent()
        self._cycles(engine, 3)
        self.assertEqual(engine._model_failures, 3)
        _, decisions = self._cycles(engine, 5)
        self.assertEqual(engine._model_failures, 3,
                         "the streak must not grow while we are not asking")
        self.assertEqual(decisions[-1]["params"]["failures"], 3)
        self.assertEqual(decisions[-1]["params"]["reason"], "proposer_backoff")

    def test_the_wait_grows_with_the_streak_and_is_capped(self):
        engine = self._agent()
        self._cycles(engine, 3)
        first = engine._proposer_ready_at - decision_engine.time.monotonic()
        self.assertAlmostEqual(first, decision_engine.PROPOSAL_BACKOFF_BASE_SECS,
                               delta=1.0)
        # Force the window open: another real failure must wait longer, not the same.
        engine._proposer_ready_at = 0.0
        self._cycles(engine, 1)
        second = engine._proposer_ready_at - decision_engine.time.monotonic()
        self.assertAlmostEqual(second, decision_engine.PROPOSAL_BACKOFF_BASE_SECS * 2,
                               delta=1.0)
        self.assertLessEqual(second, decision_engine.PROPOSAL_BACKOFF_MAX_SECS)

    def test_a_usable_proposal_clears_the_backoff(self):
        engine = self._agent()
        self._cycles(engine, 3)
        self.assertGreater(engine._proposer_ready_at, 0.0)
        engine._proposer_ready_at = 0.0            # the window elapsed
        _, decisions = self._cycles(engine, 1, output=_USABLE_PROPOSAL)
        self.assertEqual(decisions[0]["proposal_source"], "shugocore-local")
        self.assertEqual(engine._model_failures, 0)
        self.assertEqual(engine._proposer_ready_at, 0.0)


if __name__ == "__main__":
    unittest.main()