"""
Platform-isolation guarantee
============================

The hardware-agnostic core must keep working when the Android platform
modules are unavailable. The strict proof: import every core module with
platform imports hard-blocked by a meta-path hook - ``decision_engine``'s
optional-handler pattern must degrade gracefully (_HAS_MOBILE False), and
nothing else may even try.
"""

import json
import os
import subprocess
import sys
import unittest

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_BLOCK_HOOK = r'''
import json
import sys
BLOCKED = {"android_bridge", "android_runtime", "android_node", "mobile_nodes"}

class _BlockPlatform:
    """Meta-path finder that makes platform module imports fail hard."""
    def find_spec(self, name, path=None, target=None):
        if name.split(".")[0] in BLOCKED:
            raise ImportError(f"platform module blocked: {name}")
        return None

sys.meta_path.insert(0, _BlockPlatform())
for name in list(sys.modules):
    if name.split(".")[0] in BLOCKED:
        del sys.modules[name]

import decision_engine
import memory_system
import state_machine
import fallbacks
import policy
import security
import telemetry
import audit
import ros2_interface

assert decision_engine._HAS_MOBILE is False, "mobile handler must be disabled"
assert decision_engine._KNOWN_ACTION_TYPES, "known action types must survive"
print(json.dumps({
    "has_mobile": decision_engine._HAS_MOBILE,
    "known_actions": sorted(decision_engine._KNOWN_ACTION_TYPES),
}))
'''


class TestPlatformIsolation(unittest.TestCase):
    def test_core_imports_with_platform_modules_blocked(self):
        """The hard guarantee: with android/mobile imports raising ImportError,
        the entire core still imports and the mobile handler degrades off."""
        result = subprocess.run(
            [sys.executable, "-c", _BLOCK_HOOK],
            cwd=_REPO, capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0,
                         f"core broke without platform modules:\n{result.stderr}")
        payload = json.loads(result.stdout.strip().splitlines()[-1])
        self.assertFalse(payload["has_mobile"])
        self.assertIn("robot_navigate", payload["known_actions"])
        self.assertNotIn("mobile_request_compute", payload["known_actions"])

    def test_acceleration_imports_cleanly_everywhere(self):
        import acceleration  # noqa: F401 - must never raise off-device

    def test_platform_modules_import_cleanly_without_android(self):
        # Bridge/runtime/node must be importable on any platform: all
        # Android access is lazy via duck-typed bridge objects.
        import android_bridge  # noqa: F401
        import android_runtime  # noqa: F401
        import mobile_nodes  # noqa: F401
        import android_node  # noqa: F401

    def test_core_sources_have_no_unconditional_platform_imports(self):
        # Belt-and-braces static check: any platform import in core source
        # must be inside a try block (guarded, like robotics_handler).
        for module in ("decision_engine",):
            path = os.path.join(_REPO, f"{module}.py")
            with open(path, "r", encoding="utf-8") as handle:
                lines = handle.read().splitlines()
            for index, line in enumerate(lines):
                stripped = line.strip()
                if stripped.startswith(("import android", "from android",
                                        "import mobile_nodes",
                                        "from mobile_nodes")):
                    # Walk back to see whether it is inside a try: block.
                    context = "\n".join(lines[max(0, index - 6):index + 1])
                    self.assertIn("try:", context,
                                  f"unguarded platform import in {module}:"
                                  f"{index + 1}")


if __name__ == "__main__":
    unittest.main()
