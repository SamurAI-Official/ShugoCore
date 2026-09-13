"""NRR native Android port wiring (structural drift guard).

The Android app compiles upstream NRR's C++ runtime (vendored as a git
submodule) plus an ONNX Runtime extracted from the official Maven AAR. None of
that can run in CI without an NDK/device, so this locks in the *wiring* that
is easy to break silently:

  * the submodule is registered and present,
  * ORT stays a hard requirement (upstream's ORT-less path does not compile),
  * the upstream portability patches we depend on are still applied,
  * Gradle packages the same .so that CMake links.

Device-level behavior is covered by nrr_probe (see docs/nrr_android_port.md).
"""
import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CPP = os.path.join(ROOT, "platforms", "android", "app", "src", "main", "cpp")
CMAKE = os.path.join(CPP, "CMakeLists.txt")
GRADLE = os.path.join(ROOT, "platforms", "android", "app", "build.gradle")
GITMODULES = os.path.join(ROOT, ".gitmodules")
GITIGNORE = os.path.join(ROOT, ".gitignore")
SUBMODULE = os.path.join(CPP, "nrr")
ORT = os.path.join(CPP, "onnxruntime")
FETCH = os.path.join(ROOT, "scripts", "fetch_ort_android.sh")


def read(path):
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()


class TestNrrAndroidPortWiring(unittest.TestCase):
    def test_submodule_registered_and_present(self):
        self.assertIn("platforms/android/app/src/main/cpp/nrr", read(GITMODULES))
        for rel in ("include/nrr.h", "runtime/mobile/mobile_kernel.cpp",
                    "runtime/platform/android/nrr_android.cpp"):
            self.assertTrue(os.path.isfile(os.path.join(SUBMODULE, rel)),
                            f"submodule missing {rel}")

    def test_fetch_script_present_and_executable(self):
        self.assertTrue(os.path.isfile(FETCH), "scripts/fetch_ort_android.sh missing")
        self.assertTrue(os.access(FETCH, os.X_OK),
                        "scripts/fetch_ort_android.sh not executable")

    def test_onnxruntime_is_a_hard_requirement(self):
        """Upstream's ORT-less path does not compile (provider_note_ is
        declared only under NRR_HAVE_ONNXRUNTIME), so a missing ORT must fail
        the configure rather than silently fall back."""
        cmake = read(CMAKE)
        self.assertIn("NRR_HAVE_ONNXRUNTIME=1", cmake)
        self.assertIn("ONNX Runtime headers missing", cmake)
        self.assertIn("ONNX Runtime library missing", cmake)
        # Upstream's Windows-only detection (which made NRR_HAVE_ONNXRUNTIME
        # unreachable on Android) must not be reintroduced as logic.
        code = "\n".join(ln for ln in cmake.splitlines()
                         if not ln.lstrip().startswith("#"))
        self.assertNotIn("onnxruntime.dll", code)
        self.assertNotIn("onnxruntime.lib", code)

    def test_android_platform_layer_wired(self):
        """The four power-manager hooks upstream never implemented are
        supplied by ShugoCore and compiled into the runtime."""
        self.assertIn("nrr_android_platform.cpp", read(CMAKE))
        impl = read(os.path.join(CPP, "nrr_android_platform.cpp"))
        for fn in ("android_get_battery_level", "android_get_battery_status",
                   "android_get_thermal_headroom", "android_is_low_power"):
            self.assertIn(fn, impl, f"{fn} not implemented")

    def test_upstream_portability_patches_applied(self):
        """Two upstream bugs would break the Android build if reverted."""
        ort = read(os.path.join(SUBMODULE, "runtime", "onnx_runtime.cpp"))
        # windows.h must be Windows-only (ORTCHAR_T is wchar_t only there).
        self.assertIn("#if defined(_WIN32)", ort)
        self.assertRegex(ort, r"ort_path\s*\(")
        header = read(os.path.join(SUBMODULE, "runtime", "platform",
                                   "android", "nrr_android.h"))
        self.assertIn("android_get_battery_level", header)

    def test_submodule_fixes_are_durable_as_a_patch_series(self):
        """The parent repo records only the submodule SHA, so in-submodule
        fixes must also exist as a re-appliable patch series -- otherwise a
        fresh clone builds a runtime that cannot compile."""
        patch_dir = os.path.join(ROOT, "patches", "nrr")
        patches = [p for p in os.listdir(patch_dir) if p.endswith(".patch")]
        self.assertTrue(patches, "no NRR patch series in patches/nrr")
        blob = "".join(read(os.path.join(patch_dir, p)) for p in patches)
        for needle in ("onnx_runtime.cpp", "nrr_android.h",
                       "nrr_power_manager.cpp"):
            self.assertIn(needle, blob, f"patch series missing {needle}")

        apply_script = os.path.join(ROOT, "scripts", "apply_nrr_patches.sh")
        self.assertTrue(os.path.isfile(apply_script))
        self.assertTrue(os.access(apply_script, os.X_OK))

    def test_gradle_packages_the_linked_onnxruntime(self):
        """One .so: the one CMake links is the one Gradle ships."""
        gradle = read(GRADLE)
        self.assertIn("jniLibs.srcDirs += 'src/main/cpp/onnxruntime/lib'", gradle)
        self.assertIn("platforms/android/app/src/main/cpp/onnxruntime/lib/",
                      read(GITIGNORE))

    def test_probe_target_builds_for_device(self):
        cmake = read(CMAKE)
        self.assertIn("add_executable(nrr_probe nrr_probe.cpp)", cmake)
        self.assertTrue(os.path.isfile(os.path.join(CPP, "nrr_probe.cpp")))


if __name__ == "__main__":
    unittest.main()
