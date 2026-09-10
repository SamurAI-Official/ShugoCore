"""Root ↔ Android-bundled tree identity (structural drift guard).

The Android app packages a copy of the canonical Python runtime under
platforms/android/app/src/main/python/. That copy is DERIVED: every shared
module must stay byte-identical to the repo-root original. This test fails
the suite the moment one copy is edited without the other — the exact bug
class that once shipped a stale DecisionEngine to the device.
"""
import os
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BUNDLED = os.path.join(ROOT, "platforms", "android", "app", "src", "main", "python")


class TestAndroidTreeSync(unittest.TestCase):
    def test_every_bundled_module_matches_root(self):
        self.assertTrue(os.path.isdir(BUNDLED), "bundled python tree missing")
        drifted, bundled_count = [], 0
        for name in sorted(os.listdir(BUNDLED)):
            if not name.endswith(".py"):
                continue
            bundled_count += 1
            root_path = os.path.join(ROOT, name)
            bundled_path = os.path.join(BUNDLED, name)
            if not os.path.isfile(root_path):
                # Android-only adapters (android_*) are legitimately
                # bundled-only; anything else is a tree inconsistency.
                if name.startswith("android_"):
                    continue
                drifted.append(f"{name}: no root counterpart")
                continue
            with open(root_path, "rb") as fh:
                root_bytes = fh.read()
            with open(bundled_path, "rb") as fh:
                bundled_bytes = fh.read()
            if root_bytes != bundled_bytes:
                drifted.append(name)
        self.assertGreater(bundled_count, 20, "bundled tree looks truncated")
        self.assertEqual(
            drifted, [],
            "root ↔ bundled Python drift detected — re-sync with:\n  "
            + "\n  ".join(f"cp {name} platforms/android/app/src/main/python/{name}"
                          for name in drifted))

    def test_canonical_modules_present(self):
        for name in ("shugocore_agent.py", "decision_engine.py",
                     "subconscious.py", "android_inference.py",
                     "telemetry.py", "policy.py", "human_interaction.py",
                     "security.py", "personality/governor.py"):
            self.assertTrue(
                os.path.isfile(os.path.join(BUNDLED, name)),
                f"{name} missing from the bundled tree")


if __name__ == "__main__":
    unittest.main()
