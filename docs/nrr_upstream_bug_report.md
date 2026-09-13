# Upstream NRR issues found while porting Phase 13 to Android

Ready-to-file report against
[SamurAI-Official/NRR](https://github.com/SamurAI-Official/NRR) at commit
`6c977e24fab4c395d4defa20755bb998c4a40ae1`
("feat: real mobile ONNX execution kernels (MobileExecutionKernel)").

All six were found while building the runtime for Android in ShugoCore. The
short version: the Phase 13 checklist marks most items complete, but the
Android path had never actually been compiled, so several of them cannot build
anywhere except Windows.

Local fixes live in `patches/nrr/` in the ShugoCore repository
(`scripts/apply_nrr_patches.sh` re-applies them); each item below says whether
ShugoCore works around it or is still blocked by it.

---

## 1. The ONNX-Runtime-less build does not compile (README says it does)

**Severity:** build breaker (no-ORT configuration)
**Files:** `runtime/onnx_runtime.cpp:511`, `runtime/onnx_runtime.h:99`

`ONNXRuntime::set_execution_provider()` calls `provider_note_.clear()`
unconditionally, but `provider_note_` is declared in the header only inside
`#ifdef NRR_HAVE_ONNXRUNTIME`.

README: *"Without the SDK the build still succeeds using the placeholder path
... everything still compiles and passes tests."* It does not.

```
runtime/onnx_runtime.cpp:511:5: error: use of undeclared identifier 'provider_note_'
```

**Repro:** configure without ONNX Runtime (`-DNRR_ONNXRUNTIME_ROOT=` unset and
no `third_party/onnxruntime-*`) and build `nrr_static`.

**Suggested fix:** move `provider_note_` outside the `#ifdef`, or guard the
`clear()` calls.

**ShugoCore impact:** ONNX Runtime is now a hard requirement; configure fails
fast with instructions rather than silently degrading.

---

## 2. The ONNX-Runtime build is Windows-only (ORTCHAR_T mishandled)

**Severity:** build breaker (ORT-enabled configuration, any non-Windows target)
**File:** `runtime/onnx_runtime.cpp:10-22, 152`

Inside `#ifdef NRR_HAVE_ONNXRUNTIME` the file unconditionally does:

```cpp
#define WIN32_LEAN_AND_MEAN
#define NOMINMAX
#include <windows.h>
static std::wstring utf8_to_wide(const std::string& s) { /* MultiByteToWideChar */ }
```

and later passes `wpath.c_str()` (`const wchar_t*`) to `CreateSession`.
ORT's path type is `ORTCHAR_T`: `wchar_t` on Windows, `char` elsewhere.

```
runtime/onnx_runtime.cpp:13:10: fatal error: 'windows.h' file not found    # Android
error: cannot convert 'const wchar_t*' to 'const ORTCHAR_T*' ...          # any non-Windows
```

**Repro:** build with `NRR_HAVE_ONNXRUNTIME` on Android/Linux/macOS.

**Suggested fix:** `#if defined(_WIN32)` the wide helpers and pass
`std::string::c_str()` elsewhere (see ShugoCore's patch for the shape).

**ShugoCore impact:** patched; this is why the AAR-extracted `.so` works.
---

## 3. `backend_apple.h` lacks declarations that `backend_registry.cpp` uses

**Severity:** build breaker on macOS
**Files:** `runtime/mobile/backend_apple.h`, `runtime/backend_registry.cpp:123`

`backend_registry.cpp` references `backend_apple_is_supported` and
`backend_apple_create` under `#ifdef __APPLE__`, but `backend_apple.h`
declares neither (compare `backend_adreno.h`, which declares both as
`extern`).

```
runtime/backend_registry.cpp:123:13: error: use of undeclared identifier 'backend_apple_is_supported'
```

**Suggested fix:** add the two `extern` declarations to `backend_apple.h`, as
`backend_adreno.h`/`backend_mali.h` already do.

**ShugoCore impact:** host CI compiles that TU with `__APPLE__` undefined.

---

## 4. `backend_apple.cpp` pulls Objective-C headers into a `.cpp`

**Severity:** build breaker on macOS
**File:** `runtime/mobile/backend_apple.cpp:15-21`

```cpp
#ifdef __APPLE__
#include <TargetConditionals.h>
#if TARGET_OS_IOS || TARGET_OS_OSX
#include <Metal/Metal.h>
#include <CoreML/CoreML.h>
#endif
#endif
```

`backend_apple.cpp` is a `.cpp`, so on macOS (`TARGET_OS_OSX` is 1) the
Objective-C framework headers are parsed as C++:

```
NSZone.h:9:1: error: expected unqualified-id
  @class NSString;
```

**Suggested fix:** compile it as Objective-C++ (upstream already sets
`LANGUAGE CXX` for `nrr_ios.mm`), or split the Metal/CoreML calls into a `.mm`
and keep the `.cpp` free of framework headers.

**ShugoCore impact:** same `-U__APPLE__` workaround as item 3 for host CI.

---

## 5. Android power-manager hooks are declared nowhere and defined nowhere

**Severity:** build breaker on Android (Phase 13 never compiled)
**Files:** `runtime/mobile/nrr_power_manager.cpp:95-104`,
`runtime/platform/android/nrr_android.h`,
`runtime/platform/android/nrr_android.cpp`

Under `#ifdef __ANDROID__`, `power_manager_get_status()` calls four functions
from inside `namespace nrr::mobile`:

```cpp
status->battery_level    = android_get_battery_level();
status->charging         = (android_get_battery_status() == 1) ? 1 : 0;
status->thermal_headroom = android_get_thermal_headroom();
status->low_power_mode   = android_is_low_power();
```

None of the four is declared or defined. `nrr_android.h` declares unrelated
functions (`android_get_thermal_status`, `android_get_available_memory`,
`android_log`, ...) and those are in `nrr::android`, a namespace the call sites
do not resolve into.

```
nrr_power_manager.cpp:97:34: error: use of undeclared identifier 'android_get_battery_level'
```

**Suggested fix:** declare them (in `nrr::mobile`, matching the call sites) in
`nrr_android.h` and implement them in `nrr_android.cpp` — the file that is
supposed to be the Android NDK bridge. Sysfs is sufficient for battery
(`/sys/class/power_supply/*/capacity`, `/status`) and thermal
(`/sys/class/thermal/thermal_zone*/temp`).

**ShugoCore impact:** supplied in `nrr_android_platform.cpp`.

---

## 6. The power-manager C API is defined in a namespace while its own header declares it `extern "C"`

**Severity:** ABI mismatch (latent)
**Files:** `runtime/mobile/nrr_power_manager.h:13`,
`runtime/mobile/nrr_power_manager.cpp:130-149`

The header wraps the declarations in a global `extern "C"` block, but the
definitions sit inside `namespace nrr`, so the symbols are C++-mangled
(`nrr::nrr_power_manager_init`). Upstream's own tests link only because they
call the functions unqualified from `nrr::test`, where enclosing-namespace
lookup finds them. Any plain-C consumer (the actual point of a C API) will
fail to link.

**Suggested fix:** close `namespace nrr` before the wrappers and add
`extern "C"` to match the header.

**ShugoCore impact:** patched; our JNI bridge is a C++ consumer so it hit this
immediately.

---

## Also worth a look (not build breakers)

### A. Vendor backend auto-selection picks a backend that cannot initialize

`BackendAdreno::is_supported()` (and Mali/PowerVR/Apple/Xenos/Radeon-mobile)
returns `true` unconditionally under `NRR_ENABLE_MOBILE_VENDOR`, without
probing for that vendor's GPU. `backend_priorities[]` ranks Adreno 60 >
Mali 55 > Vulkan 50 > CPU 10, so `select_best_backend()` resolves to Adreno on
**every** device; `BackendAdreno::initialize()` then calls
`detect_adreno_gpu()`, which fails on non-Qualcomm silicon and returns
`NRR_ERROR_BACKEND_UNAVAILABLE` — taking `nrr_device_create()` down with it.

This is masked in the default mobile configuration (Vulkan on, Adreno GPU) and
in upstream's own Windows test runs (vendor backends off). On an Exynos/Mali
device with `NRR_ENABLE_MOBILE_VENDOR=1` and no Vulkan, device creation simply
fails.

**Suggested fix:** make `is_supported()` actually probe (Vulkan enumeration
and/or an EGL/`ro.hardware`-style vendor check), or stop ranking an
unverifiable vendor backend above CPU.

**ShugoCore impact:** we always pass an explicit
`NRRDeviceOptions.preferred_backend`; the trap is documented in
`docs/nrr_android_port.md`.

### B. `runtime/platform/android/nrr_android.cpp` is mostly a no-op

`detect_android_device_capabilities()` returns a hardcoded capability block
regardless of hardware, and `load_model_from_assets()` /
`bind_android_surface()` are `(void)`-cast stubs returning `NRR_SUCCESS`.
Callers cannot distinguish "did nothing" from "succeeded", which is the
opposite of how the rest of the runtime reports. Either implement them
(`AAssetManager`, `ANativeWindow`, `AHardwareBuffer`) or return
`NRR_ERROR_NOT_IMPLEMENTED`.

### C. `mobile_kernel` reports NNAPI while running the CPU provider

`MobileExecutionKernel::select_best_execution_provider()` sets
`active_ep_name_ = "NNAPI"` and `mobile_caps_.supports_nnapi = true` for
`MobileEP::NNAPI`, and calls `onnx_->set_execution_provider("nnapi")`. But
`ONNXRuntime::apply_provider()` only recognises `cpu`/`cuda`/`directml`, so
nothing is appended and the session runs on the default CPU provider. The
capability report therefore claims acceleration that is not happening.

**Suggested fix:** either add the NNAPI EP (`OrtApi::SessionOptionsAppend-
ExecutionProvider_Nnapi`, or the EP-API form) or leave `supports_nnapi` false
so callers are not misled. On Android the AAR ships
`nnapi_provider_factory.h`.

**ShugoCore impact:** capability advertisement is derived from
`execution_provider` rather than from the vendor backend's wish list, so the
node advertises `nrr_render` with `neural_acceleration: absent` — the honest
answer (see `docs/nrr_android_port.md`).

