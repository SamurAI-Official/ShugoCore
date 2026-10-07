# ShugoCore v1.29.0 — native NRR neural rendering on Android

Upstream NRR's C++ runtime now runs **on-device**: a paired node can execute
`nrr_render` natively instead of answering `not_supported`. The Python `nrr/`
contract layer stays binary-free.

> This release was never published on GitHub when the code landed; it is being
> published now for completeness. Its successor, **v1.29.1**, is the current
> Latest release.

## Vendored NRR C++ runtime (submodule)

- `SamurAI-Official/NRR` added as a git submodule (mirrors the llama.cpp
  pattern); the Android build compiles its C++ runtime + Phase 13 mobile
  sources into a static `nrr_runtime`.
- Built from ShugoCore's own `CMakeLists.txt` rather than upstream's
  `add_subdirectory()` — upstream gates `NRR_HAVE_ONNXRUNTIME` on the Windows
  layout with non-`CACHE` `set()`s, so the ORT path is unreachable on Android
  from outside.
- ONNX Runtime is **required** (the ORT-less placeholder path does not
  compile); configure fails fast with instructions.

## ONNX Runtime from the official Maven AAR

- `scripts/fetch_ort_android.sh` extracts the version-matched C headers and
  per-ABI `libonnxruntime.so` from `onnxruntime-android` (default 1.23.2).
  Gitignored; regenerate with the script. Gradle packages the same `.so` that
  CMake links, so the APK ships exactly what was tested.

## Upstream bugs found (Phase 13 had never been compiled)

- ORT-less path does not compile (`provider_note_` outside its `#ifdef`).
- ORT path was Windows-only; patched to `ORTCHAR_T` semantics.
- `nrr_power_manager.cpp` calls four Android hooks upstream neither declares
  nor defines — supplied by ShugoCore (`nrr_android_platform.cpp`, sysfs
  battery/thermal reads).
- macOS is unbuildable; host CI compiles those TUs with `__APPLE__` undefined.
- **Vendor auto-selection trap:** Adreno outranks CPU and then fails
  detection, killing `nrr_device_create()`; callers must pass an explicit
  `preferred_backend`.

## Durable upstream patches

- Fixes live as an idempotent patch series (`patches/nrr/`,
  `scripts/apply_nrr_patches.sh`) re-applied after
  `git submodule update --init`; the port-wiring test fails if the series or
  the apply script goes missing.

## App-side native layers & reachability

- `nrr_jni.cpp` (`libnrr_jni.so`) and `NRRBridge.kt`, mirroring
  `LlamaCppBridge`.
- `nrr/adapter.py`: `NRRRenderWorker` + `android_native_worker()` (returns
  `None` when unavailable, so callers keep the fail-closed stub); routing
  advertises `nrr_render` only when the worker can serve it.
- `ShugoCoreService` builds the bridge at startup, registers it with the agent
  (`register_nrr_renderer`), runs a self-test, and serves `GET /nrr/info` +
  `GET|POST /nrr/selftest` via `LocalApiServer`.

## Verified

- In-app self-test on the A51: `768 bytes, 17 distinct, 4.3ms`.
- **Live camera frame**: `320x426 -> 408960 bytes, 88 distinct, 11.0ms` (a real
  image through ONNX Runtime, not a synthetic fill).
- Full device harness after the port: **11/11 phases pass, verdict STABLE**.

## Install

```
pip install shugocore==1.29.0
```
