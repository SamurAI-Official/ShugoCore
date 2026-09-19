"""Tests for Tier 2 entity graph helpers (link_entities, query_subgraph)."""
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, "")

from memory_system import SemanticMemory


class EntityGraphSqliteTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="shugocore_egraph_")
        self.mem = SemanticMemory(db_path=os.path.join(self.tmp, "mem.db"))

    def tearDown(self):
        try:
            self.mem.close()
        finally:
            shutil.rmtree(self.tmp, ignore_errors=True)

    def _seed(self):
        f1 = self.mem.store_fact("incident tape_api down due to ollama host")
        f2 = self.mem.store_fact("billing service depends on tape_api")
        return f1, f2

    def test_link_entities_idempotent(self):
        f, _ = self._seed()
        first = self.mem.link_entities(f, ["billing"])
        second = self.mem.link_entities(f, ["billing"])
        # Same name linked twice => same entity, idempotent link table row.
        facts = self.mem.facts_about("billing")
        self.assertEqual(len(facts), 1)
        self.assertEqual(first, 1)
        self.assertEqual(second, 1)

    def test_link_entities_makes_facts_about_queryable(self):
        f, _ = self._seed()
        self.mem.link_entities(f, ["billing", "sre"])
        self.assertEqual(len(self.mem.facts_about("billing")), 1)
        self.assertEqual(len(self.mem.facts_about("sre")), 1)
        # Facts that never mention billing are not linked.
        self.assertEqual(len(self.mem.facts_about("unrelated")), 0)

    def test_query_subgraph_returns_bounded_shape(self):
        self._seed()
        sg = self.mem.query_subgraph("tape_api", depth=2, limit=10)
        self.assertEqual(sg["root"], "tape_api")
        self.assertLessEqual(len(sg["nodes"]), 10)
        self.assertIsInstance(sg["nodes"], list)
        self.assertIsInstance(sg["edges"], list)
        for node in sg["nodes"]:
            self.assertIn("id", node)
            self.assertIn("name", node)
            self.assertIn("hops", node)
        # tape_api should be present as the root node.
        names = [n["name"] for n in sg["nodes"]]
        self.assertIn("tape_api", names)

    def test_query_subgraph_unknown_entity_returns_only_root_or_empty(self):
        self._seed()
        sg = self.mem.query_subgraph("nope", depth=1, limit=5)
        self.assertEqual(sg["root"], "nope")
        self.assertEqual(sg["nodes"], [])

    def test_query_subgraph_depth_one_reaches_immediate_peers(self):
        self._seed()
        # Explicitly link "billing" (it's not auto-extracted from lowercase
        # prose) — exercising the new link_entities API on the graph walk.
        self.mem.link_entities(self.mem.facts_about("tape_api")[-1]["id"],
                               ["billing"])
        sg = self.mem.query_subgraph("tape_api", depth=1, limit=10)
        names = [n["name"] for n in sg["nodes"]]
        self.assertIn("billing", names)
        for node in sg["nodes"]:
            self.assertLessEqual(node["hops"], 1)

    def test_query_subgraph_respects_limit(self):
        self._seed()
        # Many nodes — tiny limit must cap the subgraph hard.
        for i in range(5):
            self.mem.store_fact(
                f"host{i} connects to tape_api and service{i}")
        sg = self.mem.query_subgraph("tape_api", depth=3, limit=4)
        self.assertLessEqual(len(sg["nodes"]), 4)


class EntityGraphPgParityTestCase(unittest.TestCase):
    """PgSemanticMemory mirrors the SQLite graph API (mock connection)."""

    def test_pg_has_link_and_subgraph_methods(self):
        from unittest import mock
        import pg_memory
        from pg_memory import PgSemanticMemory

        class FakeCursor:
            def __init__(self, conn):
                self._conn = conn
                self._results = []
                self.rowcount = 0

            def execute(self, sql, params=None):
                from tests.test_pg_memory import _flatten_sql
                normalized = " ".join(_flatten_sql(sql).split())
                self._conn.executed.append((normalized, params))
                self._results = []
                for needle, rows in (self._conn.script or {}).items():
                    if needle in normalized and rows is not None:
                        self._results = rows
                        self.rowcount = len(rows)
                        break
                if not self._results and "RETURNING" in normalized:
                    self._results = [[self._conn.next_auto_id()]]
                    self.rowcount = 1

            def fetchone(self):
                return self._results[0] if self._results else None

            def fetchall(self):
                return list(self._results)

        class FakeConnection:
            def __init__(self):
                self.executed = []
                self.script = {
                    "SELECT e.id, e.name, e.mention_count": [[1, "tape_api", 3]],
                    "e2.name, e2.mention_count, fe2.fact_id": [
                        [2, "billing", 2, 7]],
                    "SELECT id FROM shugocore_entities WHERE name": [],
                }
                self._auto_id = 100

            def next_auto_id(self):
                self._auto_id += 1
                return self._auto_id

            def cursor(self):
                return FakeCursor(self)

            def commit(self):
                pass

            def close(self):
                pass

        conn = FakeConnection()
        with mock.patch.object(pg_memory, "_HAS_PSYCOPG", True):
            store = PgSemanticMemory("postgresql://db/fleet", connection=conn)
        # API is present.
        self.assertTrue(callable(store.link_entities))
        self.assertTrue(callable(store.query_subgraph))
        # link_entities issues INSERT/SELECT over entities + fact_entities.
        store.link_entities(1, ["billing"])
        sqls = [s for s, _ in conn.executed]
        self.assertTrue(any("INSERT INTO shugocore_entities" in s
                            for s in sqls))
        # query_subgraph walks the peer JOIN.
        sg = store.query_subgraph("tape_api", depth=1, limit=5)
        self.assertEqual(sg["root"], "tape_api")
        store.close()


if __name__ == "__main__":
    unittest.main()