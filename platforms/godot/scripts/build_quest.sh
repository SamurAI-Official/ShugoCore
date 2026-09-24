#!/usr/bin/env bash
# Build the ShugoCore XR Quest debug APK (Phase 4).
#
# Pipeline (all validated headless, no editor required):
#   1. generate export_presets.cfg from the proven Quest reference
#   2. --install-android-build-template  (Gradle shell for the XR loader)
#   3. --export-debug "Quest3" -> ../build/ShugoCoreXR.apk
#   4. (optional) adb install -r  <ADB_SERIAL=... for a chosen device>
#
# Toolchain needs (Phase 0, resolved read-only-safe):
#   - Godot 4.7.2 at /Applications/Godot.app
#   - export templates 4.7.2.stable  (~/Library/Application Support/...)
#   - JDK 17  (/opt/homebrew/opt/openjdk@17)  <- tried installed set first;
#     Gradle refused JDK21 (class file 65), so JDK17 it is.
#   - Android SDK: build-tools/36.1.0 + platforms/android-36 installed.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
GODOT_PROJ="$(cd "$SCRIPT_DIR/.." && pwd)"
GODOT_BIN="${GODOT_BIN:-/Applications/Godot.app/Contents/MacOS/Godot}"
export JAVA_HOME="${JAVA_HOME:-/opt/homebrew/opt/openjdk@17/libexec/openjdk.jdk/Contents/Home}"
export PATH="$JAVA_HOME/bin:$PATH"
export ANDROID_HOME="${ANDROID_HOME:-$HOME/Library/Android/sdk}"
export ANDROID_SDK_ROOT="${ANDROID_SDK_ROOT:-$ANDROID_HOME}"
ADB="${ADB:-$ANDROID_HOME/platform-tools/adb}"

cd "$GODOT_PROJ"
echo "== project: $GODOT_PROJ"
echo "== godot: $($GODOT_BIN --version)"
"$GODOT_BIN" --version >/dev/null

echo "== 0/3 toolchain check (templates, SDK, JDK path, keystore)"
python3 scripts/ensure_toolchain.py --fix

echo "== 1/3 regenerate Quest3 export preset"
python3 scripts/make_quest_preset.py

echo "== 2/3 install android build template"
"$GODOT_BIN" --headless --path "$GODOT_PROJ" --install-android-build-template

APK="$GODOT_PROJ/../build/ShugoCoreXR.apk"
mkdir -p "$(dirname "$APK")"
echo "== 3/3 export debug APK -> $APK"
"$GODOT_BIN" --headless --path "$GODOT_PROJ" --export-debug "Quest3" "$APK" 2>&1 | tee /tmp/quest_export.log
echo "== built: $(ls -la "$APK")"

if [ "${1:-}" = "install" ]; then
  shift || true
  if [ -n "${ADB_SERIAL:-}" ]; then
    "$ADB" -s "$ADB_SERIAL" install -r "$APK"
  else
    "$ADB" install -r "$APK"
  fi
fi
