# ShugoCore v1.30.1 — one APK, self-determining CPU kernels + release signing

Patch release. **No public API change**; the full Python suite is **1005 tests
passing** (Python 3.9–3.13).

## One APK, self-determining CPU kernels

The fleet needed a single installable that is safe on every arm64 SoC and still
fast where the silicon allows it. ggml now builds **every arm64 CPU kernel
variant** into its own dlopen'ed backend library
(`libggml-cpu-android_armv8.0_1.so` … `android_armv9.2_2.so`). Each exports
`ggml_backend_score()`, which returns **0** when the running CPU lacks that
variant's features (detected via `getauxval(AT_HWCAP/AT_HWCAP2)`), and
`ggml_backend_load_best("cpu")` loads the highest scorer — so the same APK
picks the portable `armv8.0` kernels on an Exynos 9611 and the dotprod+fp16
set on an Exynos 1380, with no per-device builds and no `SIGILL`.

- **`llama_jni`** now holds the shared llama/ggml libraries and gained
  `nativeSetBackendPath()`: ggml's own search paths (executable dir, cwd,
  `GGML_BACKEND_DIR`) never include Android's native library directory, so the
  app passes `applicationInfo.nativeLibraryDir` in before any model work —
  otherwise no CPU backend registers and every model call fails.
- **`extractNativeLibs`/`useLegacyPackaging` is now `true`.** With the libs
  packed uncompressed inside the APK they are invisible to ggml's directory
  scan (verified: 0 backends found on the A51). Extraction makes the directory
  real.
- **16 KB page alignment** applies to every shipped shared object, not just
  `llama_jni` (global `add_link_options`).
- **Escape hatch:** `-Pshugocore.singlearch=true` restores the historical
  static single-library layout (with the `-Pshugocore.dotprod` override).
- **Verified on two devices with one APK:** A51 (no `asimddp`) →
  `libggml-cpu-android_armv8.0_1.so`; Tab S9 FE (`asimddp`+fp16) →
  `libggml-cpu-android_armv8.2_2.so`. Both models load, zero crashes.
  Measured: ~30 s/decision on the A51 (0.5B Q4_K_M); ~23 s/decision on the
  Tab (1.5B Q4_K_M, dotprod+fp16).

## Decision-prompt steering

The decision prompt now says to prefer `record_observation` for routine
self-maintenance and that side-effecting actions (send/query/sync, device or
hardware control) need operator approval — reducing the rate at which the small
model proposes gated actions for nothing.

## CI

`.github/workflows/android.yml` builds and signs the release APK on every
`v*` tag and attaches it to the release. It also applies the NRR patch series
(a fresh clone without them fails on the upstream Windows-only
`windows.h` include), installs Python 3.13 (Chaquopy's build interpreter) and
the JetBrains Runtime 21 (the Gradle daemon's pinned toolchain).

## Install

APK: `shugocore-1.30.1.apk` (attached to this release, arm64-v8a).
Python: `pip install shugocore==1.30.1`.

Then stage a model (`Qwen2.5-0.5B-Instruct-Q4_K_M.gguf` or larger) into
`/data/local/tmp/` and start the app — it is moved into `files/models/`
automatically on startup.
