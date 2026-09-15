"""ModelManager performance accounting — bounded by construction.

The soak surfaced that the multiplicative performance update (×1.1 per
success) was unbounded: a healthy CSFA loop drove the score (and with it
``aggregated_output``) toward float overflow within ~7300 cycles. These tests
pin the bound and the property that matters (relative ranking).
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from model_manager import MODEL_PERFORMANCE_CAP, ModelManager  # noqa: E402


def _manager(*model_ids: str) -> ModelManager:
    return ModelManager([{"id": mid, "type": "text"} for mid in model_ids])


class TestBoundedPerformance(unittest.TestCase):
    def test_successes_never_exceed_the_cap(self):
        mgr = _manager("m")
        for _ in range(10_000):
            mgr.update_model_performance("m", success=True)
        self.assertLessEqual(mgr.get_model_performance("m"),
                             MODEL_PERFORMANCE_CAP)
        self.assertEqual(mgr.get_model_performance("m"),
                         MODEL_PERFORMANCE_CAP)

    def test_failures_shrink_below_baseline(self):
        mgr = _manager("m")
        for _ in range(10):
            mgr.update_model_performance("m", success=False)
        self.assertLess(mgr.get_model_performance("m"), 1.0)
        self.assertGreater(mgr.get_model_performance("m"), 0.0)

    def test_relative_ranking_in_the_working_regime(self):
        # Both models stay below the cap here; more successes must still
        # rank higher (the property selection actually uses).
        mgr = _manager("a", "b")
        for _ in range(30):
            mgr.update_model_performance("a", success=True)
        for _ in range(15):
            mgr.update_model_performance("b", success=True)
        self.assertLess(mgr.get_model_performance("b"),
                        MODEL_PERFORMANCE_CAP)
        self.assertGreater(mgr.get_model_performance("a"),
                           mgr.get_model_performance("b"))

    def test_saturated_models_tie_rather_than_explode(self):
        # Past the cap both sit at MODEL_PERFORMANCE_CAP: bounded, and no
        # false ordering — the honest steady-state behaviour.
        mgr = _manager("a", "b")
        for _ in range(500):
            mgr.update_model_performance("a", success=True)
            mgr.update_model_performance("b", success=True)
        self.assertEqual(mgr.get_model_performance("a"),
                         mgr.get_model_performance("b"))
        self.assertEqual(mgr.get_model_performance("a"),
                         MODEL_PERFORMANCE_CAP)

    def test_set_model_performance_is_clamped(self):
        mgr = _manager("m")
        mgr.set_model_performance("m", 1e9)
        self.assertEqual(mgr.get_model_performance("m"),
                         MODEL_PERFORMANCE_CAP)
        mgr.set_model_performance("m", -5.0)
        self.assertEqual(mgr.get_model_performance("m"), 0.0)

    def test_unknown_model_defaults_and_ignores_updates(self):
        mgr = _manager("m")
        self.assertEqual(mgr.get_model_performance("ghost"), 1.0)
        mgr.update_model_performance("ghost", success=True)  # must not raise
        self.assertEqual(mgr.get_model_performance("ghost"), 1.0)


if __name__ == "__main__":
    unittest.main()
