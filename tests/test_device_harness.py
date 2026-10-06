"""The device harness, and the on-device facts it has to be able to see.

Everything here was found by running the adb smoke probe against real hardware
(a Tab S9 FE and an A16 on the same LAN), not by reading the code.

1. The probe hardcoded the macOS adb path, so on Windows it died with
   ``FileNotFoundError`` before it could say anything about a device.
2. A node that is *legitimately* deferring to a capable peer (the fleet working
   as designed) emits no decision lines -- so the probe waited five minutes and
   then reported a healthy node as broken. The verdict now reaches stderr, where
   adb can read it, and the probe reports the deferral instead of blaming it.
3. ``decision_engine.log`` had no rotation: measured 113 MB on the A16 and
   climbing, against a project principle of bounded state everywhere.
"""
import contextlib
import importlib.util
import io
import logging
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from shugocore_agent import create_agent  # noqa: E402

HARNESS = os.path.join(ROOT, "tests", "android_device_smoke.py")


def _load_harness():
    spec = importlib.util.spec_from_file_location("device_harness_under_test",
                                                  HARNESS)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class AdbIsFoundWhereverWeAreTestCase(unittest.TestCase):
    """The probe must not require one particular OS's SDK layout."""

    def test_a_non_macos_path_is_accepted(self):
        harness = _load_harness()
        with tempfile.TemporaryDirectory() as tmp:
            fake = os.path.join(tmp, "adb")
            with open(fake, "w", encoding="utf-8") as fh:
                fh.write("#!/bin/sh\n")
            old = os.environ.get("SHUGOCORE_ADB")
            os.environ["SHUGOCORE_ADB"] = fake
            try:
                self.assertEqual(harness._find_adb(), fake)
            finally:
                if old is None:
                    os.environ.pop("SHUGOCORE_ADB", None)
                else:
                    os.environ["SHUGOCORE_ADB"] = old

    def test_a_missing_override_is_ignored(self):
        """A stale SHUGOCORE_ADB must not shadow a working adb."""
        harness = _load_harness()
        old = os.environ.get("SHUGOCORE_ADB")
        os.environ["SHUGOCORE_ADB"] = os.path.join("nope", "adb")
        try:
            self.assertNotEqual(harness._find_adb(), os.path.join("nope", "adb"))
        finally:
            if old is None:
                os.environ.pop("SHUGOCORE_ADB", None)
            else:
                os.environ["SHUGOCORE_ADB"] = old


class DeferralIsVisibleTestCase(unittest.TestCase):
    """A subordinate node's verdict must reach the place a harness can read."""

    def setUp(self):
        self._origin = os.getcwd()
        self._tmp = tempfile.TemporaryDirectory(prefix="shugocore_harness_")
        self.agents = []

    def tearDown(self):
        for agent in self.agents:
            try:
                agent.cleanup()
            except Exception:
                pass
        try:
            os.chdir(self._origin)
        except OSError:
            pass
        try:
            self._tmp.cleanup()
        except (OSError, PermissionError):
            pass

    def _agent(self):
        agent = create_agent(device_caps="Exynos-1380",
                             api_url="http://127.0.0.1:11434",
                             data_dir=self._tmp.name)
        self.agents.append(agent)
        return agent

    def test_the_verdict_reaches_stderr(self):
        agent = self._agent()
        agent._orchestration_mode = lambda: ("subordinate",
                                             "peer shugo-mac holds the lease")
        captured = io.StringIO()
        with contextlib.redirect_stderr(captured):
            agent.tick()
        self.assertIn("ORCHESTRATION: subordinate:", captured.getvalue(),
                      "a harness watching logcat could not tell a deferring "
                      "node from a broken one")

    def test_the_verdict_is_announced_once_per_change(self):
        """Bounded: one line per change, not one per tick."""
        agent = self._agent()
        agent._orchestration_mode = lambda: ("subordinate", "peer holds the lease")
        captured = io.StringIO()
        with contextlib.redirect_stderr(captured):
            agent.tick()
            agent.tick()
            agent.tick()
        self.assertEqual(captured.getvalue().count("ORCHESTRATION:"), 1,
                         captured.getvalue())

    def test_a_self_governing_node_also_announces_itself(self):
        agent = self._agent()
        agent._orchestration_mode = lambda: ("standalone",
                                             "no live candidates to hand to")
        captured = io.StringIO()
        with contextlib.redirect_stderr(captured):
            agent.tick()
        self.assertIn("ORCHESTRATION: standalone:", captured.getvalue())


class HarnessReadsTheVerdictTestCase(unittest.TestCase):
    """The probe must parse the verdict, and know which modes mean "deferred"."""

    def test_a_deferral_is_parsed_from_a_logcat_line(self):
        harness = _load_harness()
        harness.log_lines_containing = lambda serial, needle: [
            "10-06 04:05:01.123 31081 31178 W python.stderr: "
            "ORCHESTRATION: subordinate: peer shugo-mac holds the lease"]
        mode, why = harness.orchestration_mode("serial")
        self.assertEqual(mode, "subordinate")
        self.assertIn("shugo-mac", why)

    def test_nothing_reported_means_unknown_not_primary(self):
        """Absence of evidence is not evidence of self-governance."""
        harness = _load_harness()
        harness.log_lines_containing = lambda serial, needle: []
        self.assertEqual(harness.orchestration_mode("serial"), ("", ""))

    def test_the_deferred_set_matches_what_the_agent_can_return(self):
        """Drift guard: the probe's notion of 'deferred' is the agent's."""
        harness = _load_harness()
        self.assertEqual(set(harness.DEFERRED_MODES),
                         {"subordinate", "degraded"})
        with open(os.path.join(ROOT, "shugocore_agent.py"),
                  encoding="utf-8") as fh:
            source = fh.read()
        for mode in harness.DEFERRED_MODES:
            self.assertIn(f'"{mode}"', source)
        # And the modes that mean "this node decides for itself".
        self.assertIn('return "primary"', source)
        self.assertIn('return "standalone"', source)


class TheDecisionLogIsBoundedTestCase(unittest.TestCase):
    """Rotation, not an unbounded file (measured 113 MB on a real A16)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="shugocore_log_")
        self.logger = logging.getLogger("logging_manager")
        self._saved = list(self.logger.handlers)
        for handler in list(self.logger.handlers):
            self.logger.removeHandler(handler)

    def tearDown(self):
        for handler in list(self.logger.handlers):
            self.logger.removeHandler(handler)
            try:
                handler.close()
            except Exception:
                pass
        for handler in self._saved:
            if handler not in self.logger.handlers:
                self.logger.addHandler(handler)
        try:
            self._tmp.cleanup()
        except (OSError, PermissionError):
            pass

    def test_the_file_handler_is_a_rotating_one(self):
        import logging_manager as lm
        old = (lm._LOG_MAX_BYTES, lm._LOG_BACKUPS)
        try:
            path = os.path.join(self._tmp.name, "decision_engine.log")
            manager = lm.LoggingManager(path)
            handler = manager.logger.handlers[0]
            self.assertEqual(type(handler).__name__, "RotatingFileHandler")
            self.assertGreater(handler.maxBytes, 0)
            self.assertGreater(handler.backupCount, 0)
        finally:
            lm._LOG_MAX_BYTES, lm._LOG_BACKUPS = old

    def test_the_file_actually_rotates_and_stays_bounded(self):
        import logging_manager as lm
        old = (lm._LOG_MAX_BYTES, lm._LOG_BACKUPS)
        lm._LOG_MAX_BYTES, lm._LOG_BACKUPS = 2048, 2
        try:
            path = os.path.join(self._tmp.name, "decision_engine.log")
            manager = lm.LoggingManager(path)
            for i in range(400):
                manager.logger.info("decision %d %s", i, "x" * 80)
            names = os.listdir(self._tmp.name)
            self.assertTrue([n for n in names
                             if n.startswith("decision_engine.log.")],
                            f"the log never rotated: {names}")
            biggest = max(os.path.getsize(os.path.join(self._tmp.name, n))
                          for n in names if n.startswith("decision_engine.log"))
            self.assertLess(biggest, 64 * 1024, f"unbounded: {biggest} B")
        finally:
            lm._LOG_MAX_BYTES, lm._LOG_BACKUPS = old


if __name__ == "__main__":
    unittest.main()
