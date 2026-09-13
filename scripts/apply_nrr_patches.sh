#!/usr/bin/env bash
# apply_nrr_patches.sh — apply ShugoCore's local fixes to the NRR submodule.
#
# The NRR submodule carries four small fixes without which the Android build
# cannot work (see docs/nrr_android_port.md). Because the parent repository
# only records the submodule's commit SHA, edits made inside the submodule
# would be lost on a fresh clone -- so they live here as a patch series that
# is re-applied after `git submodule update --init`.
#
# Pinned upstream commit: 6c977e24fab4c395d4defa20755bb998c4a40ae1
# ("feat: real mobile ONNX execution kernels (MobileExecutionKernel)")
#
# Idempotent: safe to run repeatedly; already-applied patches are skipped.
#
# Usage: scripts/apply_nrr_patches.sh

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(dirname "$HERE")"
SUBMODULE="$ROOT/platforms/android/app/src/main/cpp/nrr"
PATCHES="$ROOT/patches/nrr"

if [ ! -d "$SUBMODULE/.git" ] && [ ! -f "$SUBMODULE/.git" ]; then
    echo "error: NRR submodule not initialized at $SUBMODULE" >&2
    echo "       run: git submodule update --init --recursive" >&2
    exit 1
fi

if [ ! -d "$PATCHES" ]; then
    echo "error: no patch directory at $PATCHES" >&2
    exit 1
fi

cd "$SUBMODULE"
applied=0
for patch in "$PATCHES"/*.patch; do
    [ -e "$patch" ] || continue
    name="$(basename "$patch")"
    if git apply --reverse --check "$patch" >/dev/null 2>&1; then
        echo "==> $name already applied"
        continue
    fi
    if git apply --check "$patch" >/dev/null 2>&1; then
        git apply "$patch"
        echo "==> applied $name"
        applied=$((applied + 1))
    else
        echo "error: $name does not apply cleanly (upstream moved?)" >&2
        echo "       Inspect with: git -C $SUBMODULE apply --check -v $patch" >&2
        exit 1
    fi
done

echo "==> done ($applied applied)"
