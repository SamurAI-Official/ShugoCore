"""Where the Android toolchain is, asked the way the build asks it.

The JNI contract tests read symbols out of the built APK, which needs an ``llvm-nm``
from the same NDK the build used. That binary is easy to be *near* and still miss:
``ANDROID_NDK_HOME`` is unset on a machine where Gradle finds the NDK through
``local.properties`` -- which is how the SDK is supposed to be located, and why two
tests skipped here while a built APK and a complete NDK sat five directories apart.

So the search order is the build's own:

1. the environment, when someone has pointed at an NDK explicitly;
2. ``platforms/android/local.properties``' ``sdk.dir``, which is where Gradle reads it;
3. ``ANDROID_HOME`` / ``ANDROID_SDK_ROOT``, the SDK roots CI exports;
4. the NDK recorded in this checkout's own CMake cache, when a native build has
   already run -- the exact binary that produced the library under test;
5. ``PATH``.

Nothing here is required. A checkout with no NDK, no ``local.properties`` and no
native build gets ``None``, and the caller skips with a reason.
"""
import os
import re
import shutil

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOCAL_PROPERTIES = os.path.join(ROOT, "platforms", "android", "local.properties")
CXX_DIR = os.path.join(ROOT, "platforms", "android", "app", ".cxx")
NAMES = ("llvm-nm.exe", "llvm-nm")


def _version_key(name):
    """Sort versions numerically: 27.0.12077973 is newer than 9.0.1, unlike strsort."""
    parts = re.findall(r"\d+", name)
    return [int(part) for part in parts] or [0]


def _from_toolchain_layout(root):
    """The fast path: <ndk>/toolchains/llvm/prebuilt/<host>/bin/llvm-nm."""
    prebuilt = os.path.join(root, "toolchains", "llvm", "prebuilt")
    if not os.path.isdir(prebuilt):
        return None
    for host in sorted(os.listdir(prebuilt)):
        for name in NAMES:
            candidate = os.path.join(prebuilt, host, "bin", name)
            if os.path.isfile(candidate):
                return candidate
    return None


def _from_ndk_root(root):
    """An NDK root, by its layout first and by a walk second (a walk is the last
    resort on purpose: an NDK is tens of thousands of files)."""
    if not root or not os.path.isdir(root):
        return None
    found = _from_toolchain_layout(root)
    if found:
        return found
    for base, dirs, files in os.walk(root):
        dirs.sort(key=_version_key, reverse=True)
        for name in NAMES:
            if name in files:
                return os.path.join(base, name)
    return None


def _from_sdk_root(sdk):
    """An SDK root: whichever NDK under it is newest."""
    if not sdk or not os.path.isdir(sdk):
        return None
    ndk = os.path.join(sdk, "ndk")
    if not os.path.isdir(ndk):
        return None
    for version in sorted(os.listdir(ndk), key=_version_key, reverse=True):
        found = _from_ndk_root(os.path.join(ndk, version))
        if found:
            return found
    return None


def _sdk_from_local_properties():
    try:
        with open(LOCAL_PROPERTIES, encoding="utf-8") as handle:
            text = handle.read()
    except OSError:
        return ""
    match = re.search(r"^\s*sdk\.dir\s*=\s*(.+?)\s*$", text, re.MULTILINE)
    return match.group(1).replace("\\", "/") if match else ""


def _from_cmake_cache():
    """The nm this checkout's own native build configured, if one has run."""
    if not os.path.isdir(CXX_DIR):
        return None
    for base, _dirs, files in os.walk(CXX_DIR):
        if "CMakeCache.txt" not in files:
            continue
        try:
            with open(os.path.join(base, "CMakeCache.txt"), encoding="utf-8",
                      errors="replace") as handle:
                text = handle.read()
        except OSError:
            continue
        match = re.search(r"^CMAKE_NM:FILEPATH=(.+?)\s*$", text, re.MULTILINE)
        if match and os.path.isfile(match.group(1)):
            return match.group(1)
    return None


def find_llvm_nm():
    """The llvm-nm to read a built .so with, or None when this host has none."""
    for root in (os.environ.get("ANDROID_NDK_HOME"), os.environ.get("ANDROID_NDK")):
        found = _from_ndk_root(root)
        if found:
            return found
    for sdk in (_sdk_from_local_properties(), os.environ.get("ANDROID_HOME"),
                os.environ.get("ANDROID_SDK_ROOT")):
        found = _from_sdk_root(sdk)
        if found:
            return found
    found = _from_cmake_cache()
    if found:
        return found
    return shutil.which("llvm-nm")


NO_NDK = ("no llvm-nm found (no NDK to take it from: set ANDROID_NDK_HOME, or a "
          "sdk.dir in platforms/android/local.properties)")
