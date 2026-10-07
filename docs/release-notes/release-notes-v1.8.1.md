## v1.8.1 — Android Runtime Fixes, On-Device Model Downloads & Desktop Server Mode

### Fixed — "Start Agent does nothing" on Android

Root cause: `android_inference` (and the whole decision-engine closure) was
never bundled into the Chaquopy source set, so `create_backend({"type":
"android"})` raised `ValueError`, was silently caught, and left `pyAgent =
null` — the Start Agent button appeared to do nothing.

- `py-modules` now includes `android_inference` + `shugocore_agent`; all 21
  engine modules are bundled into `app/src/main/python/`.
- `DecisionEngine` imports lazily inside a guarded `_initialize_engine` —
  missing engine dependencies degrade to the stub observation loop instead of
  crashing agent construction.
- Kotlin compile fixes: `Service.START_STICKY` (previous constant is
  API-34-only), `PyObject.toJava(Map::class.java)`, vararg spread for
  `create_agent(soc, api_url)`, null-safe model-dir scan, inference init moved
  off the main thread.

### Added — On-device model downloads (no adb pushes)

- **Models… dialog in `MainActivity`**: curated catalog of 5 ungated Hugging
  Face GGUF quantizations (Qwen2.5-0.5B/1.5B/3B-Instruct,
  Llama-3.2-1B-Instruct, SmolLM2-1.7B-Instruct, all Q4_K_M), tagged
  `recommended` per RAM tier; plus sideloaded `.gguf` files, select & load /
  delete, and a live %/MB progress bar.
- **`ModelDownloader`**: resumable HTTP downloads (Range + `.part` → atomic
  rename + exact-size verification + cancel + auto-retry) into
  `filesDir/models`.
- **Hot-load**: `ShugoCoreService.loadOnDeviceModel()` starts the llama.cpp
  bridge and the loopback `LocalApiServer` on `127.0.0.1:11434` without
  restarting the agent — the agent's default backend URL is the same
  endpoint, so on-device inference activates on the next generate call.
- `findModelFile()` prefers the persisted `selected_model`.

### Added — Sensor engagement test cycle

- `AndroidAgent.update_telemetry()` / `sensor_test_cycle(steps)` /
  enriched `get_status()`; Kotlin pushes battery / charging / CPU temp / RAM /
  accelerometer / thermal telemetry every tick.
- Binder APIs `getAgentStatus()` / `runSensorTestCycle()` /
  `getDeviceRecommendation()`; `MainActivity` polls agent status at 1 Hz —
  **fixes the frozen memory status display**.
- `tests/test_sensor_engagement.py` — 6/6 passing.

### Added — Desktop server mode (macOS / Linux / Windows)

- `shugocore_server.py` (`shugocore-server` console script) speaks the Ollama
  wire contract (`/api/generate`, `/api/chat`, `/api/tags`, `/health`) plus
  the engine API (`/api/v1/status`, `/api/v1/task`), so the tablet pairs as a
  node with zero client changes. Desktop URL field in `MainActivity`;
  on-device llama.cpp remains the default when blank.
- `tests/test_shugocore_server.py` exercises every endpoint with real HTTP.

### Fixed — Android build toolchain

- Gradle wrapper pinned back to **8.11.1** (Gradle 9 removed
  `Project.exec(Action)` that AGP 8.7.3 needs), daemon JDK pinned to 21,
  Kotlin Gradle plugin + `kotlinOptions jvmTarget 17` + `ndkVersion
  27.0.12077973` + Chaquopy `buildPython` added. `compileDebugKotlin`,
  `configureCMakeDebug`, and the full `assembleDebug` APK all build.

### Fixed — Backend resilience

- `OllamaBackend` default timeout 30 → 120 s (cold model loads made every
  decision fall back).
