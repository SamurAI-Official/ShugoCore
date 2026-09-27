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

## Building the host (Windows, MSVC)

`scripts/build_llama_rpc.py` runs the whole thing; the commands used to live only
in prose. It locates CMake/Ninja (the Android SDK's copies work as host tools),
loads the MSVC environment through `vswhere`, records the submodule commit and
the repo pin in `runtime/evidence/mesh_model_host/llama_build.txt`, and verifies
the result:

```powershell
python scripts/build_llama_rpc.py --host            # llama-server + ggml-rpc-server
python scripts/build_llama_rpc.py --android --ndk G:\Android\Sdk\ndk\27.0.12077973
python scripts/build_llama_rpc.py --host --dry-run  # show what it would run
```

Three traps, each found by running it rather than reading it:

1. **Static backends, not shared.** With ggml's shared/dynamic backends the first
   Windows build produced a `llama-server.exe` that logged "failed to find
   ggml_backend_init" for its own DLLs and could not offload. The host build now
   sets `-DBUILD_SHARED_LIBS=OFF -DGGML_BACKEND_DL=OFF`; the Android peripheral
   keeps the documented shared build.
2. **`--list-devices` is not a working-build check** on a static build: it
   reports `(none)` for a binary that then loads the model, holds 377 MB on a
   peripheral and returns identical tokens. The builder gates on `--rpc` being
   present in the server's help instead, and records the device list as
   information.
3. **The MSVC environment must be captured, not inlined.** `cmd /c "call …"` with
   a quoted path does not survive Windows argv quoting, and `set` wraps long
   values so `PATH` came back empty. The environment is now dumped to JSON from
   inside the environment (`capture_msvc_env`).

A linker error is usually a *running* previous build: `LNK1104: cannot open file
'bin\ggml-rpc-server.exe'` means an old peripheral process still holds the file.

### Version rule

Both ends must be built from the commit the parent repo pins. A host and a
peripheral on different trees can disagree about the wire protocol while every
local test passes, and the earlier measurements in this file are only true of the
commit they name. The pin is `git ls-tree HEAD -- <submodule path>`; a leading
`+` in `git submodule status` means the checkout has drifted from it. Changing the
pin is an explicit change: bump it, rebuild **both** ends, and re-measure, naming
the commit. `git add -A` will move the pin for you if you are not watching -- that
is how it moved once already.

## Runtime: the mesh model host (P1.1)

`plan_layer_split()` decides *what* to offload; `mesh_model_host.py` is the runtime
that does it, and `scripts/desktop_agent.py --model-host <gguf>` turns it on:

```powershell
$env:SHUGOCORE_LLAMA_SERVER = "G:\Android\llama-rpc\host\bin\llama-server.exe"
python scripts/desktop_agent.py --device-caps desktop --data-dir runtime/desktop `
    --mesh-node-id shugo-desktop --mesh-priority 10 `
    --model-host G:\Android\llama-rpc\models\qwen2.5-0.5b-instruct-q4_k_m.gguf `
    --model-host-lan --model-host-reserve-mb 64 `
    --api-url http://127.0.0.1:8099
```

The sequence, and why each step fails closed:

1. **Plan** from the election's live peers (their *measured* headroom and thermal
   state). A peer that is thermally critical, unpaired, has no usable headroom, or
   has stopped heartbeating gets no layers and is reported with its reason.
2. **Wake the peripherals** by delegating `mesh_rpc` (`start`, port, lan flag) over
   the mesh. This is new in v1.30.18: only the app can execute its own
   native-library binary, so the primary asks from inside the mesh instead of via
   an adb debug broadcast. The device's own authority gate applies, and it returns
   what it did.
3. **Verify reachability directly** (TCP connect to the peripheral's port). A reply
   would only say what the peer believed; the socket says what is true. A device
   that does not answer is dropped and its layers stay local.
4. **Launch** llama-server through the same launcher the agent already uses, with
   `--rpc <endpoint>,... -dev RPC0,... -ngl <remote layers>` and `--tensor-split`
   when more than one device contributes.
5. **Verify the model, not just the port**: `/health` and then a one-token
   completion, because llama.cpp answers health while a model is still loading.

Reported as one status-line field: `model_host=split(layers=23/24 dev=shugo-mac:23)`
or `model_host=local(skipped shugo-a51:thermal_status=4, ...)`, so "it chose not to
use the fleet" and "it could not" are never confused.

Measured locally (loopback peripheral, all layers remote): host RSS **183 MB**
against **537 MB** local-only, with the probe answering. `mmap` defaults *off* when
offloading and on when local: with mmap the host keeps the GGUF mapped and its RSS
stays at the local-only figure, hiding exactly the win the split exists for.

Security note: the RPC socket is unauthenticated. `--model-host-lan` is therefore an
explicit, audited operator choice -- without it the peripheral stays on loopback and
only a same-machine peripheral can be used. Over Wi-Fi, keep the hive on a trusted
link.


One command, end to end (starts the app's peripheral itself, prints the table
above, stops the peripheral afterwards):

## Reproduce

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
