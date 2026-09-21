# Layer Split over llama.cpp RPC (Option 2)

## Goal

Spread transformer inference across peripheral devices so the peripherals
contribute **RAM and compute** and the host's load drops. This is Option 2
(layer/pipeline split), as opposed to Option 1 (KV-cache / context split, see
`kv_cache_mesh_split.md`).

Unlike Option 1, Option 2 does not need a new attention algorithm: llama.cpp
ships an RPC backend where a peripheral process (`ggml-rpc-server`) exposes its
memory and CPU/GPU as an offload device, and the host's `llama-server` assigns
layers to it. **The RPC server source is already vendored** at
`platforms/android/app/src/main/cpp/llama.cpp/tools/rpc/rpc-server.cpp`, so this
is an integration and orchestration problem rather than a research problem.

## Measured on our hardware (2026-09-20)

Setup: host = this Mac (AppleClang arm64 host build of llama.cpp, commit 6703d78,
`-DGGML_RPC=ON`); peripheral = Tab S9 FE (SM-X518U), NDK 27 cross-build for
arm64-v8a (`-DANDROID_PLATFORM=android-28`, `-DGGML_RPC=ON`,
`-DGGML_BACKEND_DL=ON`, `-DANDROID_STL=c++_static`), pushed to
`/data/local/tmp/rpc/` and run as `ggml-rpc-server -H 0.0.0.0 -p 50052 -t 4`.
Model: Qwen2.5-0.5B-Instruct-Q4_K_M (398 MB), same file on both ends.
Prompt "The capital of France is", 32 tokens, greedy.

| configuration | host RSS | peripheral RSS | prefill | decode |
|---|---|---|---|---|
| local only (no RPC) | 677 MB | - | 241 tok/s | 140 tok/s |
| 12 layers -> Tab | 502 MB | - | 9.2 tok/s | 4.2 tok/s |
| all layers -> Tab | **206 MB** | **402 MB** | 0.9 tok/s | 1.95 tok/s |

The peripheral is real: after loading, the phone's `MemAvailable` fell 504 MB ->
231 MB while its `ggml-rpc-server` held 402 MB resident, and the host's RSS fell
70%. `llama-server --list-devices` reports the phone as
`RPC0: 192.168.1.164:50052 (5425 MiB, 5425 MiB free)`.

### What this means

- **Memory offload works today.** A host with a small RAM budget can run a model
  whose weights live on a phone, at the cost of throughput.
- **Throughput is transport-bound.** llama.cpp's RPC backend is synchronous per
  layer step, so each decoded token pays many LAN round trips: decode fell
  140 -> ~2-4 tok/s over Wi-Fi. This is the binding constraint, not the phone's
  compute. The Tab also showed its ~1 s LAN latency anomaly during this session,
  which makes it the worst-case node in the fleet.
- Therefore the near-term value is **capacity and locality** (models that do not
  fit, or running the model "on" a phone with the host's help), not raw speed.
  A throughput win needs a lower-latency link: USB (`adb forward`), wired
  Ethernet, or the RDMA-over-Thunderbolt transport ggml-rpc already carries for
  Apple-to-Apple links.


## Two hazards the orchestration layer must own

1. **The advertised capacity is not usable capacity.**
   `--list-devices` reported `5425 MiB free` for a phone whose real
   `MemAvailable` was ~504 MB. Trusting that number would push the host to
   allocate beyond the phone's headroom and get the peripheral OOM-killed
   mid-generation. The orchestrator must compute a safe budget from the device's
   *measured* free memory (minus the agent runtime's own footprint) and cap the
   layer count it assigns - the same accounting discipline `kv_mesh/allocator.py`
   already applies to KV shards.
2. **The RPC server is unauthenticated.** llama.cpp prints it itself:
   `Never expose the RPC server to an open network! This is an experimental
   feature and is not secure!` There is no token, no TLS, no peer check. Our
   trust boundary must therefore be external to the RPC transport: bind to a
   private interface, restrict peers to *paired* devices (the existing
   `MobileNodeRegistry` / `CapabilityRegistry.mobile_devices_allowlist`), prefer
   a tunnel (adb forward over USB, or an authenticated Shogunet/overlay path)
   over a raw LAN socket, and audit every attach/detach. This is a direct input
   to the security workstream: an unauthenticated memory-and-compute device on
   the LAN is exactly the kind of asset the monitoring layer should watch.

## Increments

1. **Cross-build + run the peripheral — DONE, and now packaged in the APK.**
   `platforms/android/app/src/main/cpp/CMakeLists.txt` gained
   `SHUGOCORE_RPC_SERVER` (ON; arm64-v8a only, since the x86_64 ABI is for the
   emulator), which forces `GGML_RPC=ON` before `add_subdirectory(llama.cpp)`
   and builds a minimal executable from `tools/rpc/rpc-server.cpp` linking only
   `ggml` (the RPC backend is a dlopen'd module discovered at runtime). The app
   packages it as `lib/arm64-v8a/libshugocore_rpc_server.so` beside
   `libggml-rpc.so` — verified in the built APK and on the device, where
   `useLegacyPackaging true` extracts it to `nativeLibraryDir` as
   `-rwxr-xr-x`, i.e. a real file that can be `exec`'d (Android blocks exec of
   app-writable data dirs, so the lib dir is the only viable home).

   **Two runtime requirements found by running it there:**
   - it needs `LD_LIBRARY_PATH=<its own directory>` — a directly-exec'd helper
     does not get `nativeLibraryDir` on its search path, so it fails with
     `library "libggml.so" not found` even though every library it needs is
     beside it. `MeshRpcLauncher` now sets this for the child (preserving any
     inherited value).
   - `libc++_shared.so` and `libomp.so` come from the app's own lib dir; they
     are already shipped (the app links C++ shared and ggml uses OpenMP), so
     nothing extra is needed — but a build that disabled OpenMP for the app
     would need `GGML_OPENMP=OFF` here too.

   Verified end-to-end on the Tab S9 FE (Android 16) by executing the packaged
   binary as the app uid:
   ```
   load_backend: loaded RPC backend from .../lib/arm64/libggml-rpc.so
   load_backend: loaded CPU backend from .../lib/arm64/libggml-cpu-android_armv8.2_2.so
   Usage: libshugocore_rpc_server.so [options]
   ```
   Note the CPU backend: the peripheral picks the *runtime-selected* kernel set,
   the same one the app's own inference uses.
2. **`MeshRpcLauncher`** (Python, next to `TermuxLlamaServer`): fail-closed
   start (no binary / no permission -> refuse), port allocation, thread count
   from `hardware_concurrency`, `running()` / `stop()` idempotent, injectable
   `popen` for tests - mirroring the existing launcher's shape.
3. **Capacity + orchestration**: `RpcCapacity` computing a safe layer budget per
   node from measured headroom; pairing-gated attach/detach with audit events;
   rebalance on leave; fail-closed to local inference when the mesh cannot hold
   the model.
4. **Device smoke phases**: `rpc_node_up` — the phase asks the app to start the
   peripheral, then asserts reachability from the host and a clean stop — now
   passes on the Tab S9 FE. `layers_offloaded` lives instead as the repeatable
   benchmark `tests/mesh_rpc_bench.py` (needs a host llama-server build and a
   GGUF, so it is not part of the on-device suite), refusing a split on a
   thermally critical node stays open work.
5. **Transport work** (the throughput unlock): measure USB `adb forward`,
   then evaluate RDMA/wired options before promising a speed win.

## Reproduce

One command, end to end (starts the app's peripheral itself, prints the table
above, stops the peripheral afterwards):

```bash
python3 tests/mesh_rpc_bench.py --server /tmp/rpc-host/bin/llama-server \
    --model /tmp/qwen0.5b.gguf \
    --serial adb-R52WC05JPMW-4kMS88._adb-tls-connect._tcp
```

The manual version of the same run (step by step):

```bash
# peripheral (NDK cross-build, arm64)
cmake -S platforms/android/app/src/main/cpp/llama.cpp -B /tmp/rpc-arm64 \
  -DCMAKE_TOOLCHAIN_FILE=$ANDROID_NDK/build/cmake/android.toolchain.cmake \
  -DANDROID_ABI=arm64-v8a -DANDROID_PLATFORM=android-28 \
  -DANDROID_STL=c++_static -DCMAKE_BUILD_TYPE=Release \
  -DGGML_RPC=ON -DGGML_BACKEND_DL=ON -DBUILD_SHARED_LIBS=ON \
  -DLLAMA_BUILD_TOOLS=ON -DLLAMA_BUILD_SERVER=OFF
cmake --build /tmp/rpc-arm64 --target ggml-rpc-server -j8

# host
cmake -S platforms/android/app/src/main/cpp/llama.cpp -B /tmp/rpc-host \
  -DCMAKE_BUILD_TYPE=Release -DGGML_RPC=ON -DLLAMA_BUILD_SERVER=ON \
  -DLLAMA_BUILD_TOOLS=ON
cmake --build /tmp/rpc-host --target llama-server ggml-rpc-server -j8

# run: phone holds the layers, host holds the logits
adb push /tmp/rpc-arm64/bin/* /data/local/tmp/rpc/
adb shell 'cd /data/local/tmp/rpc && LD_LIBRARY_PATH=. ./ggml-rpc-server -H 0.0.0.0 -p 50052 -t 4'
/tmp/rpc-host/bin/llama-server -m qwen0.5b.gguf --rpc <phone-ip>:50052 -dev RPC0 -ngl 99
```
