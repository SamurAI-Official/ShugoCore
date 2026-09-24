#!/usr/bin/env python3
"""Track 3 Quest build / operator-console tests (source-level + routes).

Phase 4/5 locks for the Quest 3 operator-console work:

  1. FUNCTIONAL CONTRACT: ShugoCoreConfig's resolution rules (file-first
     layer order, poll clamp bounds, provenance) as observable strings in
     the GDScript — no logic duplicated under a second language.
  2. ROUTE cross-check: every route the bridge greets resolves in
     ShugoCoreServer.route_name (fleet + approvals are Phase 3 additions).
  3. SCAFFOLD integrity: package identity, renderer, preset keys, scene
     references, no orphan handlers, honesty regressions carried over.
"""
import json
import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GODOT = os.path.join(ROOT, "platforms", "godot")
import sys
sys.path.insert(0, ROOT)

from shugocore_server import ShugoCoreServer  # noqa: E402


def read(relpath: str) -> str:
    with open(os.path.join(GODOT, relpath)) as fh:
        return fh.read()


CONFIG_DEFAULT = json.loads(read("shugocore_xr.default.json"))


class TestAndroidSafeConfig(unittest.TestCase):
    """Env vars are dead on Android — config must be file-first."""

    def test_config_module_exists_with_layer_order(self):
        cfg = read("scripts/shugocore_config.gd")
        self.assertIn("class_name ShugoCoreConfig", cfg)
        user_i = cfg.index("user://shugocore_xr.json")
        default_i = cfg.index("res://shugocore_xr.default.json")
        # The doc header explains WHY (env is dead on Android); the first
        # functional use of env vars comes after the file layers.
        first_load = cfg.index("load_config_read_json(USER_PATH)")
        env_i = cfg.index('OS.get_environment("SHUGOCORE_XR_AGENT_URL")')
        self.assertLess(user_i, default_i)
        self.assertLess(default_i, first_load)
        self.assertLess(first_load, env_i)
        for env in ("SHUGOCORE_XR_AGENT_URL", "SHUGOCORE_SERVER_TOKEN",
                    "SHUGOCORE_XR_POLL_SECONDS"):
            self.assertIn(env, cfg)

    def test_config_clamps_poll_bounds(self):
        cfg = read("scripts/shugocore_config.gd")
        self.assertIn("MIN_POLL_SECONDS", cfg)
        self.assertIn("MAX_POLL_SECONDS", cfg)

    def test_config_reports_provenance(self):
        cfg = read("scripts/shugocore_config.gd")
        for token in ('"user://"', '"res://"', '"defaults"', "source"):
            self.assertIn(token, cfg)

    def test_config_save_targets_user_path(self):
        cfg = read("scripts/shugocore_config.gd")
        self.assertIn("func save()", cfg)
        self.assertIn("FileAccess.WRITE", cfg)

    def test_shipped_default_points_at_lan_not_localhost(self):
        url = str(CONFIG_DEFAULT.get("agent_url", ""))
        self.assertTrue(url.startswith("http://"))
        self.assertNotIn("127.0.0.1", url)
        self.assertNotIn("localhost", url)
        self.assertIn("poll_seconds", CONFIG_DEFAULT)

    def test_bridge_is_config_driven(self):
        bridge = read("scripts/shugocore_agent_bridge.gd")
        self.assertIn("ShugoCoreConfig.load_config()", bridge)
        self.assertIn("reload_config", bridge)
        self.assertIn("config_changed", bridge)
        self.assertIn("config_source", bridge)


class TestBridgeResponseWiring(unittest.TestCase):
    """Lock for the disconnected-handler bug: request nodes wired, demux
    by route tag."""

    def test_every_request_node_is_wired(self):
        bridge = read("scripts/shugocore_agent_bridge.gd")
        for node in ("http_status", "http_sensors", "http_generate",
                     "http_task", "http_fleet", "http_approvals",
                     "http_resolve"):
            self.assertIn(f'_make_http("{node}"', bridge)
        self.assertEqual(
            len(re.findall(r"request_completed\.connect\(", bridge)), 1)
        self.assertIn("_on_http_completed.bind(tag)", bridge)

    def test_demux_covers_all_endpoints(self):
        bridge = read("scripts/shugocore_agent_bridge.gd")
        for tag in ('"status"', '"sensors"', '"fleet"', '"approvals"',
                    '"generate"', '"resolve"'):
            self.assertIn(f"{tag}:", bridge)

    def test_no_orphan_handlers(self):
        bridge = read("scripts/shugocore_agent_bridge.gd")
        self.assertNotIn("_on_request_completed", bridge)
        self.assertNotIn("_on_generate_completed", bridge)

    def test_failure_reports_route_tag(self):
        bridge = read("scripts/shugocore_agent_bridge.gd")
        self.assertIn("request_failed.emit(tag, code)", bridge)


class TestRouteTableExtended(unittest.TestCase):
    """Phase 3 routes cross-checked against the real Python table."""

    EXPECTED = [
        ("GET", "/health"),
        ("GET", "/api/v1/status"),
        ("GET", "/api/v1/sensors"),
        ("POST", "/api/generate"),
        ("POST", "/api/v1/task"),
        ("GET", "/api/v1/fleet"),
        ("GET", "/api/v1/approvals"),
    ]

    def test_all_routes_resolve_server_side(self):
        for method, path in self.EXPECTED:
            self.assertIsNotNone(
                ShugoCoreServer.route_name(method, path), path)

    def test_all_routes_greeted_in_bridge(self):
        bridge = read("scripts/shugocore_agent_bridge.gd")
        for _, path in self.EXPECTED:
            self.assertIn(f'"{path}"', bridge, path)

    def test_approval_resolve_path_builder(self):
        bridge = read("scripts/shugocore_agent_bridge.gd")
        self.assertIn('"/api/v1/approvals/"', bridge)
        self.assertIsNotNone(ShugoCoreServer.route_name(
            "POST", "/api/v1/approvals/abc123/approve"))
        self.assertIsNotNone(ShugoCoreServer.route_name(
            "POST", "/api/v1/approvals/abc123/deny"))


class TestOperatorConsole(unittest.TestCase):
    def test_console_scene_references_console_script(self):
        self.assertIn("scripts/console/operator_console.gd",
                      read("scenes/operator_console.tscn"))

    def test_five_tabs_present(self):
        console = read("scripts/console/operator_console.gd")
        for tab in ('"Node"', '"Fleet"', '"Approvals"', '"Task"',
                    '"Settings"'):
            self.assertIn(tab, console)

    def test_absent_surfaces_render_explicitly(self):
        console = read("scripts/console/operator_console.gd")
        for msg in ("fleet registry unavailable on this engine",
                    "registry present, no paired nodes",
                    "no approval broker on this engine",
                    "broker present, queue empty"):
            self.assertIn(msg, console)

    def test_resolve_goes_through_bridge(self):
        console = read("scripts/console/operator_console.gd")
        self.assertIn("_bridge.resolve_approval(", console)

    def test_task_dispatch_is_policy_gated_by_construction(self):
        console = read("scripts/console/operator_console.gd")
        self.assertIn("_bridge.send_task(", console)
        self.assertIn("Never bypasses policy", console)

    def test_settings_persists_to_user_path(self):
        console = read("scripts/console/operator_console.gd")
        self.assertIn("user://shugocore_xr.json", console)
        # Persistence is delegated to ShugoCoreConfig.save() — the module
        # that owns USER_PATH — rather than hand-rolled file I/O here.
        self.assertIn("ShugoCoreConfig.new()", console)
        self.assertIn("cfg.save()", console)
        self.assertIn("_on_settings_save", console)
        self.assertIn("_on_settings_test", console)

    def test_spatial_mount_uses_subviewport_quad(self):
        panel = read("scripts/console/console_panel.gd")
        self.assertIn("SubViewport.new()", panel)
        self.assertIn("QuadMesh.new()", panel)
        self.assertIn("operator_console.tscn", panel)
        self.assertIn("push_pointer", panel)
        self.assertNotIn("OpenXR", panel)  # core nodes only

    def test_approval_id_is_path_safe(self):
        bridge = read("scripts/shugocore_agent_bridge.gd")
        self.assertIn("_is_safe_approval_id", bridge)
        for bad in ('"/"', '"?"', '"#"'):
            self.assertIn(bad, bridge)

    def test_payloads_are_bounded(self):
        bridge = read("scripts/shugocore_agent_bridge.gd")
        for cap in ("MAX_FLEET_NODES := 64", "MAX_APPROVALS := 32",
                    "MAX_MANIFEST_KEYS := 12"):
            self.assertIn(cap, bridge)


class TestQuestProjectConfig(unittest.TestCase):
    def test_gl_compatibility_matches_proven_env(self):
        cfg = read("project.godot")
        self.assertIn('renderer/rendering_method="gl_compatibility"', cfg)
        self.assertIn(
            'renderer/rendering_method.mobile="gl_compatibility"', cfg)
        self.assertIn("GL Compatibility", cfg)
        self.assertIn("import_etc2_astc=true", cfg)
        self.assertIn('"4.7"', cfg)
        self.assertNotIn("Forward Plus", cfg)

    def test_preset_matches_proven_quest_reference(self):
        preset = read("export_presets.cfg")
        self.assertIn('name="Quest3"', preset)
        self.assertIn("gradle_build/use_gradle_build=true", preset)
        self.assertIn("xr_features/xr_mode=1", preset)
        self.assertIn("architectures/arm64-v8a=true", preset)
        self.assertIn('package/unique_name="com.samurai.shugocore.xr"',
                      preset)
        self.assertIn("permissions/internet=true", preset)
        self.assertIn("screen/immersive_mode=true", preset)
        self.assertIn("../build/ShugoCoreXR.apk", preset)

    def test_preset_generator_is_deterministic(self):
        gen = read("scripts/make_quest_preset.py")
        self.assertIn("body-tracking-xr-sample", gen)
        self.assertIn("preset sanity OK", gen)

    def test_main_scene_environment_is_valid(self):
        # Regression: the dangling ExtResource("3_env") is gone.
        main = read("scenes/main.tscn")
        self.assertNotIn('ExtResource("3_env")', main)
        self.assertIn('SubResource("Environment_main")', main)

    def test_presence_reuses_scene_labels(self):
        presence = read("scripts/shugo_presence.gd")
        self.assertNotIn("func _build_visuals", presence)
        self.assertIn('get_node_or_null("StateLabel")', presence)
        self.assertIn('get_node_or_null("DetailLabel")', presence)

    def test_build_script_pins_toolchain(self):
        build = read("scripts/build_quest.sh")
        self.assertIn("--install-android-build-template", build)
        self.assertIn('--export-debug "Quest3"', build)
        self.assertIn("openjdk@17", build)
        self.assertIn("ANDROID_HOME", build)

    def test_integration_script_is_idempotent_and_safe(self):
        integ = read("scripts/integrate_sample.py")
        self.assertIn("symlink_to", integ)
        self.assertIn("editor holds host", integ)
        self.assertIn("refusing project.godot edit", integ)
        self.assertIn("dry run", integ)


if __name__ == "__main__":
    unittest.main()
