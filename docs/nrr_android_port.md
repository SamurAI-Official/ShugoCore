# NRR native runtime on Android (port notes)

ShugoCore's `nrr/` Python package is the *contract* layer (descriptors,
scene/motion schema, capability-gated dispatch) and stays binary-free. This
document covers the separate, optional **native** layer: upstream NRR's C++
runtime + Phase 13 mobile sources, compiled into the Android app so a paired
node can actually execute `nrr_render` instead of answering `not_supported`.

Upstream: <https://github.com/SamurAI-Official/NRR> (MIT), vendored as a git
submodule at `platforms/android/app/src/main/cpp/nrr`.

## What upstream Phase 13 actually contains

| Piece | State |
|---|---|
| `runtime/mobile/mobile_kernel.{h,cpp}` | Real. Owns one ONNX Runtime session; `execute_frame()` does RGBA8 → NCHW → ONNX → RGB8 → output texture, with a passthrough fallback. |
| `runtime/mobile/backend_{adreno,mali,pwrvr,apple,android_vulkan,xenos,radeon_mob}.{h,cpp}` | `nrr::Backend` shells: GPU detect (Vulkan enumeration only when `NRR_ENABLE_VULKAN`), capability report, and `execute_model()` → `kernel->execute_frame()`. Texture upload/download are no-op `memcpy`. |
| `runtime/mobile/nrr_power_manager.{h,cpp}` | Real, platform-agnostic battery/thermal → resolution scale + power profiles. |
| `runtime/platform/android/nrr_android.{h,cpp}` | Mostly placeholders (no-op `NRR_SUCCESS`) and missing the power-manager hooks entirely. |
| Adreno/Mali GPU compute inference | **Not implemented upstream** (Vulkan compute shaders are an open `[ ]` item). |

So the port buys real mobile ONNX inference on the CPU/XNNPACK execution
provider, plus the vendor-backend registry and power manager — not GPU
compute neural rendering.

## Why we own the CMake build

`platforms/android/app/src/main/cpp/CMakeLists.txt` compiles the NRR sources
directly rather than `add_subdirectory(cpp/nrr)`:

- Upstream's ONNX Runtime detection gates `NRR_HAVE_ONNXRUNTIME` on
  `lib/onnxruntime.lib` **and** `lib/onnxruntime.dll` (the Windows
  `fetch_ort.ps1` layout). Both `set()`s are non-`CACHE`, so the value cannot
  be injected from outside — the ORT-enabled path is unreachable on Android.
- Upstream's ORT-*less* fallback does not compile at all (bug 1 below).

The source list mirrors upstream's `NRR_RUNTIME_SOURCES` plus the Phase 13
mobile sources, so an upstream sync is a diff against one list.

## ONNX Runtime

Extracted from the official Maven AAR by `scripts/fetch_ort_android.sh` into
`platforms/android/app/src/main/cpp/onnxruntime/` (gitignored):

- `include/` — the AAR's own headers (`onnxruntime_c_api.h`,
  `onnxruntime_ep_c_api.h`, `nnapi_provider_factory.h`, ...), so the API
  version always matches the `.so`.
- `lib/<abi>/libonnxruntime.so` — ~19 MB (arm64-v8a), ~22 MB (x86_64).

Gradle packages that directory via `jniLibs.srcDirs`, so the file CMake links
and the file shipped in the APK are the same one. ORT is **required**: the
runtime refuses to configure without it, because the ORT-less path (bug 1)
cannot build. Set `-DSHUGOCORE_NRR=OFF` to build without NRR entirely.
## Upstream patches (durable)

The parent repository records only the submodule's commit SHA, so edits made
inside the submodule would be lost on a fresh clone.  The four required fixes
therefore also live in `patches/nrr/` and are re-applied by:

```
git submodule update --init --recursive
scripts/apply_nrr_patches.sh          # idempotent
```

Pinned upstream commit: `6c977e24fab4c395d4defa20755bb998c4a40ae1`.
`tests/test_nrr_android_port.py` fails if the patch series or the apply
script goes missing, and if the applied patches stop being present.

## The app-side layers

| Layer | Purpose |
|---|---|
| `nrr_jni.cpp` | JNI bindings (`libnrr_jni.so`): create/destroy session, `renderFrame`, capabilities, power status. Same handle-passing and `__ANDROID__`-guard conventions as `llama_jni.cpp`. |
| `NRRBridge.kt` | Kotlin wrapper in `.../inference/`, mirroring `LlamaCppBridge`. Loads `nrr_jni`, exposes `render()` / `capabilities()` / `powerStatus()`. |
| `nrr/adapter.py::NRRRenderWorker` | Peripheral worker: the step up from `worker_stub`. Injected `frame_source` + `renderer`, so it is device-free and unit-tested. |
| `nrr/adapter.py::android_native_worker` | Factory that binds `NRRRenderWorker` to `NRRBridge` through Chaquopy. Returns `None` when unavailable, so callers keep the fail-closed stub. |

`NRRRenderWorker.compute_caps()` advertises `nrr_render` **only** when the
worker can actually serve it, so the primary's `nodes_for_workload()` routing
never sends pixel work to a node that would answer `not_supported`.

## Upstream bugs found while porting

Verified against `main` (NRR `1.0.0-dev`); Phase 13 had never been compiled.

1. **The ORT-less path does not compile.** `runtime/onnx_runtime.cpp` calls
   `provider_note_.clear()` unconditionally, but `provider_note_` is declared
   in `onnx_runtime.h` only inside `#ifdef NRR_HAVE_ONNXRUNTIME`. The README
   claim that "without it the build falls back to a placeholder path and
   everything still compiles" is incorrect.
2. **The ORT path is Windows-only.** The same file hard-codes
   `#include <windows.h>` + `MultiByteToWideChar` for the model path inside
   the `NRR_HAVE_ONNXRUNTIME` block. ORT's `CreateSession` takes `ORTCHAR_T*`,
   which is `wchar_t` only on Windows. Patched here to select the wide/narrow
   path via `ORTCHAR_T` so Android builds.
3. **The Android power-manager hooks exist nowhere.**
   `nrr_power_manager.cpp` calls `android_get_battery_level()`,
   `android_get_battery_status()`, `android_get_thermal_headroom()` and
   `android_is_low_power()` from `namespace nrr::mobile`, but
   `platform/android/nrr_android.h` declares unrelated functions in
   `nrr::android` and defines none of them: Android has therefore never
   compiled. Filled in by ShugoCore in `nrr_android_platform.cpp` (sysfs
   battery/thermal reads) with the declarations added to the upstream header.
4. **macOS does not build.** `runtime/mobile/backend_apple.cpp` is guarded only
   by `#if TARGET_OS_IOS || TARGET_OS_OSX`, so it pulls Objective-C framework
   headers into a `.cpp`; and `backend_apple.h` declares neither
   `backend_apple_is_supported()` nor `backend_apple_create()` although
   `backend_registry.cpp` references both under `#ifdef __APPLE__`. Host CI
   compiles those two TUs with `__APPLE__` undefined, reproducing the
   Windows/Linux behavior upstream actually supports.
5. **Vendor backend auto-selection is a trap.** `is_supported()` returns `true`
   for every mobile vendor backend without probing for that vendor's GPU, and
   priorities are Adreno 60 > Mali 55 > Vulkan 50 > CPU 10. Auto-selection
   therefore resolves to Adreno on *every* device, whose `initialize()` then
   fails GPU detection on non-Qualcomm silicon — taking
   `nrr_device_create()` down with it. **Always pass an explicit
   `NRRDeviceOptions.preferred_backend`**, which `select_best_backend()`
   honours deterministically.

## Validation

`nrr_probe` is an end-to-end diagnostic (create device → load model → RGBA8
texture → upload → `execute_frame` → download) printing a JSON summary.

Cross-compile and run on a device without Gradle:

```
NDK=$HOME/Library/Android/sdk/ndk/27.0.12077973
cmake -S platforms/android/app/src/main/cpp -B build-nrr \
  -DCMAKE_TOOLCHAIN_FILE=$NDK/build/cmake/android.toolchain.cmake \
  -DANDROID_ABI=arm64-v8a -DANDROID_PLATFORM=android-28 \
  -DCMAKE_BUILD_TYPE=Release
cmake --build build-nrr --target nrr_probe

adb push build-nrr/nrr_probe /data/local/tmp/
adb push platforms/android/app/src/main/cpp/onnxruntime/lib/arm64-v8a/libonnxruntime.so /data/local/tmp/
adb push platforms/android/app/src/main/cpp/nrr/models/nrr_upscaler_v0.1.onnx /data/local/tmp/
adb shell 'cd /data/local/tmp && LD_LIBRARY_PATH=/data/local/tmp ./nrr_probe \
  --model /data/local/tmp/nrr_upscaler_v0.1.onnx'
```

Expected (verified on a Galaxy A51, Exynos 1380, arm64-v8a):

```json
{ "onnxruntime": true, "active_backend": "CPU", "device_created": true,
  "model_loaded": true,
  "kernel": { "initialized": true, "loaded": true, "active_ep": "CPU",
              "supports_nnapi": false, "supports_fp16": true },
  "execute_frame": true, "output_texture": true,
  "output_distinct_bytes": 17, "failures": 0, "ok": true }
```

Capabilities are reported honestly: `active_ep` is `CPU` and
`neural_acceleration` is `absent`, because upstream's `apply_provider()` only
understands `cpu`/`cuda`/`directml`. `mobile_kernel.cpp` requests `"nnapi"`
and then sets `supports_nnapi = true`, but the session silently runs on the
default CPU provider — so ShugoCore must not advertise NNAPI until the EP is
genuinely appended (see below).

## Next (not in this phase)

- `nrr_jni` shared library + `NRRBridge.kt`, routing `runInference("nrr_render")`
  from `android_node.py` instead of the Python `worker_stub`.
- Ship the ONNX model in `assets/` and resolve it via `AAssetManager`.
- Advertise `compute_caps.workloads: ["nrr_render"]` sourced from the real
  capabilities (CPU EP only) so `nodes_for_workload()` routing is truthful.
- Optionally wire NNAPI: `apply_provider()` needs an `OrtSessionOptionsAppend-
  ExecutionProvider_Nnapi` arm (the AAR ships `nnapi_provider_factory.h`).
  Until then `supports_nnapi` must stay `false`.
- Report bugs 1-4 upstream.

