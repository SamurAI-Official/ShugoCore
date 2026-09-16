"""Tests for BackendPool (v1.30.4): health-based routing across backends."""
import sys
import unittest

sys.path.insert(0, "")

from model_backends import BackendError, BackendPool, BaseBackend, StubBackend


class _FlakyBackend(BaseBackend):
    """Deterministic backend that fails the first N calls then succeeds."""
    name = "flaky"

    def __init__(self, fail_first=0):
        self.calls = 0
        self.fail_first = fail_first

    def generate(self, model_id="", prompt="", timeout=30.0, grammar=None):
        self.calls += 1
        if self.calls <= self.fail_first:
            raise BackendError("simulated failure")
        return "ok"

    def list_models(self):
        return []

    def get_health(self):
        return True


class _AlwaysFailsBackend(BaseBackend):
    name = "always-fails"

    def generate(self, model_id="", prompt="", timeout=30.0, grammar=None):
        raise BackendError("always fails")

    def list_models(self):
        return []


class _CountingSuccessBackend(BaseBackend):
    name = "counting"

    def __init__(self):
        self.calls = 0

    def generate(self, model_id="", prompt="", timeout=30.0, grammar=None):
        self.calls += 1
        return "ok"

    def list_models(self):
        return []


class BackendPoolTestCase(unittest.TestCase):
    def test_requires_at_least_one_backend(self):
        with self.assertRaises(ValueError):
            BackendPool([])

    def test_requires_backend_key(self):
        with self.assertRaises(ValueError):
            BackendPool([{"priority": 1.0}])

    def test_rejects_invalid_priority(self):
        for bad in (0.0, -1.0, float("inf"), float("nan")):
            with self.assertRaises(ValueError):
                BackendPool([{"backend": StubBackend(), "priority": bad}])

    def test_routes_to_single_backend(self):
        b = StubBackend()
        pool = BackendPool([{"backend": b, "priority": 1.0}])
        out = pool.generate("m", "hi")
        self.assertIn("hi", out)
        stats = pool.stats()
        self.assertEqual(stats[0]["successes"], 1)

    def test_picks_healthiest_after_failure(self):
        """If preferred backend fails, pool falls back to next one."""
        flaky = _FlakyBackend(fail_first=1)
        ok = StubBackend()
        pool = BackendPool([{"backend": flaky, "priority": 1.0},
                            {"backend": ok, "priority": 1.0}])
        out = pool.generate("m", "hi")
        self.assertTrue(out)
        stats = {s["name"]: s for s in pool.stats()}
        self.assertGreaterEqual(stats["flaky"]["failures"], 1)
        self.assertEqual(stats["stub"]["successes"], 1)

    def test_raises_after_all_backends_fail(self):
        a = _AlwaysFailsBackend()
        b = _AlwaysFailsBackend()
        pool = BackendPool([{"backend": a, "priority": 1.0},
                            {"backend": b, "priority": 1.0}])
        with self.assertRaises(BackendError):
            pool.generate("m", "hi")

    def test_circuit_breaker_skips_flaky_once_open(self):
        """After max_failures, the failing backend is skipped entirely and
        the healthy one handles all subsequent calls."""
        bad = _AlwaysFailsBackend()
        good = _CountingSuccessBackend()
        pool = BackendPool([{"backend": bad, "priority": 1.0},
                            {"backend": good, "priority": 1.0}],
                           max_failures=1, cooldown_s=999)
        # Call 1: bad fails (>= 1 failure => circuit opens immediately),
        # good handles it -> "ok".
        self.assertEqual(pool.generate("m", "hi"), "ok")
        # Call 2: bad is circuit-open (skipped), good again.
        self.assertEqual(pool.generate("m", "hi"), "ok")
        stats = {s["name"]: s for s in pool.stats()}
        self.assertTrue(stats["always-fails"]["circuit_open"])
        self.assertGreaterEqual(stats["always-fails"]["failures"], 1)
        self.assertEqual(stats["counting"]["successes"], 2)

    def test_deterministic_tiebreak_by_index(self):
        a = _CountingSuccessBackend()
        b = _CountingSuccessBackend()
        a.name = "first"
        b.name = "second"
        pool = BackendPool([{"backend": a}, {"backend": b}])
        for _ in range(3):
            pool.generate("m", "hi")
        self.assertEqual(a.calls, 3)
        self.assertEqual(b.calls, 0)

    def test_priority_influences_selection(self):
        """Higher priority means 'prefer this one' when both healthy."""
        a = _CountingSuccessBackend()
        b = _CountingSuccessBackend()
        a.name = "primary"
        b.name = "secondary"
        pool = BackendPool([{"backend": a, "priority": 2.0},
                            {"backend": b, "priority": 1.0}])
        pool.generate("m", "hi")
        self.assertEqual(a.calls, 1)
        self.assertEqual(b.calls, 0)

    def test_list_models_union(self):
        a = StubBackend()
        a.list_models = lambda: ["alpha"]
        b = StubBackend()
        b.list_models = lambda: ["beta", "alpha"]
        pool = BackendPool([{"backend": a}, {"backend": b}])
        models = pool.list_models()
        self.assertEqual(sorted(models), ["alpha", "beta"])

    def test_get_health_true_when_any_healthy(self):
        bad = _AlwaysFailsBackend()
        good = StubBackend()
        pool = BackendPool([{"backend": bad}, {"backend": good}])
        self.assertTrue(pool.get_health())


    def test_create_backend_supports_pool_type(self):
        from model_backends import create_backend
        pool = create_backend({
            "type": "pool",
            "backends": [{"type": "stub"}, {"type": "stub", "priority": 0.5}],
        })
        self.assertIsInstance(pool, BackendPool)
        self.assertEqual(len(pool), 2)

    def test_create_backend_pool_requires_list(self):
        from model_backends import create_backend
        with self.assertRaises(ValueError):
            create_backend({"type": "pool", "backends": "not-a-list"})


if __name__ == "__main__":
    unittest.main()