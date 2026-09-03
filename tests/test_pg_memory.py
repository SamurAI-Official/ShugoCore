"""Tests for the PostgreSQL/pgvector Tier 2 backend (pg_memory.py).

No live PostgreSQL is required: a scripted fake connection records every
executed SQL statement and returns canned rows, so the tests verify exact
SQL shape, parameter binding, embedding parity with the SQLite backend, and
fail-closed behavior.
"""

import json
import os
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pg_memory  # noqa: E402
from pg_memory import PgSemanticMemory, _vector_literal, open_semantic_memory  # noqa: E402


class FakeCursor:
    """Records executed SQL; serves scripted rows or safe defaults."""

    def __init__(self, conn):
        self._conn = conn
        self._results = []
        self.rowcount = 0

    def execute(self, sql, params=None):
        normalized = " ".join(str(sql).split())
        self._conn.executed.append((normalized, params))
        for needle, exc in (self._conn.fail_on or {}).items():
            if needle in normalized:
                raise exc
        self._results = []
        self.rowcount = self._conn.default_rowcount
        for needle, rows in (self._conn.script or {}).items():
            if needle in normalized:
                self._results = rows
                self.rowcount = len(rows)
                break
        if not self._results and "RETURNING" in normalized:
            # Auto-increment surrogate for INSERT ... RETURNING id.
            self._results = [[self._conn.next_auto_id()]]
            self.rowcount = 1

    def fetchone(self):
        return self._results[0] if self._results else None

    def fetchall(self):
        return list(self._results)


class FakeConnection:
    """Minimal psycopg2-connection stand-in driven by a script table."""

    def __init__(self, script=None, fail_on=None, default_rowcount=0):
        self.executed = []
        self.commits = 0
        self.script = script or {}
        self.fail_on = fail_on or {}
        self.default_rowcount = default_rowcount
        self.closed = False
        self._auto_id = 100

    def next_auto_id(self):
        self._auto_id += 1
        return self._auto_id

    def cursor(self):
        return FakeCursor(self)

    def commit(self):
        self.commits += 1

    def close(self):
        self.closed = True


def make_pg(**conn_kwargs):
    conn = FakeConnection(**conn_kwargs)
    with mock.patch.object(pg_memory, "_HAS_PSYCOPG", True):
        store = PgSemanticMemory("postgresql://user:pw@db:5432/fleet",
                                 connection=conn)
    return store, conn


class FailureModeTestCase(unittest.TestCase):
    """Construction must fail closed, loudly, and with actionable hints."""

    def test_missing_psycopg2_raises_install_hint(self):
        with mock.patch.object(pg_memory, "_HAS_PSYCOPG", False):
            with self.assertRaises(RuntimeError) as ctx:
                PgSemanticMemory("postgresql://db/fleet")
            self.assertIn("shugocore[postgres]", str(ctx.exception))

    def test_missing_psycopg2_hits_factory_dsn_dispatch(self):
        with mock.patch.object(pg_memory, "_HAS_PSYCOPG", False):
            with self.assertRaises(RuntimeError):
                open_semantic_memory("postgres://db/fleet")

    def test_table_prefix_is_strictly_validated(self):
        for bad in ("ShugoCore", "shugo; DROP TABLE users", "shugo core",
                    "shugocore-facts", "-lead", "9lead", "x" * 42, ""):
            with self.assertRaises(ValueError, msg=repr(bad)):
                conn = FakeConnection()
                with mock.patch.object(pg_memory, "_HAS_PSYCOPG", True):
                    PgSemanticMemory("postgresql://db/fleet",
                                     table_prefix=bad, connection=conn)

    def test_valid_table_prefixes_accepted(self):
        for good in ("shugocore", "fleet_a1", "a"):
            conn = FakeConnection()
            with mock.patch.object(pg_memory, "_HAS_PSYCOPG", True):
                store = PgSemanticMemory("postgresql://db/fleet",
                                         table_prefix=good, connection=conn)
            self.assertEqual(store._t_facts, f"{good}_facts")
            store.close()

    def test_extension_failure_is_fail_closed(self):
        conn = FakeConnection(fail_on={"CREATE EXTENSION":
                                       RuntimeError("permission denied")})
        with mock.patch.object(pg_memory, "_HAS_PSYCOPG", True):
            with self.assertRaises(RuntimeError) as ctx:
                PgSemanticMemory("postgresql://db/fleet", connection=conn)
        self.assertIn("CREATE EXTENSION vector", str(ctx.exception))
        self.assertIn("pgvector", str(ctx.exception))

    def test_schema_creates_extension_and_tables(self):
        store, conn = make_pg()
        sqls = [sql for sql, _ in conn.executed]
        self.assertIn("CREATE EXTENSION IF NOT EXISTS vector", sqls)
        facts = next(s for s in sqls
                     if "CREATE TABLE IF NOT EXISTS shugocore_facts" in s)
        self.assertIn("embedding vector(256)", facts)
        self.assertTrue(any("shugocore_entities" in s for s in sqls))
        self.assertTrue(any("shugocore_fact_entities" in s for s in sqls))
        store.close()

    def test_close_closes_connection(self):
        store, conn = make_pg()
        store.close()
        self.assertTrue(conn.closed)


def _parse_literal(literal):
    return [float(part) for part in literal.strip("[]").split(",")]


class StoreFactTestCase(unittest.TestCase):
    CONTENT = "Retry tape_api documented failure for model gpt-4 and openai.com"

    def test_store_fact_returns_id_and_serializes_vector(self):
        store, conn = make_pg()
        fact_id = store.store_fact(self.CONTENT, kind="error_pattern",
                                   metadata={"k": "v"})
        self.assertEqual(fact_id, 101)  # first auto id
        inserts = [(sql, params) for sql, params in conn.executed
                   if "INSERT INTO shugocore_facts" in sql]
        self.assertEqual(len(inserts), 1)
        sql, params = inserts[0]
        self.assertIn("%s::vector", sql)
        self.assertIn("RETURNING id", sql)
        content, kind, salience, created, accessed, literal, meta = params
        self.assertEqual(content, self.CONTENT)
        self.assertEqual(kind, "error_pattern")
        self.assertEqual(salience, 1.0)
        self.assertEqual(created, accessed)
        self.assertEqual(json.loads(meta), {"k": "v"})
        vector = _parse_literal(literal)
        self.assertEqual(len(vector), 256)
        self.assertGreater(sum(v * v for v in vector), 0.99)
        self.assertGreater(conn.commits, 0)

    def test_embedding_matches_sqlite_backend(self):
        from memory_system import SemanticMemory
        tmp = tempfile.mkdtemp(prefix="shugocore_pg_parity_")
        try:
            sqlite_store = SemanticMemory(
                db_path=os.path.join(tmp, "mem.db"), dimension=256)
            store, conn = make_pg()
            store.store_fact(self.CONTENT)
            literal = next(params[5] for sql, params in conn.executed
                           if "INSERT INTO shugocore_facts" in sql)
            expected = sqlite_store._embed(self.CONTENT)
            actual = _parse_literal(literal)
            for got, want in zip(actual, expected):
                self.assertAlmostEqual(got, want, places=12)
            sqlite_store.close()
            store.close()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_store_fact_sanitizes_content(self):
        store, conn = make_pg()
        store.store_fact("secret\x00fact\nwith\nnewlines" * 100)
        content = next(params[0] for sql, params in conn.executed
                       if "INSERT INTO shugocore_facts" in sql)
        self.assertNotIn("\x00", content)
        self.assertNotIn("\n", content)
        self.assertLessEqual(len(content), 2000)

    def test_store_fact_links_entities(self):
        store, conn = make_pg()
        store.store_fact(self.CONTENT)
        sqls = [sql for sql, _ in conn.executed]
        self.assertTrue(any("INSERT INTO shugocore_entities" in s
                            and "RETURNING id" in s for s in sqls))
        self.assertTrue(any("INSERT INTO shugocore_fact_entities" in s
                            and "ON CONFLICT DO NOTHING" in s for s in sqls))
        # Entity extraction must match the SQLite backend exactly.
        from memory_system import SemanticMemory
        tmp = tempfile.mkdtemp(prefix="shugocore_pg_entities_")
        try:
            sqlite_store = SemanticMemory(
                db_path=os.path.join(tmp, "mem.db"))
            self.assertEqual(store.extract_entities(self.CONTENT),
                             sqlite_store.extract_entities(self.CONTENT))
            sqlite_store.close()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_get_fact_shape_and_missing(self):
        row = (3, "cached fact", "fact", 2.0, 1,
               "2026-01-01T00:00:00+00:00", "2026-01-02T00:00:00+00:00",
               '{"x": "y"}')
        store, _ = make_pg(script={"SELECT id, content, kind": [row]})
        fact = store.get_fact(3)
        self.assertEqual(fact["id"], 3)
        self.assertEqual(fact["content"], "cached fact")
        self.assertEqual(fact["salience"], 2.0)
        self.assertEqual(fact["access_count"], 1)
        self.assertEqual(fact["metadata"], {"x": "y"})
        self.assertNotIn("similarity", fact)
        missing_store, _ = make_pg()
        self.assertIsNone(missing_store.get_fact(999))
        missing_store.close()


class SearchTestCase(unittest.TestCase):
    ROW = (1, "retry tape_api on failure", "error_pattern", 1.5, 2,
           "2026-01-01T00:00:00+00:00", "2026-01-02T00:00:00+00:00",
           '{"a": 1}', 0.87)

    def test_search_pushes_cosine_distance_to_db(self):
        script = {
            "FROM shugocore_facts WHERE salience": [self.ROW],
            "SELECT salience, access_count": [[1.5, 2]],
        }
        store, conn = make_pg(script=script)
        results = store.search("retry tape_api", top_k=5, min_salience=0.2)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["id"], 1)
        self.assertEqual(results[0]["similarity"], 0.87)
        self.assertEqual(results[0]["metadata"], {"a": 1})
        sql, params = next((sql, params) for sql, params in conn.executed
                           if "WHERE salience >= %s" in sql)
        self.assertIn("1 - (embedding <=> %s::vector) AS similarity", sql)
        self.assertIn("embedding <=> %s::vector < 1.0", sql)
        self.assertIn("ORDER BY embedding <=> %s::vector LIMIT %s", sql)
        self.assertEqual(params[1], 0.2)   # min_salience bound
        self.assertEqual(params[4], 5)     # top_k bound
        updates = [sql for sql, _ in conn.executed
                   if sql.startswith("UPDATE shugocore_facts SET salience")]
        self.assertEqual(len(updates), 1)  # reinforce on the hit

    def test_search_without_reinforce_skips_updates(self):
        script = {"FROM shugocore_facts WHERE salience": [self.ROW]}
        store, conn = make_pg(script=script)
        results = store.search("retry", reinforce=False)
        self.assertEqual(len(results), 1)
        self.assertFalse(any(sql.startswith("UPDATE")
                             for sql, _ in conn.executed))

    def test_reinforce_caps_and_increments(self):
        script = {"SELECT salience, access_count": [[9.9, 3]]}
        store, conn = make_pg(script=script)
        store.reinforce(7, boost=0.25, cap=10.0)
        sql, params = next((sql, params) for sql, params in conn.executed
                           if "SET salience = %s" in sql)
        self.assertEqual(params[0], 10.0)  # capped
        self.assertEqual(params[1], 4)     # access_count incremented
        self.assertEqual(params[3], 7)

    def test_reinforce_missing_fact_is_noop(self):
        store, conn = make_pg()
        store.reinforce(404)
        self.assertFalse(any(sql.startswith("UPDATE")
                             for sql, _ in conn.executed))


class DecayPruneGraphTestCase(unittest.TestCase):
    def test_decay_matches_sqlite_semantics(self):
        two_hours_ago = (datetime.now(timezone.utc)
                         - timedelta(hours=2)).isoformat()
        rows = [(1, 1.0, two_hours_ago), (2, 1.0, "garbage")]
        store, conn = make_pg(script={"SELECT id, salience, last_accessed":
                                      rows})
        decayed = store.decay(half_life_hours=1.0)
        self.assertEqual(decayed, 1)  # only the parseable, aged row
        updates = [(sql, params) for sql, params in conn.executed
                   if "UPDATE shugocore_facts SET salience" in sql]
        self.assertEqual(len(updates), 1)
        expected = 0.5 ** 2  # 2 hours over a 1-hour half-life
        # places=5: microsecond clock drift between the test's now() and
        # decay()'s now() perturbs the result by ~1e-8; any real formula
        # error (wrong exponent, wrong base) would miss by >= 1e-2.
        self.assertAlmostEqual(updates[0][1][0], expected, places=5)
        self.assertEqual(updates[0][1][1], 1)

    def test_prune_returns_rowcount(self):
        store, conn = make_pg(default_rowcount=5)
        self.assertEqual(store.prune(0.05), 5)
        sql, params = next((sql, params) for sql, params in conn.executed
                           if sql.startswith("DELETE FROM"))
        self.assertEqual(sql, "DELETE FROM shugocore_facts WHERE salience < %s")
        self.assertEqual(params, (0.05,))

    def test_facts_about_joins_graph_tables(self):
        row = (1, "fact", "fact", 1.0, 0, "t1", "t2", "{}")
        script = {"JOIN shugocore_fact_entities fe": [row]}
        store, conn = make_pg(script=script)
        facts = store.facts_about("tape_api", limit=3)
        self.assertEqual(len(facts), 1)
        self.assertEqual(facts[0]["id"], 1)
        sql, params = next((sql, params) for sql, params in conn.executed
                           if "JOIN shugocore_fact_entities fe" in sql)
        self.assertIn("JOIN shugocore_entities e ON e.id = fe.entity_id", sql)
        self.assertIn("ORDER BY f.salience DESC, f.id DESC LIMIT %s", sql)
        self.assertEqual(params, ("tape_api", 3))

    def test_related_entities_groups_by_entity(self):
        row = ("openai.com", 4, 2)
        script = {"COUNT(*) AS co_occurrences": [row]}
        store, conn = make_pg(script=script)
        related = store.related_entities("tape_api", limit=5)
        self.assertEqual(related,
                         [{"name": "openai.com", "mention_count": 4,
                           "co_occurrences": 2}])
        sql, params = next((sql, params) for sql, params in conn.executed
                           if "COUNT(*) AS co_occurrences" in sql)
        self.assertIn("GROUP BY e2.id, e2.name, e2.mention_count", sql)
        self.assertEqual(params, ("tape_api", "tape_api", 5))

    def test_entity_names_ordered_by_mentions(self):
        script = {"SELECT name FROM shugocore_entities": [["openai.com"]]}
        store, conn = make_pg(script=script)
        self.assertEqual(store.entity_names(limit=7), ["openai.com"])
        sql, params = next((sql, params) for sql, params in conn.executed
                           if sql.startswith("SELECT name FROM"))
        self.assertIn("ORDER BY mention_count DESC LIMIT %s", sql)
        self.assertEqual(params, (7,))


class FactoryAndIntegrationTestCase(unittest.TestCase):
    def test_factory_dispatches_on_scheme(self):
        pg, _ = make_pg()
        with mock.patch.object(pg_memory, "_HAS_PSYCOPG", True), \
                mock.patch.object(pg_memory, "PgSemanticMemory",
                                  return_value=pg) as factory:
            got = open_semantic_memory("postgresql://db/fleet")
            self.assertIs(got, pg)
            factory.assert_called_once()
            got = open_semantic_memory("postgres://db/fleet")
            self.assertIs(got, pg)
            self.assertEqual(factory.call_count, 2)

    def test_factory_defaults_to_sqlite(self):
        tmp = tempfile.mkdtemp(prefix="shugocore_factory_sqlite_")
        try:
            store = open_semantic_memory(os.path.join(tmp, "mem.db"))
            from memory_system import SemanticMemory
            self.assertIsInstance(store, SemanticMemory)
            store.close()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_memory_manager_accepts_pg_backend(self):
        from memory_system import MemoryManager
        store, _ = make_pg()
        manager = MemoryManager(agent_id="fleet", semantic=store,
                                auto_start=False)
        self.assertIs(manager.tier2, store)
        result = manager.retrieve_context("tape_api status", top_k=2)
        self.assertIn("semantic", result)
        self.assertIn("graph", result)
        manager.shutdown()
        store.close()


if __name__ == "__main__":
    unittest.main()
