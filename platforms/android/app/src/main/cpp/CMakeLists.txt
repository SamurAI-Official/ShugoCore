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
