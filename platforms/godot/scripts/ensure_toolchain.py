#!/usr/bin/env python3
"""Ensure the headless Android export toolchain is resolvable (Phase 4).

Godot's Android exporter reads two paths from EditorSettings — NOT from
the process environment — so a headless `--export-debug` fails unless
they are present in editor_settings-4.7.tres:

  - export/android/java_sdk_path   (OpenJDK 17; Gradle refuses JDK 21)
  - export/android/debug_keystore  (+ user/pass)

This script is idempotent: it only writes what is missing or wrong, and
it never invents a toolchain — every path must exist on disk or the
problem is reported instead of papered over.

Also checks (report-only): export templates for the running engine
version, and the SDK components the Gradle build needs.

Usage: python3 ensure_toolchain.py [--fix]
"""
import glob
import os
import shutil
import subprocess
import sys
from pathlib import Path

HOME = Path.home()
GODOT_APP = Path("/Applications/Godot.app/Contents/MacOS/Godot")
SUPPORT = HOME / "Library" / "Application Support" / "Godot"
SETTINGS = SUPPORT / "editor_settings-4.7.tres"
JDK17_CANDIDATES = [
    HOME / "opt/homebrew/opt/openjdk@17/libexec/openjdk.jdk/Contents/Home",
    Path("/opt/homebrew/opt/openjdk@17/libexec/openjdk.jdk/Contents/Home"),
    Path("/Library/Java/JavaVirtualMachines/temurin-17/Contents/Home"),
]
SDK = Path(os.environ.get("ANDROID_HOME", HOME / "Library/Android/sdk"))
KEYSTORE = SUPPORT / "keystores" / "debug.keystore"
ANDROID_DEBUG_KEYSTORE = HOME / ".android" / "debug.keystore"


def godot_version() -> str:
    """Raw and short version. `4.7.2.stable.official.ed1daf0bf` ->
    short `4.7.2.stable`, which is the templates directory name."""
    try:
        out = subprocess.run([str(GODOT_APP), "--version"],
                             capture_output=True, text=True,
                             timeout=30).stdout.strip()
        raw = out.replace("Godot Engine v", "").split(" ")[0]
        short = raw.split(".official")[0] if ".official" in raw else raw
        return short
    except Exception as exc:
        print(f"  [warn] cannot read Godot version: {exc}")
        return "unknown"


def templates_installed(version: str) -> bool:
    d = SUPPORT / "export_templates" / version
    ok = (d / "android_debug.apk").exists() and \
         (d / "android_release.apk").exists()
    return ok


def jdk17() -> Path | None:
    for c in JDK17_CANDIDATES:
        if (c / "bin" / "java").exists():
            return c
    return None


def patch_settings(fix: bool) -> list:
    findings = []
    if not SETTINGS.exists():
        print("  [FAIL] editor settings missing — open Godot once to create")
        return findings
    text = SETTINGS.read_text()
    jdk = jdk17()
    if jdk is None:
        print("  [FAIL] no OpenJDK 17 found (Gradle refuses JDK 21). "
              "Install: brew install openjdk@17")
        return findings

    want_java = f'export/android/java_sdk_path = "{jdk}"'
    if 'export/android/java_sdk_path = ""' in text:
        findings.append(("fix", f"java_sdk_path -> {jdk}"))
        if fix:
            text = text.replace('export/android/java_sdk_path = ""',
                                want_java, 1)
    elif want_java in text:
        findings.append(("ok", "java_sdk_path already correct"))
    else:
        findings.append(("ok", "java_sdk_path set to something else "
                               "(left untouched)"))

    if "export/android/debug_keystore_user" not in text:
        findings.append(("fix", "debug_keystore_user -> androiddebugkey"))
        if fix:
            text = text.replace(
                "export/android/debug_keystore_pass =",
                'export/android/debug_keystore_user = "androiddebugkey"\n'
                'export/android/debug_keystore_pass =', 1)
    else:
        findings.append(("ok", "debug_keystore_user present"))

    if not KEYSTORE.exists():
        if ANDROID_DEBUG_KEYSTORE.exists():
            findings.append(("fix", f"copy debug keystore -> {KEYSTORE}"))
            if fix:
                KEYSTORE.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy(ANDROID_DEBUG_KEYSTORE, KEYSTORE)
        else:
            findings.append(("fail", "no debug keystore anywhere "
                                     "(~/.android/debug.keystore missing)"))
    else:
        findings.append(("ok", "debug keystore present"))

    if fix:
        SETTINGS.write_text(text)
    return findings


def check_sdk() -> None:
    for comp, need in (("build-tools/36.1.0", "aapt2"),
                       ("platforms/android-36", "android.jar")):
        p = SDK / comp / need
        print(f"  [{'ok' if p.exists() else 'warn'}] {comp}/{need}")
    adb = SDK / "platform-tools" / "adb"
    print(f"  [{'ok' if adb.exists() else 'FAIL'}] platform-tools/adb")


def main() -> int:
    fix = "--fix" in sys.argv
    version = godot_version()
    print(f"godot: {version}")
    ok = templates_installed(version)
    print(f"  [{'ok' if ok else 'FAIL'}] export templates {version}")
    if not ok:
        print("         install: Editor > Manage Export Templates, or place "
              "4.7.2-stable/*.apk under ~/Library/Application Support/Godot/"
              "export_templates/")
    check_sdk()
    for kind, msg in patch_settings(fix):
        print(f"  [{kind:4s}] {msg}")
    bad = [k for k, _ in patch_settings(False) if k == "fail"]
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
