#!/usr/bin/env python3
"""Track 1 UI plumbing regression: the election role must reach the
node dashboard. Source-level check (same pattern as test_nrr_android_port)
so the wiring is guarded without needing a device or the Android SDK.
"""
import os
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JAVA = os.path.join(ROOT, "platforms", "android", "app", "src", "main",
                    "java", "com", "samurai", "shugocore")


def read(relpath: str) -> str:
    with open(os.path.join(JAVA, relpath)) as fh:
        return fh.read()


class MeshRoleUiWiring(unittest.TestCase):
    def test_header_binds_mesh_role(self):
        header = read(os.path.join("ui", "NodeStatusHeader.kt"))
        self.assertIn('addRow("Mesh")', header)
        self.assertIn('Ui.str(agent, "mesh_role", "")', header)
        # Honest states: a lone node must not display as PRIMARY.
        for state in ('"primary" -> "PRIMARY"',
                      '"follower" -> "FOLLOWER"',
                      '"standalone" -> "STANDALONE"'):
            self.assertIn(state, header)

    def test_agent_status_flattens_python_status(self):
        """getAgentStatus() mirrors the whole get_status_json output, so
        mesh_role/mesh_primary need no per-key Kotlin plumbing."""
        svc = read("ShugoCoreService.kt")
        self.assertIn('callAttr("get_status_json")', svc)
        self.assertIn("for (key in obj.keys())", svc)


if __name__ == "__main__":
    unittest.main()
