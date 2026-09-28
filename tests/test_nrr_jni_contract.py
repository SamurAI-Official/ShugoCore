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
import shutil
import subprocess
import tempfile
import unittest
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ANDROID = os.path.join(ROOT, "platforms", "android", "app", "src", "main")
KOTLIN_BRIDGE = os.path.join(ANDROID, "java", "com", "samurai", "shugocore",
                             "inference", "NRRBridge.kt")
KOTLIN_SERVICE = os.path.join(ANDROID, "java", "com", "samurai", "shugocore",
                              "ShugoCoreService.kt")
JNI_SOURCE = os.path.join(ANDROID, "cpp", "nrr_jni.cpp")
CMAKE = os.path.join(ANDROID, "cpp", "CMakeLists.txt")
ADAPTER = os.path.join(ROOT, "nrr", "adapter.py")
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


def _find_llvm_nm():
    for root in (os.environ.get("ANDROID_NDK_HOME"), os.environ.get("ANDROID_NDK")):
        if root and os.path.isdir(root):
            for base, _dirs, files in os.walk(root):
                for candidate in ("llvm-nm.exe", "llvm-nm"):
                    if candidate in files:
                        return os.path.join(base, candidate)
    return shutil.which("llvm-nm")


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


class ShippedSymbolTestCase(unittest.TestCase):
    """The built library, not the source: what a device actually dlopen()s."""

    @unittest.skipUnless(os.path.isfile(APK),
                         "no APK built in this checkout (assembleDebug)")
    def test_the_shipped_so_exports_every_jni_entry_point(self):
        nm = _find_llvm_nm()
        if not nm:
            self.skipTest("no llvm-nm found (point ANDROID_NDK_HOME at an NDK)")
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
