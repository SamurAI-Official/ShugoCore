# CMakeLists.txt — builds libllama_jni.so (llama.cpp JNI bindings) for Android.
#
# The llama.cpp submodule must be initialized first:
#   git submodule update --init --recursive
#
# GPU offload (Vulkan, Adreno/Mali) can be enabled from gradle or CLI:
#   cmake -DSHUGOCORE_VULKAN=ON ...

cmake_minimum_required(VERSION 3.18.0)
project(shugocore LANGUAGES C CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

set(LLAMA_CPP_DIR ${CMAKE_CURRENT_SOURCE_DIR}/llama.cpp)
if(NOT EXISTS "${LLAMA_CPP_DIR}/include/llama.h")
    message(FATAL_ERROR
        "llama.cpp submodule not found. Run: git submodule update --init --recursive")
endif()

# ---------------------------------------------------------------------------
# Options
# ---------------------------------------------------------------------------
option(SHUGOCORE_VULKAN "Enable Vulkan GPU offload (Adreno/Mali)" OFF)

# Defensive: Gradle's debug variant passes -DCMAKE_BUILD_TYPE=Debug, which
# would compile ggml at -O0. Inference is unusable unoptimized, so even the
# debug APK must ship an optimized engine (build.gradle also passes Release).
if(ANDROID AND CMAKE_BUILD_TYPE MATCHES "^[Dd][Ee][Bb][Uu][Gg]$")
    set(CMAKE_BUILD_TYPE Release CACHE STRING "" FORCE)
endif()

# ---------------------------------------------------------------------------
# On-device CPU kernels: ONE APK, runtime-selected variant set (arm64-v8a)
# ---------------------------------------------------------------------------
# ggml can build each arm64 kernel set into its own backend library
# (libggml-cpu-android_armv8.0_1.so, ..._armv8.2_2.so, ...). Every one exports
# ggml_backend_score(), which returns 0 when the running CPU lacks the features
# that variant was compiled for (detected via getauxval(AT_HWCAP/AT_HWCAP2)),
# and ggml_backend_load_best("cpu") dlopens them all and loads the highest
# scorer. So a single APK ships the portable armv8.0 kernels *and* the
# dotprod / i8mm / sve ones, and each device picks its own at startup.
#
# Why this exists: forcing -march=armv8.2-a+dotprod into the *baseline* backend
# compiled dotprod instructions into kernels that are not runtime gated, so any
# arm64 SoC without FEAT_DotProd took SIGILL (observed on the Exynos 9611 test
# unit, whose /proc/cpuinfo has no `asimddp`):
#   Fatal signal 4 (SIGILL), code 1 (ILL_ILLOPC)
#   #00 libllama_jni.so (ggml_vec_dot_q5_0_q8_0+236)
# Shipping only the portable baseline avoids the crash but gives up dotprod on
# capable SoCs; ALL_VARIANTS removes that trade-off.
#
# GGML_CPU_ALL_VARIANTS requires GGML_BACKEND_DL, which requires
# BUILD_SHARED_LIBS, and it is mutually exclusive with GGML_CPU_ARM_ARCH.
# x86_64 stays single-variant on purpose: it is an emulator-only ABI and
# ALL_VARIANTS would add ~13 more backends for no benefit.
#
# Escape hatch to build the old single-library layout:
#   ./gradlew assembleDebug -Pshugocore.singlearch=true
option(SHUGOCORE_CPU_VARIANTS
       "Ship every runtime-selectable arm64 CPU kernel variant in one APK" ON)
option(SHUGOCORE_ARM_DOTPROD
       "Single-arch builds only: compile the baseline for armv8.2-a+dotprod (SIGILL on CPUs without FEAT_DotProd)" OFF)
option(SHUGOCORE_RPC_SERVER
       "Build the mesh RPC peripheral server (layer-split offload; arm64-v8a only)" ON)

if(ANDROID AND SHUGOCORE_CPU_VARIANTS)
    # ggml dlopens its backend libraries, so they cannot be static.
    set(BUILD_SHARED_LIBS ON CACHE BOOL "" FORCE)
    set(GGML_BACKEND_DL ON CACHE BOOL "" FORCE)
    if(ANDROID_ABI STREQUAL "arm64-v8a")
        set(GGML_CPU_ALL_VARIANTS ON CACHE BOOL "" FORCE)
    endif()
elseif(ANDROID AND ANDROID_ABI STREQUAL "arm64-v8a" AND SHUGOCORE_ARM_DOTPROD)
    set(GGML_CPU_ARM_ARCH "armv8.2-a+dotprod" CACHE STRING "" FORCE)
endif()

# Android 15+ 16 KB page compatibility applies to every shared object we ship,
# not just llama_jni (the ggml backend variants are shared modules as well).
if(ANDROID)
    add_link_options(-Wl,-z,max-page-size=16384)
endif()

if(SHUGOCORE_VULKAN)
    set(GGML_VULKAN ON CACHE BOOL "" FORCE)
endif()

# Keep the llama.cpp build minimal: only the library targets are needed.
set(LLAMA_BUILD_EXAMPLES OFF CACHE BOOL "" FORCE)
set(LLAMA_BUILD_TESTS    OFF CACHE BOOL "" FORCE)
set(LLAMA_BUILD_SERVER   OFF CACHE BOOL "" FORCE)
set(LLAMA_BUILD_TOOLS    OFF CACHE BOOL "" FORCE)
# BUILD_SHARED_LIBS is decided by the CPU-variant block above: GGML_BACKEND_DL
# requires shared libraries, so the historical static single-library layout is
# only used when SHUGOCORE_CPU_VARIANTS is off (the singlearch escape hatch).
set(BUILD_SHARED_LIBS ${BUILD_SHARED_LIBS} CACHE BOOL "" FORCE)

# Mesh RPC peripheral (Option 2 layer split): this device becomes an offload
# target for a host's `llama-server --rpc`, contributing RAM and compute.
#
# ggml's RPC backend is OFF by default, and llama.cpp only adds tools/rpc when
# LLAMA_BUILD_TOOLS is ON (which would drag in every other tool), so the one
# executable we need is built here directly. It links only `ggml`: the RPC
# backend is a dlopen'd module discovered at runtime from the executable's own
# directory, which is why the binary must be packaged beside its libggml*.so
# siblings. arm64-v8a only — the x86_64 ABI exists for the emulator, which has
# no role as a peripheral.
set(SHUGOCORE_BUILD_RPC OFF)
if(ANDROID AND SHUGOCORE_RPC_SERVER AND ANDROID_ABI STREQUAL "arm64-v8a")
    set(SHUGOCORE_BUILD_RPC ON)
    set(GGML_RPC ON CACHE BOOL "" FORCE)
endif()

add_subdirectory(${LLAMA_CPP_DIR} llama_build EXCLUDE_FROM_ALL)

if(SHUGOCORE_BUILD_RPC)
    add_executable(shugocore_rpc_server
                   ${LLAMA_CPP_DIR}/tools/rpc/rpc-server.cpp)
    target_include_directories(shugocore_rpc_server PRIVATE
                               ${LLAMA_CPP_DIR}/ggml/src)
    target_link_libraries(shugocore_rpc_server PRIVATE ggml)

    # Ship it as `lib*.so` so Gradle packages it like a native library: with
    # useLegacyPackaging=true the installer extracts it to nativeLibraryDir as a
    # real file. That is the only place an app may exec a helper binary on
    # modern Android (exec of app-writable data dirs is blocked), and
    # nativeLibraryDir is also where its libggml*.so siblings live.
    set_target_properties(shugocore_rpc_server PROPERTIES
        PREFIX "lib" SUFFIX ".so"
        RUNTIME_OUTPUT_DIRECTORY
            "${CMAKE_CURRENT_SOURCE_DIR}/../jniLibs/${ANDROID_ABI}"
        LIBRARY_OUTPUT_DIRECTORY
            "${CMAKE_CURRENT_SOURCE_DIR}/../jniLibs/${ANDROID_ABI}")
endif()

add_library(llama_jni SHARED llama_jni.cpp)

target_include_directories(llama_jni PRIVATE
    ${LLAMA_CPP_DIR}/include
    ${LLAMA_CPP_DIR}/ggml/include
)

# Android 15+ 16KB page size compat: the ELF's LOAD segments must be
# aligned to 16384 bytes or the dynamic linker refuses to map the library
# with "ELF alignment failed". -z max-page-size tells the linker to lay
# out the binary for the larger page granularity.
if(ANDROID)
    target_link_options(llama_jni PRIVATE -Wl,-z,max-page-size=16384)
endif()

if(ANDROID)
    # Android NDK build: log lives in the platform libs.
    find_library(log-lib log)
    target_link_libraries(llama_jni PRIVATE llama ${log-lib})
else()
    # Host build (CI validation): Android's liblog does not exist and jni.h
    # must be supplied explicitly, e.g.
    #   cmake -DSHUGOCORE_JNI_INCLUDE=/tmp/jni_include ...
    set(SHUGOCORE_JNI_INCLUDE "" CACHE PATH "Directory containing jni.h (host builds only)")
    if(NOT EXISTS "${SHUGOCORE_JNI_INCLUDE}/jni.h")
        message(FATAL_ERROR
            "Host build requires SHUGOCORE_JNI_INCLUDE pointing at a directory with jni.h")
    endif()
    target_include_directories(llama_jni PRIVATE ${SHUGOCORE_JNI_INCLUDE})
    target_link_libraries(llama_jni PRIVATE llama)
endif()

# NEON is implicit on arm64-v8a. Never use -march=native with the NDK
# (it disables the portable baseline and breaks x86_64 emulator builds).
if(ANDROID_ABI STREQUAL "armeabi-v7a")
    target_compile_options(llama_jni PRIVATE
        -mfpu=neon-vfpv4 -mfloat-abi=softfp)
endif()

# ===========================================================================
# NRR (Neural Rendering Runtime) — native Android runtime
# ===========================================================================
#
# Vendored as a submodule (cpp/nrr) but compiled from *here* rather than via
# add_subdirectory(), for one hard reason: upstream's CMake ONNX Runtime
# detection requires lib/onnxruntime.lib AND lib/onnxruntime.dll (the
# Windows/fetch_ort.ps1 layout) and those set()s are not CACHE, so
# NRR_HAVE_ONNXRUNTIME can never be enabled for Android from outside.  We
# therefore own the ORT wiring; the source list below mirrors upstream's
# NRR_RUNTIME_SOURCES (runtime/*) plus the Phase 13 mobile sources.
#
# Builds a STATIC lib only: the SHARED nrr_jni bridge that consumes it is
# added separately, so a runtime port failure can never break llama_jni.

option(SHUGOCORE_NRR "Build the NRR native runtime (static)" ON)
option(SHUGOCORE_NRR_PROBE "Build the host nrr_probe diagnostic (non-Android only)" ON)

set(NRR_DIR ${CMAKE_CURRENT_SOURCE_DIR}/nrr)
set(NRR_ORT_DIR ${CMAKE_CURRENT_SOURCE_DIR}/onnxruntime)

if(SHUGOCORE_NRR)
    if(NOT EXISTS "${NRR_DIR}/include/nrr.h")
        message(FATAL_ERROR
            "NRR submodule not found at ${NRR_DIR}. Run: "
            "git submodule update --init --recursive")
    endif()
    set(NRR_SOURCES
        # Core runtime (upstream runtime/NRR_RUNTIME_SOURCES)
        ${NRR_DIR}/runtime/nrr_c_api.cpp
        ${NRR_DIR}/runtime/nrr_device.cpp
        ${NRR_DIR}/runtime/nrr_model.cpp
        ${NRR_DIR}/runtime/nrr_reference.cpp
        ${NRR_DIR}/runtime/nrr_reference_impl.cpp
        ${NRR_DIR}/runtime/nrr_temporal.cpp
        ${NRR_DIR}/runtime/nrr_conditioning.cpp
        ${NRR_DIR}/runtime/nrr_inference.cpp
        ${NRR_DIR}/runtime/backend_registry.cpp
        ${NRR_DIR}/runtime/backend_cpu.cpp
        ${NRR_DIR}/runtime/onnx_runtime.cpp
        # Phase 13: mobile execution kernel + vendor backends + power manager
        ${NRR_DIR}/runtime/mobile/mobile_kernel.cpp
        ${NRR_DIR}/runtime/mobile/backend_adreno.cpp
        ${NRR_DIR}/runtime/mobile/backend_mali.cpp
        ${NRR_DIR}/runtime/mobile/backend_pwrvr.cpp
        ${NRR_DIR}/runtime/mobile/backend_apple.cpp
        ${NRR_DIR}/runtime/mobile/backend_android_vulkan.cpp
        ${NRR_DIR}/runtime/mobile/backend_xenos.cpp
        ${NRR_DIR}/runtime/mobile/backend_radeon_mob.cpp
        ${NRR_DIR}/runtime/mobile/nrr_power_manager.cpp
    )

    if(SHUGOCORE_VULKAN)
        list(APPEND NRR_SOURCES ${NRR_DIR}/runtime/backend_vulkan.cpp)
    endif()

    # Upstream gaps that break a macOS host build (Android is unaffected --
    # neither __APPLE__ nor these code paths exist there):
    #   * runtime/mobile/backend_apple.cpp is a Metal/CoreML source guarded
    #     only by `#if TARGET_OS_IOS || TARGET_OS_OSX`, so on macOS it pulls
    #     Objective-C framework headers into a .cpp.
    #   * backend_apple.h declares neither backend_apple_is_supported() nor
    #     backend_apple_create(), yet backend_registry.cpp references both
    #     under `#ifdef __APPLE__`.
    # Compiling those two TUs with __APPLE__ undefined reproduces exactly the
    # Windows/Linux behavior upstream actually supports (the Apple backend
    # degrades to BACKEND_UNAVAILABLE).  Documented as a known upstream gap.
    if(NOT ANDROID AND APPLE)
        set_source_files_properties(
            ${NRR_DIR}/runtime/mobile/backend_apple.cpp
            ${NRR_DIR}/runtime/backend_registry.cpp
            PROPERTIES COMPILE_OPTIONS "-U__APPLE__")
    endif()

    # The Android NDK platform bridge needs <jni.h>/<android/*>: Android only.
    if(ANDROID)
        list(APPEND NRR_SOURCES ${NRR_DIR}/runtime/platform/android/nrr_android.cpp)
        # App-supplied implementation of the power-manager platform hooks that
        # upstream declares nowhere and defines nowhere (Phase 13 never
        # compiled on Android).  Shugocore-owned, not part of the submodule.
        list(APPEND NRR_SOURCES ${CMAKE_CURRENT_SOURCE_DIR}/nrr_android_platform.cpp)
    endif()

    add_library(nrr_runtime STATIC ${NRR_SOURCES})

    # Static lib is linked into a SHARED lib (nrr_jni) -> must be PIC.
    set_target_properties(nrr_runtime PROPERTIES POSITION_INDEPENDENT_CODE ON)

    target_include_directories(nrr_runtime PUBLIC
        ${NRR_DIR}/include
        ${NRR_DIR}/runtime
        ${NRR_DIR}/runtime/mobile
    )

    # NRR_ENABLE_MOBILE_VENDOR makes the vendor backends "active" (their
    # is_supported() returns true), matching upstream's mobile default.
    #
    # TRAP (upstream, verified): is_supported() does NOT probe for the
    # vendor's GPU, and backend priorities are Adreno 60 > Mali 55 >
    # Vulkan 50 > CPU 10.  Under auto-selection every device therefore
    # resolves to Adreno, whose initialize() then fails GPU detection on
    # non-Qualcomm silicon and takes nrr_device_create() down with it.
    # Callers MUST pass an explicit NRRDeviceOptions.preferred_backend
    # (select_best_backend() honours it deterministically); see nrr_probe.cpp.
    if(ANDROID)
        target_compile_definitions(nrr_runtime PRIVATE NRR_ENABLE_MOBILE_VENDOR=1)
    endif()

    if(SHUGOCORE_VULKAN)
        target_compile_definitions(nrr_runtime PRIVATE NRR_ENABLE_VULKAN=1)
    endif()

    # ---- ONNX Runtime (required) ------------------------------------------
    # Android libs + the version-matched C headers are extracted from the
    # official Maven AAR by scripts/fetch_ort_android.sh (both gitignored).
    #
    # ORT is not optional here: upstream's ORT-less "placeholder" path does
    # not compile (runtime/onnx_runtime.cpp uses provider_note_, declared in
    # onnx_runtime.h only under NRR_HAVE_ONNXRUNTIME), so NRR_HAVE_ONNXRUNTIME
    # must always be defined.  See docs/ for the upstream bug write-up.
    if(ANDROID)
        set(NRR_ORT_ABI ${ANDROID_ABI})
    else()
        set(NRR_ORT_ABI ${CMAKE_SYSTEM_NAME}-${CMAKE_SYSTEM_PROCESSOR})
    endif()
    set(NRR_ORT_LIB ${NRR_ORT_DIR}/lib/${NRR_ORT_ABI}/libonnxruntime.so)

    if(NOT EXISTS "${NRR_ORT_DIR}/include/onnxruntime_c_api.h")
        message(FATAL_ERROR
            "ONNX Runtime headers missing at ${NRR_ORT_DIR}/include. Run: "
            "scripts/fetch_ort_android.sh")
    endif()

    target_include_directories(nrr_runtime PUBLIC ${NRR_ORT_DIR}/include)
    target_compile_definitions(nrr_runtime PUBLIC NRR_HAVE_ONNXRUNTIME=1)

    # The .so extracted from the AAR is BOTH linked here and packaged by
    # Gradle (build.gradle adds onnxruntime/lib as a jniLibs source dir), so
    # there is exactly one copy and no Maven dependency to keep in sync.
    if(NOT EXISTS "${NRR_ORT_LIB}")
        message(FATAL_ERROR
            "ONNX Runtime library missing for ABI '${NRR_ORT_ABI}' at "
            "${NRR_ORT_LIB}. Run: scripts/fetch_ort_android.sh")
    endif()
    target_link_libraries(nrr_runtime PUBLIC ${NRR_ORT_LIB})

    if(ANDROID)
        target_link_options(nrr_runtime PUBLIC -Wl,-z,max-page-size=16384)
        find_library(nrr_log-lib log)
        target_link_libraries(nrr_runtime PUBLIC ${nrr_log-lib} android)
    endif()
endif()


# ---------------------------------------------------------------------------
# nrr_probe — end-to-end NRR diagnostic (host CI gate + on-device smoke).
#
# Mirrors upstream tests/unit/test_mobile.cpp: create device -> load model ->
# create RGBA8 texture -> upload -> mobile kernel execute_frame.  Prints a
# JSON summary on stdout so a harness can assert on it.
#
# The device is created with an EXPLICIT preferred_backend (see the TRAP note
# above) so backend selection is deterministic on every SoC.
# ---------------------------------------------------------------------------
if(SHUGOCORE_NRR AND SHUGOCORE_NRR_PROBE)
    add_executable(nrr_probe nrr_probe.cpp)
    target_link_libraries(nrr_probe PRIVATE nrr_runtime)
    if(NOT ANDROID)
        if(NOT EXISTS "${SHUGOCORE_JNI_INCLUDE}/jni.h")
            message(FATAL_ERROR
                "Host build requires SHUGOCORE_JNI_INCLUDE pointing at a directory with jni.h")
        endif()
        target_include_directories(nrr_probe PRIVATE ${SHUGOCORE_JNI_INCLUDE})
    endif()
endif()

# ---------------------------------------------------------------------------
# nrr_jni — the SHARED library the app actually loads (NRRBridge.kt).
# Kept separate from nrr_runtime so a bridge regression cannot break
# llama_jni, and so the runtime stays reusable for host tests.
# ---------------------------------------------------------------------------
if(SHUGOCORE_NRR)
    add_library(nrr_jni SHARED nrr_jni.cpp)
    target_link_libraries(nrr_jni PRIVATE nrr_runtime)

    if(ANDROID)
        target_link_options(nrr_jni PRIVATE -Wl,-z,max-page-size=16384)
        find_library(nrrjni_log-lib log)
        target_link_libraries(nrr_jni PRIVATE ${nrrjni_log-lib})
    else()
        if(NOT EXISTS "${SHUGOCORE_JNI_INCLUDE}/jni.h")
            message(FATAL_ERROR
                "Host build requires SHUGOCORE_JNI_INCLUDE pointing at a directory with jni.h")
        endif()
        target_include_directories(nrr_jni PRIVATE ${SHUGOCORE_JNI_INCLUDE})
    endif()
endif()

