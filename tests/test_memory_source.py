"""Tests for env-driven Tier 2 storage selection (v1.30.4)."""
import os
import unittest
from unittest import mock

from decision_engine import _resolve_memory_source


class ResolveMemorySourceTestCase(unittest.TestCase):

    def test_no_env_returns_pass_through(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            self.assertEqual(
                _resolve_memory_source("semantic_memory.db"),
                "semantic_memory.db")

    def test_dsn_env_wins(self):
        with mock.patch.dict(
                os.environ,
                {"SHUGOCORE_MEMORY_DSN":
                     "postgres://user:pw@db:5432/fleet",
                 "SHUGOCORE_MEMORY_BACKEND": "postgres"}):
            self.assertEqual(
                _resolve_memory_source("semantic_memory.db"),
                "postgres://user:pw@db:5432/fleet")

    def test_postgres_backend_with_path_falls_back_to_path(self):
        with mock.patch.dict(
                os.environ,
                {"SHUGOCORE_MEMORY_DSN": "",
                 "SHUGOCORE_MEMORY_BACKEND": "postgres"}):
            self.assertEqual(
                _resolve_memory_source("fleet.db"),
                "fleet.db")

    def test_postgres_backend_without_dsn_or_path_returns_none(self):
        with mock.patch.dict(
                os.environ,
                {"SHUGOCORE_MEMORY_DSN": "",
                 "SHUGOCORE_MEMORY_BACKEND": "postgres"}):
            self.assertIsNone(_resolve_memory_source(None))

    def test_invalid_backend_name_ignored(self):
        with mock.patch.dict(
                os.environ,
                {"SHUGOCORE_MEMORY_DSN": "",
                 "SHUGOCORE_MEMORY_BACKEND": "sqlite"}):
            self.assertEqual(
                _resolve_memory_source("mem.db"),
                "mem.db")


if __name__ == "__main__":
    unittest.main()