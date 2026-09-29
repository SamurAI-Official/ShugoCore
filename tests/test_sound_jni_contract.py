"""The audio bridge's cross-language seams, checked where they can break.

Same discipline as tests/test_nrr_jni_contract.py, applied to the sound bridge. Three
languages have to agree here, and every disagreement fails at runtime rather than at build
time:

* Kotlin declares `external fun nativeX(...)`; sound_jni.cpp exports
  `Java_com_samurai_shugocore_inference_SoundBridge_nativeX`. A rename on either side is an
  `UnsatisfiedLinkError` the first time a device tries to hear.
* The model shapes are stated three times -- Kotlin's constants, the C++ constexprs and
  Python's sound.schema -- so they are compared rather than trusted. Getting the chunk size
  wrong is exactly the bug that made Silero read 0.001 on real speech.
* `System.loadLibrary("sound_jni")` has to name the CMake target, and when an APK has been
  built its `libsound_jni.so` must actually export the entry points.
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
KOTLIN = os.path.join(ANDROID, "java", "com", "samurai", "shugocore", "inference",
                      "SoundBridge.kt")
JNI_SOURCE = os.path.join(ANDROID, "cpp", "sound_jni.cpp")
CMAKE = os.path.join(ANDROID, "cpp", "CMakeLists.txt")
APK = os.path.join(ROOT, "platforms", "android", "app", "build", "outputs", "apk",
                   "debug", "app-debug.apk")
JNI_PREFIX = "Java_com_samurai_shugocore_inference_SoundBridge_"


def read(path):
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def params(text, start):
    """The balanced parameter list beginning at the '(' index `start`."""
    depth = 0
    for index in range(start, len(text)):
        if text[index] == "(":
            depth += 1
        elif text[index] == ")":
            depth -= 1
            if depth == 0:
                return text[start + 1:index]
    raise AssertionError("unbalanced parentheses")


def arity(param_list):
    """Count parameters, ignoring a trailing comma (Kotlin allows one)."""
    inner = param_list.strip()
    return 0 if not inner else len([p for p in inner.split(",") if p.strip()])


def find_llvm_nm():
    for root in (os.environ.get("ANDROID_NDK_HOME"), os.environ.get("ANDROID_NDK")):
        if root and os.path.isdir(root):
            for base, _dirs, files in os.walk(root):
                for candidate in ("llvm-nm.exe", "llvm-nm"):
                    if candidate in files:
                        return os.path.join(base, candidate)
    return shutil.which("llvm-nm")


class JniNameContractTestCase(unittest.TestCase):
    def test_kotlin_externals_and_c_exports_are_the_same_set(self):
        kotlin = set(re.findall(r"external fun\s+(\w+)\s*\(", read(KOTLIN)))
        exported = set(re.findall(JNI_PREFIX + r"(\w+)\s*\(", read(JNI_SOURCE)))
        self.assertTrue(kotlin, "no externals parsed out of SoundBridge.kt")
        self.assertEqual(sorted(kotlin - exported), [],
                         "SoundBridge.kt declares externals the native library does not "
                         "export (UnsatisfiedLinkError on a device)")
        self.assertEqual(sorted(exported - kotlin), [],
                         "sound_jni.cpp exports entry points no Kotlin declaration reaches")

    def test_jni_arity_matches_the_kotlin_declaration(self):
        """JNI adds (JNIEnv*, jclass); the rest must line up exactly."""
        kotlin = dict(re.findall(r"external fun\s+(\w+)\s*\(([^)]*)\)", read(KOTLIN)))
        source = read(JNI_SOURCE)
        checked = 0
        for match in re.finditer(JNI_PREFIX + r"(\w+)\s*\(", source):
            name = match.group(1)
            self.assertIn(name, kotlin, f"{name} has no Kotlin declaration")
            native = arity(params(source, match.end() - 1))
            self.assertEqual(native - 2, arity(kotlin[name]),
                             f"{name}: native takes {native - 2} argument(s) after "
                             f"JNIEnv*/jclass, Kotlin passes {arity(kotlin[name])}")
            checked += 1
        self.assertGreaterEqual(checked, 6, "the JNI surface shrank unexpectedly")


class ModelShapeAgreementTestCase(unittest.TestCase):
    """The same model contract is stated three times; the three must agree."""

    def test_chunk_and_window_match_across_kotlin_cpp_and_python(self):
        from sound.schema import SAMPLE_RATE, VAD_CHUNK, WINDOW_SAMPLES

        kotlin = dict(re.findall(r"const val (\w+) = (\d+)", read(KOTLIN)))
        self.assertEqual(int(kotlin["CHUNK_SAMPLES"]), VAD_CHUNK)
        self.assertEqual(int(kotlin["WINDOW_SAMPLES"]), WINDOW_SAMPLES)
        self.assertEqual(int(kotlin["SAMPLE_RATE"]), SAMPLE_RATE)

        source = read(JNI_SOURCE)
        self.assertIn("kVadChunk = %d" % VAD_CHUNK, source)
        self.assertIn("kWindow = %d" % WINDOW_SAMPLES, source)
        self.assertIn("kSampleRate = %d" % SAMPLE_RATE, source)

    def test_the_loaded_library_is_the_target_cmake_builds(self):
        libraries = re.findall(r'System\.loadLibrary\("(\w+)"\)', read(KOTLIN))
        self.assertEqual(libraries, ["sound_jni"])
        cmake = read(CMAKE)
        self.assertRegex(cmake, re.compile(rf"add_library\(\s*{libraries[0]}\s+SHARED\b"))
        self.assertIn("option(SHUGOCORE_SOUND", cmake)


class ShippedSymbolTestCase(unittest.TestCase):
    """The built library, not the source: what a device actually dlopen()s."""

    @unittest.skipUnless(os.path.isfile(APK),
                         "no APK built in this checkout (assembleDebug)")
    def test_the_shipped_so_exports_every_jni_entry_point(self):
        nm = find_llvm_nm()
        if not nm:
            self.skipTest("no llvm-nm found (point ANDROID_NDK_HOME at an NDK)")
        exported = set(re.findall(JNI_PREFIX + r"(\w+)\s*\(", read(JNI_SOURCE)))
        with zipfile.ZipFile(APK) as apk:
            names = [n for n in apk.namelist()
                     if n.endswith("libsound_jni.so") and "arm64-v8a" in n]
            self.assertTrue(names, "the APK carries no arm64-v8a libsound_jni.so")
            with tempfile.TemporaryDirectory() as tmp:
                target = os.path.join(tmp, "libsound_jni.so")
                with open(target, "wb") as handle:
                    handle.write(apk.read(names[0]))
                out = subprocess.run([nm, "-D", "--defined-only", target],
                                     capture_output=True, text=True, timeout=90)
        found = set(re.findall(JNI_PREFIX + r"(\w+)", out.stdout))
        self.assertEqual(sorted(exported - found), [],
                         "the libsound_jni.so in the APK does not export: "
                         + ", ".join(sorted(exported - found)))

    @unittest.skipUnless(os.path.isfile(APK), "no APK built in this checkout")
    def test_the_models_ship_in_the_apk(self):
        """A bridge with no models reports no_model forever; this catches that."""
        with zipfile.ZipFile(APK) as apk:
            names = set(apk.namelist())
        for name in ("assets/sound/silero_vad.onnx", "assets/sound/yamnet_int8.onnx",
                     "assets/sound/yamnet_class_map.csv"):
            self.assertIn(name, names, f"{name} is not packaged in the APK")


if __name__ == "__main__":
    unittest.main()
