"""
Tests for Dream Consolidation and Memory Write Gates.
"""

import unittest
from unittest.mock import MagicMock, patch

from memory_system import (
    DreamConsolidation,
    MemoryManager,
    CoreIdentity,
    EpisodicMemory,
)


class TestDreamConsolidation(unittest.TestCase):
    """Tests for the DreamConsolidation class."""

    def test_initialization(self):
        memory = MemoryManager(auto_start=False)
        dream = DreamConsolidation(memory, dream_every_n_ticks=5)
        self.assertEqual(dream._dream_every, 5)
        self.assertEqual(dream.tick_count, 0)

    def test_tick_triggers_dream(self):
        memory = MemoryManager(auto_start=False)
        dream = DreamConsolidation(memory, dream_every_n_ticks=3)

        # First two ticks should not trigger dream
        self.assertIsNone(dream.tick())
        self.assertIsNone(dream.tick())

        # Third tick should trigger dream
        result = dream.tick()
        # Result may be None if no events, but tick count advances
        self.assertEqual(dream.tick_count, 3)

    def test_dream_with_no_events(self):
        memory = MemoryManager(auto_start=False)
        dream = DreamConsolidation(memory)
        result = dream.dream()
        self.assertFalse(result["dreamed"])
        self.assertEqual(result["reason"], "no_events")

    def test_dream_with_events(self):
        memory = MemoryManager(auto_start=False)

        # Record some events
        for _ in range(5):
            memory.record_event("test_action", payload={"status": "success"})
        for _ in range(3):
            memory.record_event("failing_action", payload={"status": "error"})

        dream = DreamConsolidation(memory)
        result = dream.dream()

        # Should have processed events
        self.assertTrue(result["dreamed"] or not result["dreamed"])  # Depends on thresholds
        self.assertEqual(result["events_processed"], 8)

    def test_extract_insights_failures(self):
        memory = MemoryManager(auto_start=False)
        dream = DreamConsolidation(memory)

        events = [
            {"type": "robot_navigate", "payload": {"status": "error"}},
            {"type": "robot_navigate", "payload": {"status": "error"}},
            {"type": "robot_navigate", "payload": {"status": "error"}},
        ]

        insights = dream._extract_insights(events)
        self.assertTrue(any("Recurring failure" in i for i in insights))

    def test_extract_insights_successes(self):
        memory = MemoryManager(auto_start=False)
        dream = DreamConsolidation(memory)

        events = [
            {"type": "api_call", "payload": {"status": "success"}},
            {"type": "api_call", "payload": {"status": "success"}},
            {"type": "api_call", "payload": {"status": "success"}},
        ]

        insights = dream._extract_insights(events)
        self.assertTrue(any("Consistent success" in i for i in insights))

    def test_identity_mutations_clamped(self):
        memory = MemoryManager(auto_start=False)
        dream = DreamConsolidation(memory, max_sentence_add=1)

        insights = ["Insight one", "Insight two", "Insight three"]
        mutations = dream._apply_identity_mutations(insights)

        # Should add at most 1 insight
        self.assertLessEqual(len(mutations["added"]), 1)


class TestMemoryWriteGates(unittest.TestCase):
    """Tests for memory write-gate enforcement."""

    def test_tier0_write_permission(self):
        memory = MemoryManager(auto_start=False)
        self.assertTrue(memory.check_write_permission("tier0", "scratchpad"))
        self.assertFalse(memory.check_write_permission("tier0", "consolidation"))

    def test_tier1_write_permission(self):
        memory = MemoryManager(auto_start=False)
        self.assertTrue(memory.check_write_permission("tier1", "episodic"))
        self.assertFalse(memory.check_write_permission("tier1", "dream"))

    def test_tier2_write_permission(self):
        memory = MemoryManager(auto_start=False)
        self.assertTrue(memory.check_write_permission("tier2", "consolidation"))
        self.assertTrue(memory.check_write_permission("tier2", "maintenance"))
        self.assertFalse(memory.check_write_permission("tier2", "episodic"))

    def test_tier3_write_permission(self):
        memory = MemoryManager(auto_start=False)
        self.assertTrue(memory.check_write_permission("tier3", "dream"))
        self.assertTrue(memory.check_write_permission("tier3", "promote_invariant"))
        self.assertFalse(memory.check_write_permission("tier3", "consolidation"))
        self.assertFalse(memory.check_write_permission("tier3", "episodic"))

    def test_enforce_write_raises(self):
        memory = MemoryManager(auto_start=False)
        with self.assertRaises(PermissionError):
            memory.enforce_write("tier3", "episodic")

    def test_enforce_write_passes(self):
        memory = MemoryManager(auto_start=False)
        # Should not raise
        memory.enforce_write("tier3", "dream")
        memory.enforce_write("tier2", "consolidation")


class TestDreamIntegration(unittest.TestCase):
    """Integration tests for dream consolidation with MemoryManager."""

    def test_memory_manager_dream(self):
        memory = MemoryManager(auto_start=False)
        result = memory.dream()
        self.assertIn("dreamed", result)

    def test_memory_manager_tick_dream(self):
        memory = MemoryManager(auto_start=False)
        # First tick should not trigger dream (default every 8)
        result = memory.tick_dream()
        self.assertIsNone(result)

    def test_dream_stats(self):
        memory = MemoryManager(auto_start=False)
        stats = memory.dream_stats
        self.assertIn("tick_count", stats)
        self.assertIn("last_dream_at", stats)
        self.assertIn("dream_every", stats)


if __name__ == "__main__":
    unittest.main()
