"""The NRR integration's cross-language seams, checked where they can break.

Three languages have to agree by *name*, and every one of those names fails at
runtime rather than at build time:

* `NRRBridge.kt` declares `external fun nativeX(...)`; `nrr_jni.cpp` exports
  `Java_com_samurai_shugocore_inference_NRRBridge_nativeX`. A rename on either side
  is an `UnsatisfiedLinkError` the first time a device renders.
* Python's `android_native_worker` calls `isAvailable` / `renderFrame` /
  `capabilitiesJson` / `backendName` on the object the service registers, and Kotlin's
  `NrrRendererBridge` is that object -- `ShugoCoreService.kt` says so in a comment
  ("Keep these names in sync with nrr/adapter.py::android_native_worker") but nothing
  enforced it. Chaquopy reports a missing attribute as a Python `AttributeError`, which
  the worker's fail-closed path swallows: the fleet would simply never advertise
  `nrr_render`, with no error anywhere.
* `System.loadLibrary("nrr_jni")` has to name the CMake target that produces the file
  Gradle packages.

The last test reads the symbols out of the .so inside the built APK, which is the only
form of this contract that can catch a stale or stripped build.
"""
import os
import re
import subprocess
import tempfile
import unittest
import zipfile

from tests.android_toolchain import NO_NDK, find_llvm_nm

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ANDROID = os.path.join(ROOT, "platforms", "android", "app", "src", "main")
KOTLIN_BRIDGE = os.path.join(ANDROID, "java", "com", "samurai", "shugocore",
                             "inference", "NRRBridge.kt")
KOTLIN_SERVICE = os.path.join(ANDROID, "java", "com", "samurai", "shugocore",
                              "ShugoCoreService.kt")
JNI_SOURCE = os.path.join(ANDROID, "cpp", "nrr_jni.cpp")
CMAKE = os.path.join(ANDROID, "cpp", "CMakeLists.txt")
ADAPTER = os.path.join(ROOT, "nrr", "adapter.py")
VISION_PROVIDER = os.path.join(ANDROID, "java", "com", "samurai", "shugocore",
                               "runtime", "VisionProvider.kt")
AGENT = os.path.join(ROOT, "shugocore_agent.py")
APK = os.path.join(ROOT, "platforms", "android", "app", "build", "outputs",
                   "apk", "debug", "app-debug.apk")
JNI_PREFIX = "Java_com_samurai_shugocore_inference_NRRBridge_"


def _read(path):
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def _params(text, start):
    """The balanced parameter list beginning at the '(' index `start`."""
    depth = 0
    for index in range(start, len(text)):
        char = text[index]
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return text[start + 1:index]
    raise AssertionError("unbalanced parentheses")


def _arity(params):
    """Count parameters, ignoring a trailing comma (Kotlin allows one)."""
    inner = params.strip()
    if not inner:
        return 0
    return len([part for part in inner.split(",") if part.strip()])


class JniNameContractTestCase(unittest.TestCase):
    """Kotlin's externals and the C exports must be the same set of names."""

    def test_every_kotlin_external_is_exported_and_nothing_else_is(self):
        kotlin = set(re.findall(r"external fun\s+(\w+)\s*\(", _read(KOTLIN_BRIDGE)))
        exported = set(re.findall(JNI_PREFIX + r"(\w+)\s*\(", _read(JNI_SOURCE)))
        self.assertTrue(kotlin, "no externals parsed out of NRRBridge.kt")
        self.assertEqual(
            sorted(kotlin - exported), [],
            "NRRBridge.kt declares externals the native library does not export "
            "(UnsatisfiedLinkError on a device)")
        self.assertEqual(
            sorted(exported - kotlin), [],
            "nrr_jni.cpp exports entry points no Kotlin declaration reaches")

    def test_jni_arity_matches_the_kotlin_declaration(self):
        """JNI adds (JNIEnv*, jobject/jclass); the rest must line up exactly."""
        kotlin = dict(re.findall(r"external fun\s+(\w+)\s*\(([^)]*)\)",
                                 _read(KOTLIN_BRIDGE)))
        source = _read(JNI_SOURCE)
        checked = 0
        for match in re.finditer(JNI_PREFIX + r"(\w+)\s*\(", source):
            name = match.group(1)
            self.assertIn(name, kotlin, f"{name} has no Kotlin declaration")
            native_arity = _arity(_params(source, match.end() - 1))
            self.assertEqual(
                native_arity - 2, _arity(kotlin[name]),
                f"{name}: native takes {native_arity - 2} argument(s) after "
                f"JNIEnv*/jobject, Kotlin passes {_arity(kotlin[name])}")
            checked += 1
        self.assertGreaterEqual(checked, 7, "the JNI surface shrank unexpectedly")


class BridgeProtocolTestCase(unittest.TestCase):
    """The names Python calls and the names Kotlin defines must be the same."""

    def test_the_python_worker_only_calls_methods_the_kotlin_adapter_has(self):
        adapter = _read(ADAPTER)
        service = _read(KOTLIN_SERVICE)
        body = service[service.index("class NrrRendererBridge"):]
        kotlin_methods = set(re.findall(r"fun\s+(\w+)\s*\(", body))
        called = set(re.findall(r"renderer_bridge\.(\w+)\(", adapter))
        self.assertTrue(called, "no bridge calls parsed out of nrr/adapter.py")
        self.assertEqual(
            sorted(called - kotlin_methods), [],
            "nrr/adapter.py calls NrrRendererBridge methods that do not exist. "
            "The worker's fail-closed path hides this: the fleet would simply "
            "never advertise nrr_render, with no error anywhere.")
        # The four the Android side's own comment names, explicitly.
        for name in ("isAvailable", "renderFrame", "capabilitiesJson",
                     "backendName"):
            self.assertIn(name, called)
            self.assertIn(name, kotlin_methods)

    def test_the_registration_call_and_the_python_method_agree(self):
        self.assertIn('callAttr("register_nrr_renderer"', _read(KOTLIN_SERVICE))
        self.assertRegex(_read(AGENT),
                         re.compile(r"def register_nrr_renderer\s*\(", re.M))

    def test_the_loaded_library_is_the_target_cmake_builds(self):
        libraries = re.findall(r'System\.loadLibrary\("(\w+)"\)',
                               _read(KOTLIN_BRIDGE))
        self.assertEqual(libraries, ["nrr_jni"])
        self.assertRegex(_read(CMAKE), re.compile(
            rf"add_library\(\s*{libraries[0]}\s+SHARED\b"))


class ResolutionPolicyTestCase(unittest.TestCase):
    """NRR's power manager owns the frame resolution; three files must agree.

    The analysed frame *is* the frame NRR renders -- `VisionProvider` publishes the
    same bitmap it detects faces in -- so a resolution decision made in one place
    and not the others is either a policy that silently never applies, or a frame
    geometry that changes without NRR asking for it. These checks read the four
    numbers involved (NRR's `min_resolution_scale` / `max_resolution_scale`, the
    provider's base and floor widths) plus the wiring that carries the advice from
    the native runtime to the analyser.

    Worth knowing when reading the stub guard below: `docs/nrr_android_port.md`
    records that the Android power-manager hooks exist nowhere upstream, so on
    this platform NRR's inputs are placeholders reporting "0% battery, not
    charging" -- which is under `critical_battery_threshold` and would pin the
    scale to its minimum on every phone forever.
    """

    def test_the_provider_widths_mirror_nrr_scale_bounds(self):
        native = _read(JNI_SOURCE)
        provider = _read(VISION_PROVIDER)
        low = re.search(r"min_resolution_scale\s*=\s*([\d.]+)f", native)
        high = re.search(r"max_resolution_scale\s*=\s*([\d.]+)f", native)
        self.assertTrue(low and high,
                        "nrr_jni.cpp no longer declares resolution scale bounds")
        base = re.search(r"BASE_ANALYSIS_WIDTH\s*=\s*(\d+)", provider)
        floor = re.search(r"MIN_ANALYSIS_WIDTH\s*=\s*(\d+)", provider)
        self.assertTrue(base and floor,
                        "VisionProvider no longer declares both analysis widths")
        base, floor = int(base.group(1)), int(floor.group(1))
        self.assertEqual(
            floor / base, float(low.group(1)),
            "the floor width is no longer NRR's min_resolution_scale of the base "
            "width: the advice would map onto a range NRR never asked for")
        self.assertEqual(
            float(high.group(1)), 1.0,
            "NRR's max_resolution_scale moved, but the base width is defined as it")
        self.assertEqual(base % 2, 0, "FaceDetector needs an even width")
        self.assertEqual(floor % 2, 0, "FaceDetector needs an even width")

    def test_the_analysis_width_is_state_not_a_constant(self):
        provider = _read(VISION_PROVIDER)
        self.assertRegex(provider, r"frameToRgb565\(proxy,\s*analysisWidth\)")
        self.assertIsNone(
            re.search(r"(?<!BASE_)(?<!MIN_)ANALYSIS_WIDTH", provider),
            "a hard-coded analysis width is back: the frame geometry would stop "
            "following NRR's advice -- and NRR renders the very same frame")
        self.assertRegex(provider, r"BASE_ANALYSIS_WIDTH \* advised")
        self.assertRegex(provider,
                         r"coerceIn\(MIN_ANALYSIS_WIDTH, BASE_ANALYSIS_WIDTH\)")
        # Both dimensions of the scaled frame have to stay even.
        self.assertRegex(provider, r"if \(height % 2 == 1\) height \+= 1")

    def test_the_published_frame_keeps_the_bitmap_geometry(self):
        provider = _read(VISION_PROVIDER)
        self.assertIn("val w = bitmap.width", provider)
        self.assertIn("val h = bitmap.height", provider)
        self.assertRegex(provider, r"stampFrameRgba\(w,\s*h,\s*rgba\)")

    def test_kotlin_reads_the_scale_and_rejects_stub_power_inputs(self):
        bridge = _read(KOTLIN_BRIDGE)
        self.assertRegex(bridge, r"fun advisedResolutionScale\(\):\s*Double\?")
        self.assertIn('status["resolution_scale"]', bridge)
        # The native session is single-threaded, so the power query is serialized
        # with renders: the tick and the API server call it while the camera may be
        # rendering.
        self.assertRegex(bridge,
                         r"synchronized\(renderLock\)\s*\{\s*return parseJson\("
                         r"nativePowerStatusJson")
        self.assertRegex(
            bridge, r"battery <= 0\.0 && charging == 0",
            "the stub guard is gone: docs/nrr_android_port.md records that the "
            "Android power hooks are absent upstream, so a stub's '0% battery, "
            "not charging' would pin the scale to its minimum forever")

    def test_the_service_applies_the_advice_at_boot_and_on_a_slow_cadence(self):
        service = _read(KOTLIN_SERVICE)
        self.assertGreaterEqual(
            service.count("applyAdvisedResolutionScale"), 2,
            "the advice is applied only once: a thermal throttle request would "
            "never reach the analyser")
        self.assertIn("nrr.advisedResolutionScale()", service)
        self.assertIn("nrrBridge?.advisedResolutionScale()", service)
        self.assertRegex(service, r"NRR_SCALE_RECHECK_MS\s*=\s*[\d_]+L")

    def test_the_power_status_keys_are_the_same_on_both_sides(self):
        native = _read(JNI_SOURCE)
        bridge = _read(KOTLIN_BRIDGE)
        # Scoped to the power-status body: the same file publishes other JSON
        # (capabilities, for one), and a key name found there would not be this
        # contract. Kept as bare spellings -- the C quotes are escaped in the
        # format string, so the escaping is not what is being asserted.
        body = native[native.index("nativePowerStatusJson"):]
        body = body[:body.index("return to_jstr(env, buf)")]
        for key in ("battery_level", "charging", "thermal_headroom",
                    "low_power_mode", "profile", "resolution_scale"):
            self.assertIn(key, body,
                          "nrr_jni.cpp no longer publishes the %s key" % key)
            self.assertIn(key, bridge,
                          "NRRBridge does not mention %s" % key)


class ShippedSymbolTestCase(unittest.TestCase):
    """The built library, not the source: what a device actually dlopen()s."""

    @unittest.skipUnless(os.path.isfile(APK),
                         "no APK built in this checkout (assembleDebug)")
    def test_the_shipped_so_exports_every_jni_entry_point(self):
        nm = find_llvm_nm()
        if not nm:
            self.skipTest(NO_NDK)
        exported = set(re.findall(JNI_PREFIX + r"(\w+)\s*\(", _read(JNI_SOURCE)))
        with zipfile.ZipFile(APK) as apk:
            names = [n for n in apk.namelist()
                     if n.endswith("libnrr_jni.so") and "arm64-v8a" in n]
            self.assertTrue(names, "the APK carries no arm64-v8a libnrr_jni.so")
            with tempfile.TemporaryDirectory() as tmp:
                target = os.path.join(tmp, "libnrr_jni.so")
                with open(target, "wb") as handle:
                    handle.write(apk.read(names[0]))
                out = subprocess.run([nm, "-D", "--defined-only", target],
                                     capture_output=True, text=True, timeout=90)
        found = set(re.findall(JNI_PREFIX + r"(\w+)", out.stdout))
        self.assertEqual(
            sorted(exported - found), [],
            "the libnrr_jni.so packaged in the APK does not export: "
            + ", ".join(sorted(exported - found)))


if __name__ == "__main__":
    unittest.main()
