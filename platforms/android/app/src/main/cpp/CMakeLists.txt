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

# ARMv8.2 dotprod kernels for arm64-v8a: without them Q4_K_M matmuls run
# the generic NEON path (measured ~1 tok/s prefill on Exynos 1380). The
# device exposes asimddp on ALL cores (arm64 cpuinfo lists the feature
# intersection), so dotprod codegen is safe on every core.
if(ANDROID AND ANDROID_ABI STREQUAL "arm64-v8a")
    set(GGML_CPU_ARM_ARCH "armv8.2-a+dotprod" CACHE STRING "" FORCE)
endif()

if(SHUGOCORE_VULKAN)
    set(GGML_VULKAN ON CACHE BOOL "" FORCE)
endif()

# Keep the llama.cpp build minimal: only the library targets are needed.
set(LLAMA_BUILD_EXAMPLES OFF CACHE BOOL "" FORCE)
set(LLAMA_BUILD_TESTS    OFF CACHE BOOL "" FORCE)
set(LLAMA_BUILD_SERVER   OFF CACHE BOOL "" FORCE)
set(LLAMA_BUILD_TOOLS    OFF CACHE BOOL "" FORCE)
set(BUILD_SHARED_LIBS    OFF CACHE BOOL "" FORCE) # static llama/ggml linked into llama_jni

add_subdirectory(${LLAMA_CPP_DIR} llama_build EXCLUDE_FROM_ALL)

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

