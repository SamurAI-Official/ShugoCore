#!/usr/bin/env python3
"""Track 3: Godot OpenXR scaffold tests (source-level, cross-language).

Validates the scaffold without a Godot binary: OpenXR enabled, autoloads
registered, INI plugin descriptors (regression lock for the XML fix),
honest presence states, and — cross-checked against the actual Python
server — the bridge's API paths and token convention.
"""
import os
import re
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GODOT = os.path.join(ROOT, "platforms", "godot")
sys.path.insert(0, ROOT)

from shugocore_server import ShugoCoreServer  # noqa: E402


def read(relpath: str) -> str:
    with open(os.path.join(GODOT, relpath)) as fh:
        return fh.read()


def read_repo(relpath: str) -> str:
    with open(os.path.join(ROOT, relpath)) as fh:
        return fh.read()


class TestProjectConfig(unittest.TestCase):
    def test_openxr_enabled(self):
        cfg = read("project.godot")
        self.assertIn("[xr]", cfg)
        self.assertIn("openxr/enabled=true", cfg)

    def test_autoloads_registered(self):
        cfg = read("project.godot")
        self.assertIn('ShugoCoreBridge="*res://scripts/'
                      'shugocore_agent_bridge.gd"', cfg)
        self.assertIn('XRBootstrap="*res://scripts/xr_bootstrap.gd"', cfg)

    def test_main_scene_set(self):
        cfg = read("project.godot")
        self.assertIn('run/main_scene="res://scenes/main.tscn"', cfg)


class TestPluginDescriptors(unittest.TestCase):
    """plugin.cfg is INI in Godot — the XML variant is a regression."""

    INI_PATHS = [
        "addons/shugocore_xr/plugin.cfg",
        "addons/nrr_godot/plugin.cfg",
        os.path.join(ROOT, "platforms", "android", "app", "src", "main",
                     "cpp", "nrr", "engine_plugins", "godot", "plugin.cfg"),
    ]

    def test_all_descriptors_are_ini(self):
        for rel in self.INI_PATHS:
            text = read(rel) if not os.path.isabs(rel) else open(rel).read()
            self.assertNotIn("<?xml", text, rel)
            self.assertIn("[plugin]", text, rel)

    def test_nrr_descriptor_matches_documented_api(self):
        cfg = read("addons/nrr_godot/plugin.cfg")
        self.assertIn('name="NRR"', cfg)
        self.assertIn('script="NRR.gd"', cfg)


class TestBridgeWiring(unittest.TestCase):
    """The bridge speaks the server's REAL routes — cross-checked."""

    def test_routes_match_server_route_table(self):
        bridge = read("scripts/shugocore_agent_bridge.gd")
        for method, path in (
                ("GET", "/health"),
                ("GET", "/api/v1/status"),
                ("GET", "/api/v1/sensors"),
                ("POST", "/api/generate"),
                ("POST", "/api/v1/task")):
            self.assertIn(f'"{path}"', bridge, path)
            self.assertIsNotNone(
                ShugoCoreServer.route_name(method, path), path)

    def test_token_env_matches_server_convention(self):
        bridge = read("scripts/shugocore_agent_bridge.gd")
        server = read_repo("shugocore_server.py")
        self.assertIn("SHUGOCORE_SERVER_TOKEN", bridge)
        self.assertIn("SHUGOCORE_SERVER_TOKEN", server)
        self.assertIn("Authorization: Bearer ", bridge)

    def test_bounded_polling(self):
        bridge = read("scripts/shugocore_agent_bridge.gd")
        self.assertIn("MAX_POLL_SECONDS", bridge)
        self.assertIn("MAX_TEXT_CHARS", bridge)
        # Godot 4 typed math: clampf (clamp is deprecated on floats).
        self.assertIn("clampf(", bridge)

    def test_never_bypasses_policy(self):
        bridge = read("scripts/shugocore_agent_bridge.gd")
        self.assertIn("/api/v1/task", bridge)
        self.assertIn("never bypasses policy", bridge)


class TestHonestPresence(unittest.TestCase):
    def test_state_set_is_honest(self):
        presence = read("scripts/shugo_presence.gd")
        for state in ("offline", "idle", "thinking", "speaking",
                      "listening"):
            self.assertIn(f'STATE_{state.upper()} := "{state}"', presence)

    def test_listening_is_opt_in_only(self):
        presence = read("scripts/shugo_presence.gd")
        self.assertIn("Opt-in hook for a future real mic source", presence)
        # Exactly one LISTENING call site, and it must live inside
        # set_listening — the default path never fakes listening.
        func_start = presence.index("func set_listening(")
        func_end = presence.index("func ", func_start + 1)
        self.assertIn("_apply_state(STATE_LISTENING)",
                      presence[func_start:func_end])
        self.assertNotIn("_apply_state(STATE_LISTENING)",
                         presence[:func_start])
        self.assertNotIn("_apply_state(STATE_LISTENING)",
                         presence[func_end:])

    def test_surfaces_track1_and_track2(self):
        presence = read("scripts/shugo_presence.gd")
        self.assertIn("mesh_role", presence)
        self.assertIn("security_baseline", presence)

    def test_states_driven_by_bridge_signals(self):
        presence = read("scripts/shugo_presence.gd")
        self.assertIn("connection_changed.connect", presence)
        self.assertIn("agent_status_changed.connect", presence)
        self.assertIn("agent_reply.connect", presence)


class TestNRRStubHonesty(unittest.TestCase):
    def test_stub_never_fakes_output(self):
        nrr = read("addons/nrr_godot/NRR.gd")
        self.assertIn("available: bool = false", nrr)
        self.assertIn("NO rendering", nrr)
        # Passthrough must be explicit, not silent.
        self.assertIn("MUST treat a returned image with", nrr)

    def test_documented_api_surface(self):
        nrr = read("addons/nrr_godot/NRR.gd")
        for fn in ("func initialize()", "func load_model(",
                   "func render_frame("):
            self.assertIn(fn, nrr)


class TestScenes(unittest.TestCase):
    def test_scene_resources_exist(self):
        for scene in ("scenes/main.tscn", "scenes/shugo_presence.tscn"):
            text = read(scene)
            for match in re.findall(r'path="res://([^"]+)"', text):
                self.assertTrue(os.path.exists(os.path.join(GODOT, match)),
                                f"{scene} -> {match}")

    def test_main_scene_wires_xr_and_presence(self):
        main = read("scenes/main.tscn")
        self.assertIn('type="XROrigin3D"', main)
        self.assertIn('type="XRCamera3D"', main)
        self.assertIn("scripts/xr_bootstrap.gd", main)
        self.assertIn("shugo_presence.tscn", main)

    def test_presence_scene_has_state_label(self):
        scene = read("scenes/shugo_presence.tscn")
        self.assertIn('name="StateLabel"', scene)
        self.assertIn('name="DetailLabel"', scene)


class TestReadme(unittest.TestCase):
    def test_integration_documented(self):
        readme = read("README.md")
        self.assertIn("SHUGOCORE_XR_AGENT_URL", readme)
        self.assertIn("desktop_preview", readme)
        self.assertIn("addons/shugocore_xr/", readme)


if __name__ == "__main__":
    unittest.main()
