#!/usr/bin/env python3
"""py_compat: the standard-library shims behind the Python 3.9-3.13 range.

``@dataclass(slots=True)`` is Python 3.10+, and on 3.9 it raises at *class
definition* time -- which made ``personality`` and ``kv_mesh`` unimportable and
took out the 3.9 CI leg (189 cascading TypeErrors, 120 errors). These tests pin
both halves of the contract:

* the shim produces a fully working dataclass on every supported interpreter;
* on 3.10+ it does NOT quietly give up the ``__slots__`` optimisation.

The second class is the regression test for the actual break: the production
classes that used the 3.10-only keyword must stay importable and keep their
slots wiring.
"""
import os
import sys
import unittest
from dataclasses import FrozenInstanceError, asdict, is_dataclass

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from py_compat import HAS_DATACLASS_SLOTS, dataclass_slots  # noqa: E402


@dataclass_slots
class _Point:
    x: int
    y: int = 0


@dataclass_slots(frozen=True)
class _Frozen:
    name: str


class TestDataclassSlotsShim(unittest.TestCase):
    def test_flag_matches_interpreter(self):
        self.assertEqual(HAS_DATACLASS_SLOTS, sys.version_info >= (3, 10))

    def test_bare_usage_yields_a_working_dataclass(self):
        self.assertTrue(is_dataclass(_Point))
        self.assertEqual(_Point(1, 2).x, 1)
        self.assertEqual(_Point(1), _Point(1, 0), "defaults + equality")
        self.assertEqual(asdict(_Point(3, 4)), {"x": 3, "y": 4})

    def test_parameterised_usage_is_preserved(self):
        self.assertTrue(is_dataclass(_Frozen))
        with self.assertRaises(FrozenInstanceError):
            _Frozen("a").name = "b"

    def test_slots_are_kept_where_the_interpreter_supports_them(self):
        if HAS_DATACLASS_SLOTS:
            self.assertIn("__slots__", vars(_Point),
                          "the shim must not silently drop the slots win")
            with self.assertRaises(AttributeError):
                _Point(1, 2).__dict__
        else:  # pragma: no cover - exercised only by the 3.9 leg of the matrix
            self.assertNotIn("__slots__", vars(_Point))
            self.assertEqual(_Point(1, 2).__dict__, {"x": 1, "y": 2})


class TestShimmedProductionClasses(unittest.TestCase):
    """Regression guard for the modules that broke the 3.9 matrix leg."""

    def test_kv_mesh_shard_types_import_and_construct(self):
        from kv_mesh.shard import ContextShard, KVShard, ShardSpec

        for cls in (ShardSpec, KVShard, ContextShard):
            with self.subTest(cls=cls.__name__):
                self.assertTrue(is_dataclass(cls))
                if HAS_DATACLASS_SLOTS:
                    self.assertIn("__slots__", vars(cls))

        spec = ShardSpec("m", 2, 4, 8, 16)
        # 2 (K+V) * num_heads * head_dim * dtype_bytes
        self.assertEqual(spec.kv_bytes_per_token_per_layer(), 2 * 4 * 8 * 2)
        self.assertEqual(spec.kv_total_bytes(), 2 * 16 * 128)
        shard = KVShard("s", "m", 0, 2, 0, 4, 0, 16, 1024)
        self.assertEqual(shard.to_dict()["shard_id"], "s")

    def test_personality_verdict_imports_and_constructs(self):
        from personality.governor import PersonalityVerdict

        self.assertTrue(is_dataclass(PersonalityVerdict))
        if HAS_DATACLASS_SLOTS:
            self.assertIn("__slots__", vars(PersonalityVerdict))

        verdict = PersonalityVerdict()
        self.assertEqual(verdict.verdict, "pass")
        self.assertEqual(verdict.adjustments, {}, "default_factory survives")
        # The mutable default must be per-instance, not shared between them.
        verdict.adjustments["tone"] = "warm"
        self.assertEqual(PersonalityVerdict().adjustments, {})
        self.assertIn("verdict", verdict.to_dict())


if __name__ == "__main__":
    unittest.main()
