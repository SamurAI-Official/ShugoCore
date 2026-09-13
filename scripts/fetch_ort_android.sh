#!/usr/bin/env bash
# fetch_ort_android.sh — extract ONNX Runtime for Android out of the official
# Maven AAR into platforms/android/app/src/main/cpp/onnxruntime/.
#
# Why the AAR and not a GitHub release: microsoft/onnxruntime publishes no
# Android assets on GitHub releases.  The Maven artifact is the supported
# distribution and it conveniently ships BOTH the version-matched C headers
# (headers/) and the per-ABI shared libraries (jni/<abi>/libonnxruntime.so),
# so we never risk an API/ABI mismatch between them.
#
# Extracted layout (gitignored; regenerate with this script):
#   onnxruntime/include/onnxruntime_c_api.h ...
#   onnxruntime/lib/<abi>/libonnxruntime.so
#
# Upstream NRR's own CMake looks for a Windows layout (lib/onnxruntime.lib +
# lib/onnxruntime.dll) and its ORT-less "placeholder" path does not actually
# compile (runtime/onnx_runtime.cpp uses provider_note_, which the header
# only declares under NRR_HAVE_ONNXRUNTIME).  ONNX Runtime is therefore
# REQUIRED for the Android NRR runtime.
#
# Usage:
#   scripts/fetch_ort_android.sh [VERSION] [ABI...]
#   scripts/fetch_ort_android.sh 1.23.2 arm64-v8a x86_64

set -euo pipefail

VERSION="${1:-1.23.2}"
shift || true
if [ "$#" -gt 0 ]; then
    ABIS=("$@")
else
    ABIS=(arm64-v8a x86_64)   # matches build.gradle abiFilters
fi

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(dirname "$HERE")"
DEST="$ROOT/platforms/android/app/src/main/cpp/onnxruntime"

BASE="https://repo1.maven.org/maven2/com/microsoft/onnxruntime/onnxruntime-android"
AAR="onnxruntime-android-$VERSION.aar"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

echo "==> downloading $AAR"
curl -fsSL -o "$TMP/$AAR" "$BASE/$VERSION/$AAR"

echo "==> extracting headers -> $DEST/include"
mkdir -p "$DEST/include"
unzip -o -q "$TMP/$AAR" 'headers/*' -d "$TMP"
cp -f "$TMP"/headers/*.h "$DEST/include/"

for abi in "${ABIS[@]}"; do
    echo "==> extracting $abi -> $DEST/lib/$abi"
    mkdir -p "$DEST/lib/$abi"
    unzip -o -q "$TMP/$AAR" "jni/$abi/libonnxruntime.so" -d "$TMP"
    cp -f "$TMP/jni/$abi/libonnxruntime.so" "$DEST/lib/$abi/"
done

echo "==> done"
ls -la "$DEST/include" | head -5
du -sh "$DEST/lib"/* 2>/dev/null || true
