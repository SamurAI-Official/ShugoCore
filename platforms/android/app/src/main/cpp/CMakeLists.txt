// CMakeLists.txt for building llama.cpp JNI bindings
cmake_minimum_required(VERSION 3.18.0)
project("shugocore")

set(LLAMA_CPP_DIR ${CMAKE_CURRENT_SOURCE_DIR}/llama.cpp)
if(NOT EXISTS ${LLAMA_CPP_DIR})
    message(FATAL_ERROR "llama.cpp submodule not found. Run: git submodule update --init --recursive")
endif()

add_library(
    "llama_jni"
    SHARED
    "llama_jni.cpp"
)

find_library(
    "log-lib"
    log
)

target_include_directories(
    "llama_jni"
    PRIVATE
    ${LLAMA_CPP_DIR}
    ${LLAMA_CPP_DIR}/ggml/include
    ${LLAMA_CPP_DIR}/ggml/cuda
)

# Add Vulkan for GPU offload (if available)
find_package(Vulkan QUIET)
if(Vulkan_FOUND)
    target_link_libraries("llama_jni" ${Vulkan_LIBRARY})
    target_compile_definitions("llama_jni" PRIVATE -DGGML_USE_VULKAN)
endif()

# Build llama.cpp
add_subdirectory(${LLAMA_CPP_DIR} llama_build EXCLUDE_FROM_ALL)

target_link_libraries(
    "llama_jni"
    "log-lib"
    llama
    ggml
)

# Enable Neon optimizations on ARM64
if(${ANDROID_ABI} STREQUAL "arm64-v8a")
    target_compile_options("llama_jni" PRIVATE -march=native)
endif()