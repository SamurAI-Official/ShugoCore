"""Tests for per-agent memory policies (shared_rw / shared_read / isolated)."""
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, "")

from memory_system import CoreIdentity, MemoryManager, SemanticMemory


class MemoryPolicyTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="shugocore_mempol_")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _shared_semantic(self):
        return SemanticMemory(db_path=os.path.join(self.tmp, "shared.db"))

    def _make(self, **kwargs):
        return MemoryManager(agent_id="test", auto_start=False, **kwargs)

    def test_shared_rw_uses_passed_instances(self):
        sem = self._shared_semantic()
        core = CoreIdentity()
        mm = self._make(policy="shared_rw", semantic=sem, core=core)
        self.assertIs(mm.tier2, sem)
        self.assertIs(mm.tier3, core)
        self.assertFalse(mm.read_only_tier2)
        mm.shutdown()

    def test_shared_read_uses_passed_but_blocks_writes(self):
        sem = self._shared_semantic()
        mm = self._make(policy="shared_read", semantic=sem)
        self.assertIs(mm.tier2, sem)
        self.assertTrue(mm.read_only_tier2)
        # Write gate blocks the consolidation worker.
        self.assertFalse(mm.check_write_permission("tier2", "consolidation"))
        # Reads and other writers are unaffected.
        self.assertTrue(mm.check_write_permission("tier2", "shugonet"))
        mm.shutdown()

    def test_isolated_ignores_passed_instances(self):
        sem = self._shared_semantic()
        core = CoreIdentity()
        mm = self._make(policy="isolated", semantic=sem, core=core)
        self.assertIsNot(mm.tier2, sem)
        self.assertIsNot(mm.tier3, core)
        self.assertFalse(mm.read_only_tier2)  # isolated still owns its Tier 2
        mm.shutdown()

    def test_unknown_policy_raises(self):
        with self.assertRaises(ValueError):
            self._make(policy="nonsense")

    def test_consolidation_write_skip_for_shared_read(self):
        """A shared_read agent never writes consolidated facts to Tier 2."""
        sem = self._shared_semantic()
        mm = self._make(policy="shared_read", semantic=sem,
                        failure_promotion_threshold=1)
        mm.tier1.record("decision", {"status": "error", "detail": "boom"})
        stats = mm.consolidate_now()
        # Events are processed (drained) but zero facts are stored.
        self.assertGreaterEqual(stats["events_processed"], 1)
        self.assertEqual(stats["facts_stored"], 0)
        # The shared store is still empty.
        self.assertEqual(len(mm.tier2.search("boom", top_k=5, reinforce=False)), 0)
        mm.shutdown()

    def test_shared_rw_still_promotes_failures(self):
        sem = self._shared_semantic()
        mm = self._make(policy="shared_rw", semantic=sem,
                        failure_promotion_threshold=1)
        mm.tier1.record("decision", {"status": "error", "detail": "boom"})
        stats = mm.consolidate_now()
        self.assertGreaterEqual(stats["facts_stored"], 1)
        hits = mm.tier2.search("boom", top_k=5, reinforce=False)
        self.assertGreaterEqual(len(hits), 1)
        mm.shutdown()

    def test_default_policy_is_shared_rw(self):
        mm = self._make()
        self.assertEqual(mm.policy, "shared_rw")
        mm.shutdown()


if __name__ == "__main__":
    unittest.main()