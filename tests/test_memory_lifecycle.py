"""Memory lifecycle: a closed agent must not keep its database locked.

Two facts make this file necessary. First, the defect: the semantic memory's sqlite
connection outlived the agent, so on Windows ``semantic_memory.db`` stayed locked after
cleanup -- 48 tests reported failures whose bodies had passed. Second, the hazard in the
obvious fix: ``MemoryManager.shutdown()`` closes every tier it can reach, and Tier 2/3 are
*documented* as shared between managers, so one agent's teardown could close a database a peer
is still using. `MemoryManager.close()` releases only what the manager created, and these
tests pin both halves: the handle really is released, and a lent tier really is not.
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from memory_system import MemoryManager, SemanticMemory  # noqa: E402
from shugocore_agent import create_agent  # noqa: E402


class TestCloseRespectsOwnership(unittest.TestCase):
    def test_a_lent_semantic_tier_survives_a_manager_close(self):
        shared = SemanticMemory(db_path=os.path.join(tempfile.mkdtemp(), "shared.db"))
        manager = MemoryManager(agent_id="lender", semantic=shared)
        manager.close()
        # A closed connection would raise on the next statement; the lender's must not be.
        self.assertGreaterEqual(shared.count(), 0)
        shared.store_fact("still writable after a peer closed")
        self.assertGreaterEqual(shared.count(), 1)
        shared.close()

    def test_an_owned_tier_is_released_and_close_is_idempotent(self):
        path = os.path.join(tempfile.mkdtemp(), "owned.db")
        manager = MemoryManager(agent_id="owner")
        manager.tier2 = SemanticMemory(db_path=path)
        manager._owns_tier2 = True
        manager.close()
        manager.close()  # must not raise on a second call
        # An open sqlite handle would refuse this on Windows; a released one allows it.
        os.replace(path, path + ".moved")

    def test_shutdown_still_closes_everything_it_can_reach(self):
        """The documented difference between the two methods, pinned."""
        path = os.path.join(tempfile.mkdtemp(), "shut.db")
        shared = SemanticMemory(db_path=path)
        MemoryManager(agent_id="shutdown", semantic=shared).shutdown()
        with self.assertRaises(Exception):
            shared.count()


class TestAgentCleanupReleasesTheDatabase(unittest.TestCase):
    @staticmethod
    def _databases(roots):
        found = []
        for root in roots:
            for dirpath, _dirs, files in os.walk(root):
                for name in files:
                    if name.endswith(".db"):
                        found.append(os.path.join(dirpath, name))
        return found

    def test_cleanup_leaves_no_locked_database(self):
        """The regression test for the defect that hid 48 failures: an agent whose handle
        outlived it. Renaming is the assertion, because Windows refuses to move a file that
        another handle still has open."""
        data_dir = tempfile.mkdtemp(prefix="shugo_mem_")
        working = os.getcwd()
        agent = create_agent(device_caps="Exynos-1380", data_dir=data_dir)
        try:
            databases = self._databases([data_dir, working])
            self.assertTrue(databases, "the agent created no database to check")
            agent.cleanup()
            for database in databases:
                os.replace(database, database + ".moved")
        finally:
            agent.cleanup()


if __name__ == "__main__":
    unittest.main()
