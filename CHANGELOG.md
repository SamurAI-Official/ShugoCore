# Changelog

All notable changes are documented here. This project adheres to
[Semantic Versioning](https://semver.org). The 1.0.0 public API surface is
frozen: no breaking changes across any 1.x release.

## [Unreleased]

### Added — Desktop server mode (macOS / Linux / Windows)

Users without a high-end 2020+ Android phone can now run the full agent on a
desktop and pair the phone as a lightweight node:

- **`shugocore_server.py`** — new stdlib-only HTTP server (`shugocore-server`
  console script). Speaks the Ollama wire contract (`/api/generate`,
  `/api/chat`, `/api/tags`, `/health`) on one port, so the phone's existing
  `AndroidBackend` connects with zero client changes, plus the engine API
  (`/api/v1/status`, `POST /api/v1/task`) that routes through the same
  policy-gated `execute_task` pipeline as a local call.
- **Backends** — `--backend ollama|llamacpp|openai|stub` with `--backend-url`;
  the same backend config is shared between the HTTP server and the engine's
  model registry so `/api/v1/task` decisions use the identical backend.
- **Android desktop mode** — `create_agent(soc, api_url)` accepts a desktop
  server URL; `MainActivity` gains a "Desktop server URL" field persisted to
  `SharedPreferences` and `ShugoCoreService` passes it to the agent. On-device
  llama.cpp remains the default when the field is blank.
- **Fixes** — the Android agent's engine model config now includes the
  required `id`/`type` keys (previously a silent `KeyError` on device); agent
  tick cadence reduced from 100 ms to 1 s (avoid hammering the LLM + battery).
- **Tests** — `tests/test_shugocore_server.py` exercises every endpoint with
  real HTTP (health, tags, generate, streaming, chat, task success/policy
  block, status, 404, CORS preflight).
- **Docs** — `docs/desktop_server.md` with per-OS setup (macOS brew, Linux
  systemd + ufw, Windows firewall rule) and README section.

## [1.8.0] - 2026-09-03

### Fixed — Android native inference is now real end-to-end

The Android stack previously stopped at placeholder JNI calls and stubbed
HTTP responses. The full chain now runs real tokens:

`HTTP → LocalApiServer → LlamaCppBridge → JNI → llama.cpp → GGUF`.

- **`platforms/android/app/src/main/cpp/llama_jni.cpp`** — rewritten against
  the pinned llama.cpp (b10795) C API: `llama_model_load_from_file` /
  `llama_init_from_model` session creation, vocab-based
  `llama_tokenize` / `llama_token_to_piece`, `llama_decode` batching via
  `llama_batch_get_one`, and a proper sampler chain
  (`penalties → top_k → top_p → temp → dist`) sampled with
  `llama_sampler_sample`. Per-session state (`ShugoSession`: model, context,
  vocab, KV position, pending-UTF-8 buffer) replaces the previous mutable
  globals; `nativeDrain` flushes partial multi-byte characters at
  end-of-generation so streamed text is never corrupted mid-codepoint.
- **`CMakeLists.txt`** — rewritten: valid CMake syntax, static llama/ggml
  linked into `libllama_jni.so`, optional Vulkan GPU offload
  (`-DSHUGOCORE_VULKAN=ON`), no `-march=native` (broke NDK + emulator
  builds), and dual-target support — Android NDK builds and host CI
  validation builds (`-DSHUGOCORE_JNI_INCLUDE=...`).
- **`LocalApiServer.kt`** — replaced JDK-internal `com.sun.net.httpserver`
  (absent on Android) with a minimal HTTP/1.1 implementation on
  `ServerSocket`, loopback-only. `/api/generate` and `/api/chat` now parse
  JSON bodies, apply sampling options (`temperature`, `top_k`, `top_p`,
  `repeat_penalty`, `seed`, `num_predict`), stream NDJSON chunks when
  `stream: true`, and return Ollama-shaped responses (`response` /
  `message.content`, `done`, `eval_count`, `total_duration`) so ShugoCore's
  `OllamaBackend`/`AndroidBackend` work unmodified.

### Verified

- Host build of `libllama_jni.dylib` (full llama.cpp + JNI bridge) compiles
  and links with zero warnings against the pinned headers; all 8 JNI symbols
  (`nativeInit`, `nativeFree`, `nativeTokenize`, `nativeDetokenize`,
  `nativeDrain`, `nativeEvalPrompt`, `nativeGenerateToken`, `nativeReset`)
  exported with names exactly matching the Kotlin `external fun`
  declarations.
- Version metadata aligned at 1.8.0 across `version.py`, `pyproject.toml`,
  and `build.gradle`.

## [1.4.0] - 2026-09-03
## [1.4.0] - 2026-09-03

### Added — fleet-shared Tier 2 memory (PostgreSQL + pgvector)

- **`pg_memory.py`** — `PgSemanticMemory`, a drop-in PostgreSQL + pgvector
  backend for Tier 2 semantic memory, enabling the persistence half of the
  Shogunet memory mesh: several agents (or planning nodes) pointing at the
  same DSN see one consistent knowledge base. API parity with the SQLite
  `SemanticMemory` (store_fact / search / reinforce / decay / prune /
  get_fact / extract_entities / facts_about / related_entities /
  entity_names), so it plugs directly into
  `DecisionEngine(semantic_memory=...)` and `MemoryManager(semantic=...)`.
- **`open_semantic_memory()` factory** — single storage knob for operators:
  a `postgres://` or `postgresql://` DSN selects `PgSemanticMemory`; any
  other value preserves the historical local SQLite behavior.
  `DecisionEngine` routes `memory_db_path` through it, so switching a fleet
  to shared memory is a one-line config change.
- **Embedding parity** — the pg backend embeds with the same deterministic
  hashing embedding as the SQLite backend, so facts written by one agent on
  one backend are retrievable with identical similarity scores by another
  agent on the other backend.
- **Search pushed down to pgvector** — cosine distance (`<=>`) is computed
  server-side (`similarity = 1 - distance`), with an optional HNSW index
  recipe for fleet scale documented in the module docstring.
- **Fail-closed, no silent stub** — construction raises with actionable
  instructions if psycopg2 is missing (`pip install 'shugocore[postgres]'`)
  or the pgvector extension is unavailable (`CREATE EXTENSION vector;`).
  A fleet-shared memory that quietly failed to persist would violate the
  Tier 2 invariants, so none exists.
- **`postgres` optional dependency** — `pip install 'shugocore[postgres]'`
  installs psycopg2-binary; no new required dependencies for existing users.

### Hardened — fleet memory boundary

- Table identifiers (`table_prefix`) are strictly validated
  (`^[a-z][a-z0-9_]{0,40}$`) before interpolation into DDL/DML.
- Tier 2 only: the pg store never touches Tier 0/1 (per-agent) or Tier 3
  (read-only identity), preserving the memory invariants (N0-N1) across the
  fleet.

## [1.5.0] - 2026-09-03

### Added — robot simulation framework for public test data

- **`simulation/` module** — Physics-based robot simulation framework with
  pluggable backends (MuJoCo, stub fallback) for generating public benchmark data.
- **`MuJoCoSimulation`** — Full MuJoCo physics backend (`pip install 'shugocore[simulation]'`).
  Loads MJCF/URDF models, provides joint control, IMU sensing, and deterministic
  seeded simulation. Falls back to `StubSimulation` when MuJoCo is not installed.
- **`StubSimulation`** — Deterministic in-memory simulation for testing without
  physics dependencies. Integrates joint commands into positions with simple
  kinematics.
- **Robot model loaders** — Support for multiple open-source robot platforms:
  - `BerkeleyHumanoidLite` — 24-DOF humanoid from UC Berkeley (MIT license)
  - `Reachy2` — Humanoid by Pollen Robotics (Apache-2.0 license)
  - `UnitreeG1` — Compact humanoid by Unitree Robotics (BSD-3-Clause license)
- **Standardized test scenarios** — Reproducible benchmark scenarios producing
  public JSONL test data:
  - `WalkToTarget` — Navigation efficiency and path planning
  - `BalanceTest` — Stability under perturbations
  - `EmergencyStop` — Safety response measurement
- **`run_benchmark()`** — Execute full benchmark suite on any robot, output
  results to JSONL for public dataset publication.
- **`SimulationResult`** — Standardized result format with serialization for
  public test data distribution.
- **`simulation` optional dependency** — `pip install 'shugocore[simulation]'`
  installs `mujoco>=3.0` and `numpy>=1.24`; no new required dependencies.
  installs `mujoco>=3.0` and `numpy>=1.24`; no new required dependencies.

## [1.7.0] - 2026-09-03

### Added — Android native layer

- **`platforms/android/`** — Complete Android application shell with native
  llama.cpp inference via JNI bindings.
- **`LlamaCppBridge.kt`** — Kotlin JNI wrapper for llama.cpp with token
  streaming, batching, and resource management.
- **`LocalApiServer.kt`** — OpenAI-compatible HTTP API server running on
  127.0.0.1:11434, enabling ShugoCore backends to work unmodified on Android.
- **`CapabilityDetector.kt`** — Hardware capability detection (SoC, NPU, GPU,
  RAM) for automatic model/quantization selection.
- **`ThermalMonitor.kt`** — Battery and thermal state monitoring with
  inference throttling and emergency shutdown.
- **`ShugoCoreService.kt`** — Foreground service managing inference backend
  lifecycle, thermal throttling, and periodic agent execution.
- **`MainActivity.kt`** — Minimal UI for starting/stopping the agent service.
- **`llama_jni.cpp`** — Native JNI bindings for llama.cpp with Vulkan GPU
  offload support.
- **`shugocore_agent.py`** — Python agent entrypoint for Chaquopy runtime.
- **`android_inference.py`** — Android backend compatible with
  `OllamaBackend` interface.

## [1.6.0] - 2026-09-03

### Added — dream consolidation and memory write gates

- **`DreamConsolidation`** — Periodic reflective pass that compresses episodic
  experiences into durable identity insights. Inspired by GrowBot's "dream" phase.
  Runs at a slower cadence than regular consolidation (every N ticks).
- **Dream write-permission discipline** — The dream is the SOLE writer of
  Tier 3 (CoreIdentity) mutations during normal operation (code-enforced).
  Commits are clamped: max 1 sentence added per dream, identity never falls
  below minimum length.
- **Insight extraction** — Automatically extracts actionable insights from
  episodic events: recurring failure patterns (≥2 occurrences) and consistent
  success strategies (≥3 occurrences).
- **Memory write-gate enforcement** — Code-enforced write permissions per memory
  tier:
  - Tier 0 (Scratchpad): Only scratchpad writes
  - Tier 1 (EpisodicMemory): Only episodic record (append-only)
  - Tier 2 (SemanticMemory): Only consolidation/maintenance worker
  - Tier 3 (CoreIdentity): Only dream consolidation or explicit promotion
- **`check_write_permission()` / `enforce_write()`** — Runtime write-gate
  enforcement with `PermissionError` on violation.
- **Continuous agent integration** — Dream consolidation automatically runs
  during the continuous loop. Dream stats exposed via `status()`.
- **Tests** — 16 new tests covering dream consolidation, insight extraction,
  identity mutation clamping, and write-gate enforcement.

## [1.3.0] - 2026-09-03

### Added — hardening for Continuous Synthetic Functional Agency

- **Continuous agent daemon** (`continuous_agent.py`): a top-level orchestrator
  that embodies the OBSERVE → GATE → DECIDE → EXECUTE → EVALUATE → RECORD →
  CONSOLIDATE loop in a single entry point. Bounded iteration counts, bounded
  interval pacing, and graceful shutdown. CLI:
  `python3 continuous_agent.py --interval 2.0 --max-iterations 1000`.
- **HMAC-signed audit chains** (`audit.py`): `AuditChain` now accepts an
  optional `hmac_key` (operator-held, e.g. via `SecretResolver`). Entries carry
  an HMAC-SHA256 tag over the chain hash + payload, making history
  tamper-evident *and* authenticated when the audit file lives on shared
  storage. Verification is backward-compatible with unsigned (1.2.x) chains.
  Includes a `verify_audit_file()` helper and `python3 audit.py <file>` CLI.
- **Real embeddings for Tier 2** (`vector_db.py`): environment observations
  were previously stored with all-zero placeholder vectors; they now use
  deterministic n-gram hashed embeddings (`hashed_embedding()`), making
  similarity search meaningful without any third-party dependency.
- **Pluggable embedding backends** (`vector_db.py`): `VectorDB` accepts an
  injectable embedding function, so operators can swap in a learned encoder
  (sentence-transformers, OpenAI, etc.) without touching storage logic.
- **Shogunet optional dependency** (`pyproject.toml`): the networking runtime
  is now installable via `pip install shugocore[shogunet]`.

### Hardened — ethics surface

- `EthicalGovernor` placeholder predicates (`can_explain`, `detect_bias`,
  `is_privacy_compliant`, `can_audit`) no longer return hardcoded values.
  They now evaluate real signals: decision provenance/audit-trail presence,
  input attribute screening for protected-category bias, and data-subject
  consent coverage for privacy compliance.

### Fixed

- `pyproject.toml` version was left at 1.2.0 after the 1.2.1 bump; both now
  share the single source of truth in `version.py` values.

## [1.2.1] - 2026-09-02

### Added — Shogunet multi-agent networking integration

- `shugonet_bridge.py` — ShugoCore-side adapter for the Shogunet networking
  layer, following the same pattern as `robotics_handler.py` and
  `mobile_nodes.py`. Enables multi-agent collaboration over 5G, 4G, WiFi,
  LoRa, and Bluetooth with a codependent memory mesh.
  - `ShugonetExecutionHandler` dispatches network actions to the Shogunet
    `ShugonetAgentRuntime`.
  - `register_network_handlers()` registers network action types with the
    `ExecutionLayer` and `policy.KNOWN_ACTION_TYPES`.
  - `attach_network_fallbacks()` merges network trigger severities into the
    deterministic `FallbackController`.
  - Network action types: `network_send`, `network_query`, `network_sync`
    (side-effecting) and `network_list_agents`, `network_status`
    (read-only).
  - Network fallback triggers: `network_transport_exhausted` (pause),
    `network_peer_lost` (pause), `memory_sync_conflict_storm` (safe_state),
    `audit_chain_broken` (halt).
  - `network_topic()` helper for canonical `/shugunet/{agent_id}/{tail}`
    topic construction.
- `tests/test_shugonet.py` — 22 integration tests covering action type
  registration, handler dispatch, fallback severity integration, and
  execution-layer compatibility.
- `DecisionEngine` now accepts an optional `shogonet_handler` parameter
  for automatic handler registration at engine construction time.

## [1.2.0]

### Added — Multiphase stress-test suite (97 tests) and robustness fixes

- `tests/test_android_lifecycle_stress.py` (30 tests): lifecycle churn
  (200 full create/pause/resume/destroy cycles), concurrent pause/resume
  races, monitor-thread recreation, power/thermal edge cases and streak
  semantics, `SecureStoreSecretProvider` failure modes, node start/stop
  leak detection, bridge-death-mid-run, flaky sensor bridges, and a
  required 5-second sensor soak (monotonic heartbeats, bounded log).
- `tests/test_ros2_transport_stress.py` (27 tests): bridge death,
  post-shutdown publish/subscribe, garbage drains, concurrent publish
  bursts, rate-limiter + emergency-stop bypass, payload round-trip
  fidelity, NaN/Inf sanitization, cross-transport parity.
- `tests/test_thermal_stress.py` (20 tests): ladder transitions,
  1000-cycle oscillation soak, thermal/accelerator-failure interaction,
  garbage thermal values, recovery semantics.
- `tests/test_model_execution_stress.py` (20 tests) plus
  `tests/fake_llama_server.py`: a loopback llama.cpp/Ollama/LM Studio
  wire-protocol test double driving the real `requests` HTTP paths —
  launcher detection, timeouts, malformed/500/empty responses, and
  concurrent generation.

### Fixed — found by the stress suite

- `AndroidShugoCoreNode.stop()` leaked its memory-worker thread
  (`MemoryManager.shutdown()` was never invoked; one thread leaked per
  node instantiation).
- `JavaBridgeROS2Interface.spin_once` crashed on malformed payloads
  returned by `drainMessages` (unguarded JSON parse).
- `RosBridgeInterface.spin_once` crashed on valid-JSON-but-not-dict
  packets (e.g. a bare string).

### Changed

- Package version aligned at 1.2.0 across `pyproject.toml`,
  `version.py`, and the test pin.

## [1.1.0]

### Added - Android compute nodes (hardware-agnostic mobile integration)
- `acceleration.py`: NPU/DSP/GPU/CPU accelerator abstraction with per-workload
  preference ladders, failure-induced degradation, and thermal demotion
  (NNAPI/Hexagon/Jetson DLA/Intel NPU enumerators; deterministic CPU fallback).
- `android_bridge.py`: `JavaBridgeROS2Interface` (Chaquopy + jros2/Fast-DDS
  in-process) and `RosBridgeInterface` (rosbridge over WebSocket for Termux).
- `android_runtime.py`: app-lifecycle mapping, WakeLock/MulticastLock,
  Keystore-backed secrets, battery/thermal monitoring -> fallback triggers.
- `android_node.py`: on-device node roles - `sensor_node`, `compute_node`
  (LiteRT/NNAPI), `operator_node` (clamped teleop relay), `full_agent`
  (offline local model on Ollama/llama.cpp/LM Studio, endpoint-allowlisted).
- `mobile_nodes.py`: host-side fleet layer - pairing with TTL, topic ACL
  (`/shugocore/mobile/#` only), payload sanitization, heartbeat liveness,
  topic-based compute broker, execution handler.
- Policy/engine wiring: mobile action types (consent-gated compute offload),
  loopback model-endpoint allowlist, 6 new fallback triggers; fixed the action
  parser so robot_*/mobile_* proposals are no longer silently dropped.
- Offline-first: ShugoCore remains dependency-free (websocket-client optional
  for Termux only); `platforms/<android>` code is quarantined and the core is
  verified to import and run with platform modules hard-blocked.

## [1.0.0]

### Added — Engine hardening & determinism (Phase 1)
- `state_machine.py`: `ExecutionGovernor` with an allowed-transition matrix,
  re-entrancy guard (recursive tool->agent->tool loops are refused), per-task
  step budgets and wall-clock deadlines.
- `fallbacks.py`: `FallbackController` with deterministic, rule-based safe
  escalation (`pause` default, `safe_state`/`halt` for critical triggers).
- `token_budget.py`: dependency-free token estimator and `ContextBudget`
  with rigid per-section allocations; the Tier 0 scratchpad now evicts on a
  token ceiling and decisions trim memory context to budget.

### Added — Memory tiers (Phase 2)
- Tier 1 `EpisodicMemory` is now a crash-safe append-only journal
  (`episodic_journal_path`) with startup replay and age-based eviction.
- Tier 2 `SemanticMemory` gained an entity graph (`entities`,
  `fact_entities`): deterministic extraction, `facts_about` /
  `related_entities` graph queries, merged with vector similarity in hybrid
  retrieval.
- Tier 3 `CoreIdentity.system_prompt()` deterministically renders
  world-model invariants as immutable operational rules.
- Async maintenance worker gained a watchdog (health stats) and exponential
  backoff, escalating to the fallback controller on repeated failures.

### Added — Enterprise & developer surface (Phase 3)
- `version.py` (`__version__ = "1.0.0"`), `pyproject.toml` (installable
  package, `shugocore-verify-audit` console script).
- `telemetry.py`: optional OpenTelemetry hooks with a built-in no-op tracer
  (zero-dependency guarantee preserved).
- `benchmarks/run.py`: local-first benchmarks for tool-calling accuracy,
  memory compaction fidelity and step-execution latency.

### Security & integration (from the previous hardening work)
- Single gated execution path; hash-bound policy verdict tokens; consent
  registry; approval broker; capability allowlists; egress controls;
  redacted logging; tamper-evident audit chain; honest (`not_implemented`)
  execution.

## [0.4.0] - 2026-08-31
### Changed
- README expansion: project positioning, design principles, orchestration
  loop, safety model, install guide.

## [0.3.0] - 2026-08-31
### Added
- Tiered memory system (Tier 0-3) with consolidation pipeline, promotion
  review, Tier 2->Tier 3 ledger-backed promotion.

## [0.2.0] - 2026-08-31
### Fixed
- Integration bugs: optional torch/chromadb, autonomy loop guard, subprocess
  and URL-construction fixes, missing RL/vector/model APIs, ethics-gate
  demo task. Added `.gitignore`, `requirements.txt`.

## [0.1.0] - 2026-08-31
### Added
- Initial modules: decision engine, autonomy, execution layer, model
  manager, reinforcement learning, task manager, vector DB, logging,
  subconscious (Ollama subprocess), memory scaffolding.