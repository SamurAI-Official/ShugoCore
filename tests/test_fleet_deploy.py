"""Tests for fleet_deploy - the ADB rollout capability.

No device and no process is involved: ``FakeAdb`` implements the ``AdbRunner``
surface, records every argv vector and returns scripted output, so these tests
assert the *gating* (operator allowlist, artifact root, digest) rather than
adb itself. ``FakeAudit`` captures what the audit chain would receive.
"""
import tempfile
import unittest
from pathlib import Path

from fleet_deploy import (
    FleetDeployHandler,
    _parse_devices,
    register_fleet_handlers,
    sha256_file,
)

SERIAL_A = "adb-R52WC05JPMW-4kMS88._adb-tls-connect._tcp"
SERIAL_B = "adb-R58N92Q0XRK-BM3bmS._adb-tls-connect._tcp"


class FakeAdb:
    """Scripted AdbRunner double: records calls, replays canned replies."""

    def __init__(self, replies=None):
        self.calls = []
        self.replies = dict(replies or {})

    def run(self, args, timeout):
        self.calls.append(list(args))
        key = " ".join(str(a) for a in args)
        for prefix, reply in self.replies.items():
            if key.startswith(prefix):
                return reply
        return 0, "", ""

    def install_calls(self):
        return [call for call in self.calls if "install" in call]


class FakeAudit:
    def __init__(self):
        self.events = []

    def append(self, event_type, payload):
        self.events.append((event_type, dict(payload)))


def _write_apk(directory, name="app-debug.apk", content=b"fake-apk-bytes"):
    path = Path(directory) / name
    path.write_bytes(content)
    return path


class TestParseDevices(unittest.TestCase):
    def test_parses_adb_devices_l(self):
        out = ("List of devices attached\n"
               f"{SERIAL_A}          device product:gts9fesqw model:SM_X518U\n"
               f"{SERIAL_B}   offline\n"
               "\n")
        self.assertEqual(_parse_devices(out),
                         [(SERIAL_A, "device"), (SERIAL_B, "offline")])

    def test_header_and_blank_lines_only(self):
        self.assertEqual(_parse_devices("List of devices attached\n\n"), [])


class TestFleetDeployHandler(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.artifact_root = Path(self.tmp.name) / "apk"
        self.artifact_root.mkdir()
        self.apk = _write_apk(self.artifact_root)
        self.digest = sha256_file(self.apk)

    def tearDown(self):
        self.tmp.cleanup()

    def _handler(self, adb=None, targets=(SERIAL_A,), **kwargs):
        return FleetDeployHandler(adb=adb or FakeAdb(),
                                  allowed_targets=targets,
                                  artifact_root=str(self.artifact_root),
                                  **kwargs)

    # -- refusals: nothing may reach a device ---------------------------

    def test_deploy_refuses_without_allowlist(self):
        adb = FakeAdb()
        result = self._handler(adb=adb, targets=()).deploy(
            {"artifact": str(self.apk)})
        self.assertEqual(result["status"], "refused")
        self.assertIn("allowlist", result["reason"])
        self.assertEqual(adb.calls, [])

    def test_deploy_refuses_unknown_target(self):
        adb = FakeAdb()
        result = self._handler(adb=adb).deploy(
            {"artifact": str(self.apk), "targets": [SERIAL_B]})
        self.assertEqual(result["status"], "refused")
        self.assertIn("allowlist", result["reason"])
        self.assertEqual(adb.calls, [])

    def test_deploy_refuses_artifact_outside_root(self):
        outside = _write_apk(self.tmp.name, name="other.apk")
        adb = FakeAdb()
        result = self._handler(adb=adb).deploy({"artifact": str(outside)})
        self.assertEqual(result["status"], "refused")
        self.assertIn("artifact root", result["reason"])
        self.assertEqual(adb.calls, [])

    def test_deploy_refuses_non_apk_missing_and_empty(self):
        notes = Path(self.artifact_root) / "notes.txt"
        notes.write_text("x", encoding="utf-8")
        handler = self._handler()
        self.assertIn("is not .apk",
                      handler.deploy({"artifact": str(notes)})["reason"])
        self.assertIn("not a file", handler.deploy(
            {"artifact": str(Path(self.artifact_root) / "nope.apk")})["reason"])
        self.assertIn("required", handler.deploy({})["reason"])

    def test_deploy_refuses_digest_mismatch(self):
        adb = FakeAdb()
        result = self._handler(adb=adb).deploy(
            {"artifact": str(self.apk), "sha256": "00" * 32})
        self.assertEqual(result["status"], "refused")
        self.assertIn("does not match", result["reason"])
        self.assertEqual(adb.calls, [])

    def test_deploy_refuses_too_many_targets(self):
        result = self._handler(targets=(SERIAL_A, SERIAL_B),
                               max_targets=1).deploy({"artifact": str(self.apk)})
        self.assertEqual(result["status"], "refused")
        self.assertIn("rollout bound", result["reason"])

    def test_refusals_are_audited(self):
        audit = FakeAudit()
        self._handler(targets=(), audit=audit).deploy({"artifact": str(self.apk)})
        self.assertEqual([e[0] for e in audit.events], ["fleet_deploy_refused"])

    # -- execution ------------------------------------------------------

    def test_deploy_installs_on_allowlisted_targets(self):
        adb = FakeAdb({f"-s {SERIAL_A} install":
                       (0, "Performing Streamed Install\nSuccess\n", "")})
        audit = FakeAudit()
        result = self._handler(adb=adb, audit=audit).deploy(
            {"artifact": str(self.apk), "sha256": self.digest})
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["deployed"], 1)
        self.assertEqual(result["failed"], 0)
        self.assertEqual(result["sha256"], self.digest)
        self.assertEqual(adb.install_calls(),
                         [["-s", SERIAL_A, "install", "-r", str(self.apk)]])
        self.assertEqual([e[0] for e in audit.events],
                         ["fleet_deploy_target", "fleet_deploy"])

    def test_deploy_reports_device_failure(self):
        adb = FakeAdb({f"-s {SERIAL_A} install":
                       (1, "Failure "
                           "[INSTALL_FAILED_UPDATE_INCOMPATIBLE: signatures do "
                           "not match]\n", "")})
        result = self._handler(adb=adb).deploy({"artifact": str(self.apk)})
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["deployed"], 0)
        self.assertIn("INSTALL_FAILED_UPDATE_INCOMPATIBLE",
                      result["results"][0]["detail"])

    def test_deploy_is_partial_when_one_target_fails(self):
        adb = FakeAdb({f"-s {SERIAL_A} install": (0, "Success\n", ""),
                       f"-s {SERIAL_B} install": (1, "Failure [closed]\n", "")})
        result = self._handler(adb=adb, targets=(SERIAL_A, SERIAL_B)).deploy(
            {"artifact": str(self.apk)})
        self.assertEqual(result["status"], "partial")
        self.assertEqual((result["deployed"], result["failed"]), (1, 1))

    def test_dry_run_does_not_touch_devices(self):
        adb = FakeAdb()
        result = self._handler(adb=adb).deploy(
            {"artifact": str(self.apk), "dry_run": True})
        self.assertEqual(result["status"], "success")
        self.assertTrue(result["dry_run"])
        self.assertEqual(adb.install_calls(), [])

    def test_expect_version_mismatch_fails_the_target(self):
        adb = FakeAdb({
            f"-s {SERIAL_A} install": (0, "Success\n", ""),
            f"-s {SERIAL_A} shell dumpsys":
                (0, "    versionCode=22 minSdk=28\n    versionName=1.30.5\n", ""),
        })
        result = self._handler(adb=adb).deploy(
            {"artifact": str(self.apk), "expect_version": "9.9.9"})
        self.assertEqual(result["status"], "failed")
        self.assertIn("does not match expected",
                      result["results"][0]["detail"])

    # -- read-only + wiring ---------------------------------------------

    def test_status_lists_devices_and_marks_allowlist(self):
        adb = FakeAdb({
            "devices -l": (0, "List of devices attached\n"
                              f"{SERIAL_A} device product:x model:y\n"
                              f"{SERIAL_B} unauthorized\n", ""),
            f"-s {SERIAL_A} shell dumpsys":
                (0, "    versionName=1.30.5\n    versionCode=22\n", ""),
        })
        result = self._handler(adb=adb).handle({"action_type": "fleet_status"})
        self.assertEqual(result["status"], "success")
        by_serial = {d["serial"]: d for d in result["devices"]}
        self.assertTrue(by_serial[SERIAL_A]["allowlisted"])
        self.assertEqual(by_serial[SERIAL_A]["installed"]["versionName"], "1.30.5")
        self.assertEqual(by_serial[SERIAL_B]["state"], "unauthorized")
        self.assertFalse(by_serial[SERIAL_B]["allowlisted"])
        self.assertNotIn("installed", by_serial[SERIAL_B])

    def test_status_reports_adb_failure(self):
        result = self._handler(adb=FakeAdb({"devices -l": (1, "", "boom")})
                               ).handle({"action_type": "fleet_status"})
        self.assertEqual(result["status"], "failed")
        self.assertIn("boom", result["reason"])

    def test_unknown_fleet_action_is_refused(self):
        result = self._handler().handle({"action_type": "fleet_sabotage"})
        self.assertEqual(result["status"], "refused")

    def test_register_fleet_handlers_wires_layer_and_vocabulary(self):
        class FakeLayer:
            def __init__(self):
                self.handlers = {}

            def register_handler(self, action_type, handler):
                self.handlers[action_type] = handler

        class FakePolicy:
            KNOWN_ACTION_TYPES = set()

        layer, policy_mod = FakeLayer(), FakePolicy()
        handler = self._handler()
        register_fleet_handlers(layer, handler, policy_module=policy_mod)
        self.assertEqual(sorted(layer.handlers),
                         ["fleet_deploy", "fleet_status"])
        self.assertTrue(callable(layer.handlers["fleet_deploy"]))
        self.assertIn("fleet_deploy", policy_mod.KNOWN_ACTION_TYPES)
        self.assertIn("fleet_status", policy_mod.KNOWN_ACTION_TYPES)


class TestEngineVocabulary(unittest.TestCase):
    """The engine must KNOW the action and CONSENT-GATE it on a host."""

    def test_engine_knows_and_gates_fleet_deploy(self):
        import decision_engine
        self.assertTrue(decision_engine._HAS_FLEET)
        self.assertIn("fleet_deploy", decision_engine._KNOWN_ACTION_TYPES)
        self.assertIn("fleet_deploy",
                      decision_engine._CONSENT_GATED_ACTION_TYPES)
        self.assertIn("fleet_status", decision_engine._KNOWN_ACTION_TYPES)
