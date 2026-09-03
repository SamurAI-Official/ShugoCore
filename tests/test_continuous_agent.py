"""
ShugoCore continuous agent tests
==================================

Tests for the continuous observe-act daemon (continuous_agent.py): lifecycle,
bounded iteration, bounded wall-clock, statistics, and idle behavior.
"""

import os
import shutil
import tempfile
import time
import unittest

from continuous_agent import ContinuousAgent


class ContinuousAgentTestCase(unittest.TestCase):
    """Tests the ContinuousAgent loop-safety contract."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="shugocore_cont_")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _engine(self, max_seconds: float = None):
        from decision_engine import DecisionEngine
        return DecisionEngine(
            [{"id": "stub", "type": "text", "weight": 1.0,
              "backend": {"type": "stub"}}],
            {"type": "chroma"},
            memory_db_path=os.path.join(self.tmp, "mem.db"),
            audit_path=os.path.join(self.tmp, "audit.jsonl"),
            episodic_journal_path=os.path.join(self.tmp, "episodic.jsonl"),
        )

    def test_lifecycle_start_stop_status(self):
        """Agent should start, run a bounded number of loops, and stop."""
        agent = ContinuousAgent(
            engine=self._engine(),
            interval=0.01,
            max_iterations=3,
        )
        agent.start()
        agent.await_stop(timeout=10.0)
        agent.stop()
        status = agent.status()
        self.assertGreaterEqual(status["loops"], 1)
        self.assertGreaterEqual(status["tasks"], 1)
        self.assertIn(status["governor_state"],
                      ("idle", "observing", "paused", "safe_state"))

    def test_max_iterations_bounds_loop(self):
        """max_iterations should cap the loop deterministically."""
        agent = ContinuousAgent(
            engine=self._engine(),
            interval=0.01,
            max_iterations=2,
        )
        agent.start()
        agent.await_stop(timeout=10.0)
        agent.stop()
        self.assertEqual(agent.status()["loops"], 2)

    def test_max_seconds_bounds_loop(self):
        """max_seconds should cap wall-clock runtime."""
        agent = ContinuousAgent(
            engine=self._engine(),
            interval=0.05,
            max_seconds=0.6,
        )
        start = time.monotonic()
        agent.start()
        agent.await_stop(timeout=10.0)
        agent.stop()
        elapsed = time.monotonic() - start
        self.assertLess(elapsed, 5.0)

    def test_task_source_iterator_drives_loop(self):
        """A task-source iterator should drive the loop with its own tasks."""
        tasks = iter([
            {"type": "text", "content": "one"},
            {"type": "text", "content": "two"},
        ])
        agent = ContinuousAgent(
            engine=self._engine(),
            interval=0.01,
            task_source=tasks,
            max_iterations=2,
        )
        agent.start()
        agent.await_stop(timeout=10.0)
        agent.stop()
        self.assertEqual(agent.status()["tasks"], 2)

    def test_task_source_callable_drives_loop(self):
        """A callable task source should feed tasks until if returns None."""
        calls = {"n": 0}
        def source():
            calls["n"] += 1
            if calls["n"] > 2:
                return None
            return {"type": "text", "content": "probe"}
        agent = ContinuousAgent(
            engine=self._engine(),
            interval=0.01,
            task_source=source,
            max_iterations=None,
        )
        agent.start()
        time.sleep(0.25)
        agent.stop()
        self.assertGreaterEqual(calls["n"], 2)

    def test_honors_paused_governor(self):
        """The loop should sleep (not spam refusals) when governor is paused."""
        engine = self._engine()
        agent = ContinuousAgent(
            engine=engine,
            interval=0.01,
            max_iterations=5,
        )
        engine.governor.pause("test pause")
        agent.start()
        agent.await_stop(timeout=10.0)
        agent.stop()
        # The loop should still complete iterations (idle passes) but
        # process zero tasks.

        self.assertEqual(agent.status()["tasks"], 0)

    def test_shutdown_no_error(self):
        """stop() should be idempotent and safe to call twice."""
        agent = ContinuousAgent(engine=self._engine(), interval=0.01,
                                max_iterations=1)
        agent.start()
        agent.await_stop(timeout=10.0)
        agent.stop()
        agent.stop()  # second call: safe no-op

    def test_status_snapshot_fields(self):
        """status() should expose all dashboard fields."""
        agent = ContinuousAgent(engine=self._engine(), interval=0.01,
                                max_iterations=1)
        agent.start()
        agent.await_stop(timeout=10.0)
        agent.stop()
        status = agent.status()
        for key in ("running", "loops", "tasks", "successes", "failures",
                     "governor_state", "fallback_mode", "uptime_seconds",
                     "tier1_backlog", "tier2_facts"):
            self.assertIn(key, status)


if __name__ == "__main__":
    unittest.main()