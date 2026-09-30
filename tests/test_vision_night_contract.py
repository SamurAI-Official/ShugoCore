"""The night-vision change's load-bearing properties, checked where they can break.

Everything here exists because the frame the camera analyses is the very frame NRR
renders. A contrast stretch that modified it in place would change what NRR
produces; "faces=0" in an unlit room would still read as "nobody there" unless the
dark is said out loud; and the presence line is parsed by the correlation tool, so
its legacy tokens have to keep reading exactly as they did.

The presence tests import `runtime/fleet_correlation.py` and use its *own* regex and
its *own* presence_agreement(), so the logger and the parser cannot drift apart
without a failure here.
"""
import importlib.util
import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
KOTLIN = os.path.join(ROOT, "platforms", "android", "app", "src", "main", "java",
                      "com", "samurai", "shugocore")
VISION_PROVIDER = os.path.join(KOTLIN, "runtime", "VisionProvider.kt")
KOTLIN_SERVICE = os.path.join(KOTLIN, "ShugoCoreService.kt")
MAIN_ACTIVITY = os.path.join(KOTLIN, "MainActivity.kt")
SENSORS_PANE = os.path.join(KOTLIN, "ui", "SensorsPane.kt")
CONTROL_HOST = os.path.join(KOTLIN, "runtime", "NodeState.kt")
CORRELATION = os.path.join(ROOT, "runtime", "fleet_correlation.py")


def _read(path):
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def _correlation_module():
    spec = importlib.util.spec_from_file_location("fleet_correlation_under_test",
                                                  CORRELATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class StretchStaysDetectionSideTestCase(unittest.TestCase):
    """Night vision is detection-side, or NRR's input changes with it."""

    def test_the_stretch_runs_after_the_frame_is_published(self):
        provider = _read(VISION_PROVIDER)
        published = provider.index("PerceptionState.stampFrameRgba(")
        stretched = provider.index("stretchAndDetect(bitmap)")
        self.assertLess(
            published, stretched,
            "the contrast stretch runs before the frame is published: NRR would "
            "render a stretched frame that nothing asked it for")

    def test_the_stretch_only_ever_writes_to_a_copy(self):
        provider = _read(VISION_PROVIDER)
        targets = sorted(set(re.findall(r"(\w+)\.setPixels\(", provider)))
        self.assertEqual(
            targets, ["copy"],
            "the stretch writes through %s: the published frame and the raw "
            "detection pass would both change with it" % targets)
        self.assertIn("source.copy(Bitmap.Config.RGB_565, true)", provider)

    def test_the_default_dark_threshold_is_unchanged(self):
        provider = _read(VISION_PROVIDER)
        # A calibration run moves the threshold through applyDarkLumaMax on the
        # instance; editing this constant would change every device at once.
        self.assertRegex(provider, r"DARK_LUMA_MAX\s*=\s*12")
        self.assertRegex(provider, r"val tooDark = luma in 0\.\.darkLumaMax")
        self.assertRegex(provider, r'if \(tooDark\) " unavailable=too_dark"')


@unittest.skipUnless(os.path.isfile(CORRELATION),
                     "the host-side correlation tool is a local diagnostic "
                     "(/runtime/ is ignored), so this checkout may not have it")
class PresenceLineContractTestCase(unittest.TestCase):
    """The line the correlation tool parses, in its old and its new form."""

    def test_the_legacy_tokens_come_first_and_contiguous(self):
        provider = _read(VISION_PROVIDER)
        self.assertIn('"presence faces=$faceCount luma=$luma"', provider)
        legacy = provider.index('"presence faces=$faceCount luma=$luma"')
        appended = provider.index('" raw=$faceCount stretched=$stretchedCount"')
        self.assertLess(legacy, appended,
                        "new fields were inserted before the legacy ones")

    def test_the_tool_still_parses_a_line_recorded_before_the_change(self):
        presence = _correlation_module().PRESENCE
        match = presence.search("I VisionProvider: presence faces=1 luma=61")
        self.assertIsNotNone(match)
        self.assertEqual((match.group(1), match.group(2)), ("1", "61"))
        self.assertIsNone(match.group(3))
        self.assertIsNone(match.group(9), "an old line has no verdict field")

    def test_the_tool_parses_the_new_fields(self):
        presence = _correlation_module().PRESENCE
        match = presence.search(
            "I VisionProvider: presence faces=0 luma=6 unavailable=too_dark"
            " raw=0 stretched=1 motion=2.4 width=320 dark=1 verdict=motion")
        self.assertIsNotNone(match)
        self.assertEqual((match.group(1), match.group(2), match.group(3)),
                         ("0", "6", "too_dark"))
        self.assertEqual((match.group(4), match.group(5)), ("0", "1"))
        self.assertEqual(match.group(6), "2.4")
        self.assertEqual((match.group(7), match.group(8), match.group(9)),
                         ("320", "1", "motion"))


@unittest.skipUnless(os.path.isfile(CORRELATION),
                     "the host-side correlation tool is a local diagnostic "
                     "(/runtime/ is ignored), so this checkout may not have it")
class DeviceThresholdWinsTestCase(unittest.TestCase):
    """Darkness is the device's calibrated judgement, not a hard-coded luma."""

    @staticmethod
    def _sample(entries):
        return {"audio": [], "presence": entries}

    def test_a_luma_above_the_old_hard_coded_twelve_still_reads_as_dark(self):
        module = _correlation_module()
        devices = {"a51": self._sample([
            {"t": 0.0, "faces": 0, "luma": 30, "unavailable": "", "dark": 1},
            {"t": 40.0, "faces": 1, "luma": 70, "unavailable": "", "dark": 0},
        ])}
        result = module.presence_agreement(devices)
        self.assertEqual(result["dark_buckets"].get("a51"), 1,
                         "the device's own dark verdict was ignored: luma=30 is "
                         "above the tool's old hard-coded 12")

    def test_a_series_without_the_new_field_keeps_the_luma_rule(self):
        module = _correlation_module()
        devices = {"a51": self._sample([
            {"t": 0.0, "faces": 0, "luma": 30, "unavailable": ""},
        ])}
        result = module.presence_agreement(devices)
        self.assertEqual(result["dark_buckets"].get("a51"), 0)


class ExposureIsOptInTestCase(unittest.TestCase):
    """The one night-vision lever that also moves NRR's pixels, kept default-off."""

    def test_default_is_off_and_clears_any_override(self):
        provider = _read(VISION_PROVIDER)
        self.assertRegex(provider, r"@Volatile private var exposureSteps: Int = 0")
        self.assertIn("control.clearCaptureRequestOptions()", provider)

    def test_the_override_is_a_camera2_ae_option_not_a_rebind(self):
        provider = _read(VISION_PROVIDER)
        self.assertIn("Camera2CameraControl.from(camera.cameraControl)", provider)
        self.assertIn("CaptureRequest.CONTROL_AE_EXPOSURE_COMPENSATION", provider)

    def test_a_camera_bound_later_still_receives_the_setting(self):
        provider = _read(VISION_PROVIDER)
        bound = provider.index("boundCamera = camera")
        applied = provider.index("applyExposureToCamera()", bound)
        self.assertLess(
            bound, applied,
            "exposure is only applied to a camera that was already bound, so a "
            "setting made before the camera came up would be silently ignored")

    def test_the_pref_reaches_the_provider(self):
        service = _read(KOTLIN_SERVICE)
        self.assertIn('getInt("vision_exposure_steps", 0)', service)
        self.assertIn("visionProvider?.applyExposureCompensation(", service)


class PolicyWiringTestCase(unittest.TestCase):
    """The threshold and the calibration switch have to reach the provider."""

    def test_the_threshold_and_calibration_prefs_are_read(self):
        service = _read(KOTLIN_SERVICE)
        self.assertIn('getInt("vision_dark_luma_max", -1)', service)
        self.assertIn('getBoolean("vision_calibration", false)', service)
        self.assertIn("visionProvider?.applyDarkLumaMax(", service)

    def test_the_policy_is_applied_at_boot_and_on_the_recheck(self):
        service = _read(KOTLIN_SERVICE)
        self.assertGreaterEqual(
            service.count("applyVisionPolicy()"), 2,
            "the vision policy is applied only once: a threshold set between runs "
            "would never arrive, and a relaunch would keep the old one")


class VisionControlSurfaceTestCase(unittest.TestCase):
    """The SENSORS controls, the host contract and the service must agree.

    The pane, the activity and the service each name the same three preferences by
    string; nothing checks that at build time, and a typo in one of them would be a
    control that quietly does nothing. The pane is also checked for showing the
    value the device is *running with* rather than echoing the request, because a
    calibration run reads those numbers as fact.
    """

    KEYS = ("vision_dark_luma_max", "vision_calibration", "vision_exposure_steps")

    def test_the_host_contract_declares_both_vision_methods(self):
        host = _read(CONTROL_HOST)
        self.assertIn("fun visionPolicy(): Map<String, Any>", host)
        self.assertIn("fun onVisionPolicyChanged(key: String, value: Any)", host)

    def test_the_activity_can_write_every_key_the_pane_uses(self):
        activity = _read(MAIN_ACTIVITY)
        for key in self.KEYS:
            self.assertIn(key, activity, "MainActivity never names %s" % key)
        self.assertIn("service()?.applyVisionPolicyNow()", activity)

    def test_the_service_keeps_reading_the_same_keys(self):
        service = _read(KOTLIN_SERVICE)
        for key in self.KEYS:
            self.assertIn(key, service, "the service never reads %s" % key)

    def test_the_immediate_apply_stays_off_the_ui_thread(self):
        """An operator tap must not make the main thread wait on the camera.

        The exposure value ends up in Camera2 capture-request options. Applying it
        on the caller's thread is how a tap in this pane became "Input dispatching
        timed out" on the S9FE while the camera was busy.
        """
        service = _read(KOTLIN_SERVICE)
        body = service.split("fun applyVisionPolicyNow")[1].split("private fun")[0]
        self.assertIn("executor.execute", body)
        self.assertNotIn("= applyVisionPolicy()", body)

    def test_the_pane_shows_applied_state_alongside_the_request(self):
        pane = _read(SENSORS_PANE)
        for key in self.KEYS:
            self.assertIn(key, pane, "the SENSORS pane has no control for %s" % key)
        self.assertIn("PerceptionState.visionDarkThreshold", pane)
        self.assertIn("PerceptionState.visionExposureSteps", pane)
        self.assertIn('"%d (pref %d)"', pane)

    def test_the_pane_reads_policy_through_the_host(self):
        """One read path, so the pane cannot invent its own defaults."""
        pane = _read(SENSORS_PANE)
        self.assertIn("host.visionPolicy()", pane)
        self.assertNotIn("host.prefs()", pane)


if __name__ == "__main__":
    unittest.main()
