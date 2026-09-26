"""The service must build exactly ONE Python agent per process.

Regression guard for a defect that review did not catch: ``onStartCommand`` is
START_STICKY and schedules ``initializeInference()`` on every ``startService``,
and that function had no "already bootstrapped" guard. Each call therefore built
a *second* ``AndroidAgent`` inside the same Chaquopy interpreter -- two tick
loops (duplicate tick counters in logcat), two SQLite connections (every tick
failed with "database is locked" on the Tab S9 FE) and a mesh runtime whose
second bind died with EADDRINUSE.

There is no Kotlin unit-test harness in this repo, so the invariant is asserted
against the source, like the other Android-side guards.
"""
import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVICE = os.path.join(ROOT, "platforms", "android", "app", "src", "main",
                       "java", "com", "samurai", "shugocore",
                       "ShugoCoreService.kt")


class TestAndroidAgentSingleton(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(SERVICE, encoding="utf-8") as handle:
            cls.source = handle.read()

    def test_lock_field_exists(self):
        self.assertIn("private val bootstrapLock = Any()", self.source)

    def test_guard_checks_for_an_existing_agent_under_the_lock(self):
        guard = re.search(
            r"private fun maybeInitializeInference\(\)\s*\{(?P<body>.*?)\n    \}",
            self.source, re.DOTALL)
        self.assertIsNotNone(guard, "maybeInitializeInference is missing")
        body = guard.group("body")
        self.assertIn("synchronized(bootstrapLock)", body)
        self.assertIn("pyAgent != null", body)
        self.assertIn("initializeInference()", body)

    def test_service_only_bootstraps_through_the_guard(self):
        scheduled = re.findall(r"executor\.execute \{ (\w+)\(\) \}", self.source)
        self.assertIn("maybeInitializeInference", scheduled)
        self.assertNotIn("initializeInference", scheduled,
                         "onStartCommand must not schedule a raw bootstrap")

    def test_initialize_inference_has_exactly_one_call_site(self):
        calls = [line.strip() for line in self.source.splitlines()
                 if line.strip() == "initializeInference()"]
        self.assertEqual(len(calls), 1,
                         "initializeInference() must have exactly one call site "
                         "(inside the guarded bootstrap)")


if __name__ == "__main__":
    unittest.main()
