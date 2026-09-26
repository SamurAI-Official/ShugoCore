# ShugoCore

> A continuous orchestration layer for synthetic functional agency.

[![PyPI](https://img.shields.io/pypi/v/shugocore)](https://pypi.org/project/shugocore/)
![Release](https://img.shields.io/badge/release-v1.30.4-blue)
![Tests](https://img.shields.io/badge/tests-1105%20passing-brightgreen)
![Python](https://img.shields.io/badge/python-3.9%E2%80%933.13-blue)
![Platform](https://img.shields.io/badge/platform-Linux%20%7C%20macOS%20%7C%20Android%20%28Termux%2FChaquopy%29-lightgrey)
![License](https://img.shields.io/badge/license-MIT-green)

ShugoCore coordinates models, tools, and a four-tier memory system so that an
artificial agent can *act* in an environment, register the consequences, and
adapt - indefinitely, without context degradation or unbounded state growth.

Where a chat model produces text, a functionally agentic system produces
*effects*: decisions that become tool calls and API interactions, outcomes
that become reward signals, and experience that consolidates into durable
knowledge. ShugoCore is the layer that makes that cycle safe, auditable, and
able to run continuously.

## Project scope

**In scope.** A framework for *functionally agentic* systems: a single
policy-gated execution path (`DecisionEngine.execute_task`) shared by
interactive tasks, autonomous cycles and the task queue; a four-tier memory
architecture with consolidation and salience decay; pluggable model backends
(Ollama, llama.cpp, any OpenAI-compatible endpoint, on-device Android
inference); tool/API execution with allowlisted side effects and consent +
operator approval; a tamper-evident audit chain; a mobile/Android node fleet
over ROS 2 transports; ShugoNet multi-agent messaging; and the desktop server
that exposes the engine over the Ollama-compatible wire contract.

**Out of scope.** ShugoCore is not a model, a training framework, or an
inference engine - it orchestrates backends and vendors runtimes (llama.cpp,
NRR, ONNX Runtime) for the Android node rather than producing weights. It is
not a general-purpose RPA/iPaaS platform, and it claims no autonomy beyond the
authority it is given: side effects are bounded by Tier 3 invariants, consent
grants, operator approval and capability allowlists.

**Compatibility.** The public Python surface is frozen across the 1.x series -
no breaking changes in any 1.x release; deprecated functionality is removed
only after at least one minor release of deprecation.

## Design principles

**Continuous.** Long-running agency fails when memory is naive: the context
window exhausts, or raw logs grow forever. ShugoCore's memory pipeline
consolidates, decays, and promotes continuously so the observe-act loop can
run for as long as the mission requires.

**Functional.** Decisions terminate in execution. Every action is gated by
deterministic world-model invariants *before* it touches the environment,
and every outcome is recorded as a structured episodic event that feeds
reinforcement learning.

**Accountable.** Agency without accountability is unsafe. Hard constraints
live in a read-only world model (Tier 3) that the agent's own execution
path cannot rewrite, and every decision and outcome is logged.

**Bounded.** Every subsystem is capacity- or decay-bounded: ring-buffered
episodes, salience-decayed facts, iteration-capped autonomous cycles, and a
decoupled maintenance worker that never blocks the primary loop.

## The orchestration loop

```
1. OBSERVE      task arrives; reasoning tokens enter the Tier 0 scratchpad
2. GATE         Tier 3 invariants check the action before anything runs
3. DECIDE       models are selected and aggregated, enriched with Tier 2 context
4. EXECUTE      the execution layer performs the tool / API interaction
5. EVALUATE     reinforcement learning turns the outcome into a reward signal
6. RECORD       the event lands in the Tier 1 episodic buffer
7. CONSOLIDATE a decoupled worker compresses episodes into Tier 2 facts,
                decays stale salience and prunes forgotten knowledge
```

## System architecture

| Module | Responsibility |
|---|---|
| `decision_engine.py` | Orchestrates models, ethics, memory and execution; entry point |
| `model_manager.py` | Model registry, capability-based selection, performance tracking |
| `subconscious.py` | Model output generation (Ollama integration) and weight adaptation |
| `execution_layer.py` | Executes decisions against tools/APIs |
| `reinforcement_learning.py` | Reward signals and model weight updates from outcomes |
| `task_manager.py` | Queued task execution with callbacks |
| `autonomy.py` | Autonomous task generation / learning cycles |
| `vector_db.py` | Optional ChromaDB vector store (stub mode without it) |
| `logging_manager.py` | Structured logging of tasks and decisions |
| `memory_system.py` | Four-tier memory architecture (below) |
| `security.py` | Secrets, redaction, URL validation, rate limiting, circuit breaker |
| `policy.py` | Capability registry, approval broker, consent registry |
| `human_interaction.py` | Human Interaction contract: observation/response schema, bounded interaction bus, presence state machine |
| `runtime/VisionProvider.kt` (Android) | v1.13 camera perception: ~1 fps front-camera person presence into the interaction bus |
| `runtime/AudioProvider.kt` (Android) | v1.14 hearing: energy VAD gating on-device STT; transcript observations into the interaction bus |
| `runtime/TtsProvider.kt` (Android) | v1.15 speech: local TTS output with bounded queue and VAD barge-in; the `speak` action's Kotlin executor |
| `audit.py` | Tamper-evident hash-chained audit log |
| `model_backends.py` | Pluggable model adapters (Ollama HTTP, OpenAI-compatible, stub) |
| `ros2_interface.py` | ROS 2 abstraction: abstract message types, Twist sanitization, stub and rclpy implementations |
| `android_bridge.py` | Android ↔ ROS 2 transports: JavaBridge (Chaquopy + jros2/Fast-DDS) and rosbridge (WebSocket); JSON payload codec |
| `android_runtime.py` | Android app lifecycle (`onCreate`/`onPause`/…), power + thermal monitor → fallback triggers, Keystore-backed secrets |
| `android_node.py` | On-device node roles (sensor / compute / operator / full_agent) and local llama.cpp launcher detection |
| `mobile_nodes.py` | Host-side mobile fleet: pairing with TTL, topic ACL, compute offload broker, clamped teleop relay |
| `shugonet_bridge.py` | Multi-agent networking via Shogunet: send/query/sync actions, fleet memory mesh |
| `fleet_deploy.py` | Host-only fleet rollout: consent- and approval-gated ADB installs onto operator-allowlisted devices, with artifact-root, digest and audit enforcement |
| `acceleration.py` | Hardware acceleration ladder (NPU → DSP → GPU → CPU) with thermal demotion and failure degradation |
| `robotics_handler.py` | Robotics execution handler: verified Twist/trajectory dispatch, emergency stop, watchdog |
| `state_machine.py` | Strict interlocks for the observation-action loop |
| `fallbacks.py` | Deterministic fallback controller (stall / budget / breaker triggers → safe state) |
| `gazebo_simulation.py` | Gazebo/Ignition simulation layer |
| `moveit_planner.py` | MoveIt 2 motion planning layer |
| `simulation/` | Physics simulation framework: MuJoCo backend, robot models, test scenarios |
| `telemetry.py` | Telemetry hooks |
| `token_budget.py` | Context budgeting |
| `android_inference.py` | Android local inference backend (OpenAI-compatible) |
| `shugocore_server.py` | Desktop server: Ollama wire contract + engine API (`shugocore-server`) |
| `platforms/android/` | Android node app: 5-tab control-plane UI, embedded llama.cpp (JNI) + Ollama-compatible local API, foreground service, bundled Python agent tree |
| `clients/desktop/` | Desktop control plane: backend/model chooser + node dashboard (SERVER / AGENT / ACTIVITY / SENSORS / SECURITY / LOG) |
| `scripts/desktop_agent.py` | Headless host node: agent + Tier 2 + ShugoNet mesh (`mesh_peers.json` or `SHUGOCORE_MESH_PEERS`) |

## Memory architecture

```
[ Tier 0: Scratchpad / Working Memory ]  <-- Unfiltered Token Stream (In-Context)
                  |
                  v (Consolidation Pipeline)
[ Tier 1: Episodic / Short-Term Memory ]  <-- Event Logs, Recent Tool Execution (FIFO / Sliding)
                  |
                  v (Decay & Summarization Engine)
[ Tier 2: Semantic / Long-Term Memory ]  <-- Entity Maps, Consolidated Facts (SQLite + Vectors)
                  |
                  v (Abstraction / Generalization)
[ Tier 3: Core Identity & World Model ]   <-- System Invariants, Permanent Rules (Read-Only)
```

| Tier | Class | Purpose | Lifetime |
|---|---|---|---|
| 0 | `memory_system.Scratchpad` | Active context, step-by-step reasoning tokens, instantaneous sensory/API inputs | Milliseconds-minutes; flushed on task-step resolution |
| 1 | `memory_system.EpisodicMemory` | Exact sequence of recent actions, tool outputs, environmental responses | Hours-days; session-bounded JSON ring buffer |
| 2 | `memory_system.SemanticMemory` | Consolidated learnings, success/failure patterns, historical interactions | Semi-permanent; SQLite facts + embeddings |
| 3 | `memory_system.CoreIdentity` | Hard constraints, safety boundaries, fundamental environmental rules | Permanent; read-only during standard execution |

### Memory dynamics

- **Active consolidation (compression):** episodic events are drained and
  summarized into compact semantic facts in Tier 2; raw logs are flushed.
- **Decay & pruning (forgetting):** Tier 2 salience decays exponentially
  since last access; re-accessed memories are reinforced on retrieval and
  low-salience memories are pruned.
- **Selective promotion:** critical failure modes and recurring patterns in
  Tier 1 are promoted into Tier 2 as high-salience procedural insights.
- **Tier 2 -> Tier 3 review:** `MemoryManager.review_promotion_candidates()`
  surfaces frequently re-accessed, high-salience facts; elevation into the
  world model stays an explicit privileged step (`promote_to_core`).

### Isolation model

- Tier 0 / Tier 1 are created per `MemoryManager` (per-agent isolation -
  no cross-task context contamination).
- Tier 2 / Tier 3 are shareable: pass the same `SemanticMemory` /
  `CoreIdentity` instances into multiple `MemoryManager`s so planning
  nodes see one consistent world model.
- Consolidation, decay and pruning run in a daemon worker thread and never
  block the observation-action loop; use `consolidate_now()` for
  deterministic, synchronous control.

## Safety model

Functional agency must be bounded by rules the agent cannot rewrite in the
course of acting. Enforcement is layered, so bypassing any single component
defeats nothing:

| Layer | Enforcement |
|---|---|
| Tier 3 world model | Immutable invariants (`no_harm`, `consent_required`, `no_manipulation`, `privacy`, `auditability`) evaluated before any model call or execution |
| `ConsentRegistry` | Consent-gated actions (side-effecting, robotics, mobile, network, fleet) require operator-issued grants over `GET/POST /api/v1/consent` - a `consent` flag written by the acting agent itself is never trusted |
| `ApprovalBroker` | Side effects additionally require human approval; fail-closed (no operator channel attached, or TTL expiry, means denied) |
| Policy verdict token | The engine binds an allow verdict to the canonical hash of the exact decision; the execution layer refuses missing, non-allow, or mismatched tokens |
| `CapabilityRegistry` | https-only egress, host allowlists, HTTP-method allowlists, SQL statement-type allowlists, empty-by-default hardware command allowlists |
| Egress controls | Mandatory timeouts, per-host rate limiting, circuit breakers, response size caps, redirects disabled |
| Hash-chained audit log | Every block, approval and execution is appended to a tamper-evident JSONL chain - verify with `python3 audit.py verify audit_chain.jsonl`; optionally HMAC-signed with an operator key for fleet-shared storage |
| Secret hygiene | API keys resolved from environment variables at execution time, never carried in decision dicts; every log record passes a redaction filter |
| Honest execution | Unimplemented side-effecting actions return `not_implemented` - never simulated success - so the reinforcement signal cannot reward no-ops |
| Mobile fleet isolation | Android publishers are confined to `/shugocore/mobile/#`; `operator_node` teleop is clamped and relayed - phones never write actuation topics |
| Local inference confinement | On-device model endpoints must be loopback HTTP(S) on an allowlisted port (`CapabilityRegistry.validate_model_endpoint`) |
| Desktop server exposure | `shugocore-server` binds loopback by default and refuses a non-loopback bind unless `SHUGOCORE_SERVER_TOKEN` is set (constant-time bearer auth on every route except `/health`) or `--allow-unauthenticated` is passed; per-client rate limiting, 1 MB body cap, loopback-only CORS |
| Mesh transport bounds | ShugoNet peer runtime caps inbound NDJSON frames (1 MiB), validates message shape before dispatch, and supports an optional shared-secret mesh token |
| Backend egress validation | Model backends accept only http(s) URLs with a host and no embedded credentials, never follow redirects, and cap responses at 256 KiB |

Key properties:

- **Single gated path.** Interactive tasks, autonomous cycles and the task
  queue all execute through `DecisionEngine.execute_task` - the autonomous
  loop cannot bypass the gate.
- **Read-only at runtime.** The world model changes only through the
  privileged `promote_to_core()` path, which requires operator attribution
  (`authorized_by=`) and appends to the Tier 3 ledger.
- **Fail-closed everywhere.** Missing verdict, missing consent, missing
 approval channel, unknown host, unknown command - all refuse.

## Android & mobile compute nodes

ShugoCore runs on Android as a first-class ROS 2 participant (v1.1.0). One
phone can be a sensor, an offload target, a teleop pendant, or a fully
offline agent - the role is a config switch, and the same gated
`DecisionEngine` path runs everywhere.

| Role | What the device does |
|---|---|
| `sensor_node` | Publishes IMU / GPS / battery / heartbeat under `/shugocore/mobile/<device>/…` |
| `compute_node` | Accepts `/compute_request` jobs, runs LiteRT/NNAPI inference, publishes results |
| `operator_node` | Clamped teleop pendant - relays bounded velocity commands only |
| `full_agent` | Full loopback agent on an offline llama.cpp / Ollama / LM Studio launcher |

Transports: in-process `JavaBridgeROS2Interface` (Chaquopy + jros2 /
Fast-DDS) or `RosBridgeInterface` (rosbridge JSON-over-WebSocket, the Termux
path). `AndroidRuntime` maps the Android app lifecycle onto the agent,
streams power and thermal state into the fallback controller (sustained
thermal elevation pauses compute), and resolves secrets from the Android
Keystore.

Accelerator selection is a policy, not a constant: `acceleration.py`
prefers NPU → DSP → GPU → CPU per workload, degrades on device failure and
demotes to CPU-only at thermal level 2 - always with a deterministic CPU
fallback, so nothing breaks on hardware without an NPU.

The Kotlin-side reference client (bridge contract + Gradle app shell) lives
in [`clients/android/`](clients/android/); the full integration guide - wire
topics, security model, launcher matrix, SoC accelerator cheat-sheet, Termux
fallback - is [`docs/android_integration.md`](docs/android_integration.md).

On-device model execution runs against the app's **embedded llama.cpp
engine** (JNI, no external launcher required) or standard local launchers:
ShugoCore probes llama.cpp (`/health`), Ollama (`/api/tags`) and LM Studio
(`/v1/models`), and every backend URL must pass the loopback + port
allowlist check before the first prompt leaves the process.

### Android node control plane (v1.9.0)

The Android app is the management console for an embodied AI node: an
always-visible **NODE STATUS** header (agent online, model, inference mode,
memory, sensors n-of-n, human, policy, network, temperature, battery) above
**SERVER / AGENT / SENSORS / SECURITY / LOG** tabs. Every row renders real
runtime state via a `getNodeSnapshot()` service binder call — no decorative
indicators.

- **SERVER** — the embedded llama.cpp engine serving an Ollama-compatible
  API on `127.0.0.1:11434` (native libraries are 16 KB page-size aligned for
  Android 15+ devices), four health indicators (`MODEL LOADED / INFERENCE
  READY / API READY / AGENT READY`), live request/token/latency counters, a
  GGUF model catalog with download/select, and a **backup-server fallback**:
  stopping the local stack while a desktop backup URL is configured re-points
  the agent at the backup and keeps it running. A **MODEL TEST** panel runs
  one controlled decision round-trip and shows Parse VALID/INVALID, the
  failure class (no response / invalid protocol / valid protocol), latency
  and the truncated raw response.
- **AGENT** — subsystem statuses, the current cycle with the last
  decision / action / evaluation taken from the Tier 1 head, the
  OBSERVE→GATE→DECIDE→EXECUTE→EVALUATE→RECORD→CONSOLIDATE pipeline with only
  the stages that actually ran highlighted, and memory tiers 0–3.
- **SENSORS** — a capability contract, not a permissions page: Android
  hardware → runtime permission → capability declaration → live stream →
  explicit agent acknowledgement, so the agent never assumes a capability
  exists just because Android granted a permission.
- **SECURITY** — Android permissions vs. agent authority shown as separate
  states, network posture with real enforcement (loopback always; LAN /
  internet fail-closed by default), tool allowlists, and the policy flags
  (fail-closed, audit, consent).
- **LOG** — a live category-filtered feed (MODEL / AGENT / SENSOR / POLICY /
  MEMORY / ERROR) backed by a ring-buffer log bus that both the Kotlin
  runtime and the Python agent write to.

Every DECIDE cycle terminates in a record, formalized as an **outcome
contract** — `SUCCESS`, `NO_ACTION` (a model that answers but proposes
nothing executable is a healthy, recorded cycle, not an error),
`POLICY_BLOCK`, `GOVERNOR_BLOCK`, `TASK_FAILURE`, `BACKEND_FAILURE` (the
model ensemble was unreachable), `IN_FLIGHT`, `ENGINE_FAILURE` — and the
stage trail shows only the stages that actually ran. The decision prompt
itself is **generated from the engine's real action schema**
(`available_action_types()`), so the model can only ever be offered actions
policy and execution actually support — including the always-safe
`record_observation` internal action, which is also the rule-based
fallback's target after three consecutive unproductive model cycles. The
Kotlin↔Python boundary speaks JSON (`get_status_json`, `recent_logs_json`,
`update_capabilities_json`, …) so no state is lost to crossing the runtime
edge.

### On-device structured inference (v1.30.0)

The Android node runs the model locally, and constrains it to a schema:

- **Model staging is required.** `ShugoCoreService` searches `files/models/`,
  the app cache and `/data/local/tmp`, staging whatever `.gguf` it finds into
  app-private storage. With no model the loopback server never starts and
  every model call fails (`URLError`), leaving the rule-based fallback.
- **Grammar-constrained decoding.** For decision tasks the engine builds a
  GBNF grammar from its own `available_action_types()` and ships it with the
  request; `LocalApiServer` forwards it to llama.cpp's grammar sampler, so a
  small on-device model cannot emit the loose JSON dialects that used to fail
  parsing (and a replayed grammar step can never be silently skipped).
  Conversational output stays free text. Ollama's `format: "json"` is also
  honoured, mapped to a generic JSON-object grammar.
- **One APK, self-determining.** The build ships *every* arm64 CPU kernel
  variant (portable `armv8.0`, dotprod, dotprod+fp16, i8mm, sve, sme) as its
  own dlopen'ed backend library; at startup ggml scores each against the
  running CPU (`getauxval(AT_HWCAP[2])`) and loads the best. So the same APK
  runs the portable kernels on an Exynos 9611 and the dotprod+fp16 set on an
  Exynos 1380-class device — no per-device builds, no `SIGILL`. (Force the
  historical single-arch layout with
  `./gradlew assembleDebug -Pshugocore.singlearch=true`.)
- **Measured.** A51 (Exynos 9611, 0.5B Q4_K_M, portable variant): ~30
  s/decision. Tab S9 FE (dotprod+fp16 variant, 1.5B Q4_K_M): ~23 s/decision.

### Desktop server mode (no high-end phone needed)

Users with a device older than the 2020 mid/high-tier recommendation can run
the full agent on a desktop (macOS / Linux / Windows) and pair the phone as a
lightweight node. The phone's `AndroidBackend` points at the desktop via the
"Desktop server URL" field in the app - zero client protocol changes, because
the desktop speaks the same Ollama wire contract plus the engine API.

```bash
pip install shugocore
# Non-loopback binds are an explicit decision: the app sends no token yet, so
# LAN pairing uses the acknowledgement flag (see docs/desktop_server.md).
shugocore-server --backend ollama --model qwen3.5:latest \
    --host 0.0.0.0 --allow-unauthenticated
```

The server is **loopback by default** and refuses a non-loopback bind unless
`SHUGOCORE_SERVER_TOKEN` is set (bearer auth on every route except `/health`)
or `--allow-unauthenticated` is passed. Requests are per-client rate limited,
bodies are capped at 1 MB, and CORS preflight answers loopback origins only.

Endpoints on the server:

| Endpoint | Purpose |
|---|---|
| `POST /api/generate`, `/api/chat` | Ollama wire contract (what `AndroidBackend` calls) |
| `GET /api/tags`, `/health` | Model listing + readiness |
| `GET /api/v1/status` | Engine / governor / fallback / memory state |
| `POST /api/v1/task` | Policy-gated `execute_task` over the network |

Backends: `ollama` (default), `llamacpp`, `openai`, `stub` (offline tests).
Full setup for each OS: [`docs/desktop_server.md`](docs/desktop_server.md).

## Multi-agent networking with Shogunet

ShugoCore integrates with [Shogunet](https://github.com/SamurAI-Official/Shogunet)
for networking between multiple Shugocore agents. This enables fleet-wide
collaboration over 5G, 4G, WiFi, LoRa, and Bluetooth networks with a
codependent memory mesh.

### Network action types

| Action | Type | Description |
|---|---|---|
| `network_send` | Side-effecting | Send a message/request to a peer agent |
| `network_query` | Side-effecting | Query the fleet's memory mesh |
| `network_sync` | Side-effecting | Sync facts / digest exchange |
| `network_list_agents` | Read-only | List paired agents in the fleet |
| `network_status` | Read-only | Get network health/status |

Side-effecting network actions require operator consent and approval, following
the same pattern as other side-effecting actions.

### Mesh primary election (Track 1)

Exactly one live node holds the primary lease and runs the loop's side effects;
every other live node falls back to peripheral mode (sensors, journal, RPC
offload, never speaks). Candidates are ranked by heartbeat advertisement --
lower `--mesh-priority` wins, ties broken by node id -- and a node is excluded
when it is unpaired, thermally critical (status >= 3) or reports no memory
headroom.

Advertisements travel over the ShugoNet mesh itself
(`{"type": "heartbeat", "from": ..., "payload": {...}}`; fire-and-forget, never
acked, stamped with the mesh token when one is configured) as well as the Android
DDS path, so a Python-only fleet elects a primary too. Every received
advertisement is merged into `telemetry['mesh_peers']` and evaluated immediately,
so the role tracks the fleet within one heartbeat interval. Hosts with no
telemetry measure their own free memory (`mesh_election.available_memory_bytes()`),
because the election refuses a candidate advertising zero headroom.

### Fleet deployment action types

| Action | Type | Description |
|---|---|---|
| `fleet_deploy` | Side-effecting | Install a signed APK onto operator-allowlisted devices over ADB |
| `fleet_status` | Read-only | List attached devices and the build each one runs |

`fleet_deploy` is **host-only** - `fleet_deploy.py` is deliberately not part of
the Android bundle, so a phone never learns the action exists - and **off by
default**: the host launcher registers it only when it is given
`--deploy-target SERIAL` (repeatable). The decision engine consent-gates it, the
approval broker adds its human gate, and the handler refuses, before touching any
device: a non-`.apk` artifact, an artifact outside the operator's artifact root,
a digest that does not match the supplied `sha256`, a target outside the
operator's serial allowlist, or more targets than the rollout bound. Every
attempt, including every refusal, is appended to the audit chain.

Use `dry_run` to plan a rollout without installing, and `expect_version` to make
the handler verify the version the device reports afterwards:

```json
{"action_type": "fleet_deploy",
 "params": {"artifact": "…/app-debug.apk", "sha256": "…",
            "targets": ["<adb-serial>"], "expect_version": "1.30.5"}}
```

A rollout only upgrades in place if every device trusts the signing key; a
mismatch (`INSTALL_FAILED_UPDATE_INCOMPATIBLE`) is reported per target and the
device keeps its data - see the fleet-key notes in `CHANGELOG.md`.

### Operator consent surface

`ConsentRegistry` gates side-effecting, robotics, mobile, network and fleet
actions, and a grant may only come from an operator - the acting agent may never
assert its own consent. The operator channel is the consent surface:

| Route | Purpose |
|---|---|
| `GET /api/v1/consent` | Grants currently in force (bounded, sanitized) |
| `POST /api/v1/consent/<action_type>` | Issue a grant; JSON body may carry `ttl_seconds` (capped at 24 h), `granted_by`, `scope`, `note` |
| `POST /api/v1/consent/<action_type>/revoke` | Drop every grant for the type |

Only `policy`'s consent-gated families may be granted; anything else is refused,
and if `policy` cannot be imported nothing is grantable (fail-closed). Bearer
auth and rate limiting apply exactly as they do to `/api/v1/approvals`. Example:

```bash
curl -s -X POST http://127.0.0.1:11435/api/v1/consent/network_send \
     -H 'Authorization: Bearer <token>' -H 'Content-Type: application/json' \
     -d '{"granted_by": "operator", "ttl_seconds": 3600}'
```

The desktop control plane exposes the same thing in its SECURITY pane, and
`GET /api/v1/security` reports the grants in force (from the registry the gate
actually consults).

### Memory mesh semantics (v1.30.0)

`network_query` and `network_sync` are backed by each agent's **living Tier 2
memory** — not placeholders:

- **`query`** answers from the peer's `MemoryManager` (hybrid semantic +
  entity-graph recall). With no memory backend configured the reply is empty,
  never invented.
- **`sync`** is incremental: the caller passes a watermark, the peer returns
  the Tier 2 facts created after it, and the caller merges them into its own
  store and advances a per-peer watermark — so repeats transfer nothing.
- **Only Tier 2 crosses the mesh.** Tier 0/1 are per-agent private and Tier 3
  is read-only identity; imports are idempotent and carry `shared_from` /
  `shared_at` provenance, so the origin of every belief is auditable.
- **Bounded links.** Peers are re-dialed by `reconnect_peers()` (periodic and
  bounded), so a startup dial race cannot strand a link; duplicate-heavy syncs
  trip the `memory_sync_conflict_storm` guard.
- **Joining a mesh.** Host nodes set `SHUGOCORE_MESH_PEERS`
  (`id=host:port,...`); Android nodes drop `mesh_peers.json`
  (`{"peer-id": "host:port"}`) into the app data dir. When any node runs
  with `SHUGOCORE_MESH_TOKEN`, every other node needs the same token:
  hosts via the env var, Android nodes via `mesh_token.txt` (single line)
  in the app data dir next to `mesh_peers.json`. On device, the
  deterministic command **"sync your memory with your peer"** merges a peer's
  memory and reports the imported count.

Verified across two devices (A51 + Tab S9 FE): the A51 imported **64** facts
from its peer and the Tab imported 2 in the opposite direction, with the
imported knowledge then recallable through each agent's own memory API.

### Build transfer over the mesh (v1.30.7)

The same transport ships *builds*, so a node that makes one can hand it to the
hive without an ADB cable:

- **One shared directory per node.** `--share-dir` (or
  `SHUGOCORE_ARTIFACT_ROOT`, default `<data-dir>/shared`) is what this node
  offers; `--artifact-dir` / `SHUGOCORE_ARTIFACT_DIR` (default
  `<data-dir>/artifacts`) is where builds it receives land. Artifacts are
  addressed by **bare file name** only, and the real path is re-checked for
  containment, so `../..` or a symlink reaches nothing outside the share root.
- **Pull:** `--artifact-fetch app-debug.apk=shugo-desktop` (or the
  `mesh_artifact_fetch` tool) pulls the build in verified chunks. The manifest
  digest is checked before the first byte is written and the assembled file is
  digested again before it is renamed into place — a partial transfer is deleted,
  never promoted.
- **Push:** `--artifact-offer app-debug.apk=shugo-mac` (or
  `mesh_artifact_offer`) sends name/size/digest; the *receiver* pulls the bytes,
  so no node ever writes on a peer's say-so alone. The receiver must know the
  offerer as a peer (`SHUGOCORE_MESH_PEERS` / `mesh_peers.json`).
- **Bounds and evidence.** One transfer is capped (256 MiB) and chunked
  (192 KiB); every request is behind the mesh token; receipts are in
  `status()["artifacts"]` / `artifact_receipts()` and
  `mesh_artifact_received|offered|failed` events go to the node's audit chain.
- **Joining laptop / Mac:** pull the repo, set the mesh token and peers, then

  ```bash
  python3 scripts/desktop_agent.py --device-caps mac --data-dir ~/.shugocore \
      --mesh-priority 20 --sync all \
      --artifact-fetch app-debug.apk=shugo-desktop
  ```

  which lands the current build in `~/.shugocore/artifacts/`, digest-verified.
  Verified end to end on the real hive: the desktop shared its
  `app-debug.apk` (78,117,673 bytes, sha256 `d4ce2339…`), a plain mesh node
  pulled it in **398** verified chunks bit-for-bit, and an offer in the other
  direction was audited by the hub (`artifacts=1 art_in=1`).

### Quickstart

```python
from decision_engine import DecisionEngine
from shugonet_bridge import (
    ShugonetExecutionHandler,
    register_network_handlers,
   attach_network_fallbacks,
)
from agent_runtime import ShugonetAgentRuntime

# Create and connect the Shogunet runtime
runtime = ShugonetAgentRuntime(
   agent_id="agent-001",
    host_tcp_host="127.0.0.1",
    host_tcp_port=9000,
    host_relay_url="http://127.0.0.1:9001",
)
runtime.connect_to_host()

# Create the execution handler
shogonet_handler = ShugonetExecutionHandler(runtime)

# Create the decision engine with the network handler
engine = DecisionEngine(
    models=models,
    vector_db_config={'type': 'chroma'},
    shogonet_handler=shogonet_handler,
)

# Or register handlers after engine creation
register_network_handlers(engine.execution_layer, runtime)
attach_network_fallbacks(engine.fallbacks)
```

### Network fallback triggers

| Trigger | Severity | Description |
|---|---|---|
| `network_transport_exhausted` | pause | All transports failed |
| `network_peer_lost` | pause | A paired agent disconnected |
| `memory_sync_conflict_storm` | safe_state | Excessive sync conflicts |
| `audit_chain_broken` | halt | Audit integrity failure |

### Link-cycle correlation (`scripts/netwatch.py`)

The triggers above only exist while the node runtime is running, so an outage
that happens between runs leaves no ShugoCore-side trace to correlate. The
stdlib-only `scripts/netwatch.py` sampler records a 1 Hz link timeline so a
suspected "network cycle" (a gateway re-publishing its RA/RDNSS/DHCPv6 data, a
NAT64/DNS64 re-provision, a relay or VPN drop, or a software-update retry
storm) is measured instead of guessed at:

```bash
# sample the local engine + Apple's update host for an hour
python3 scripts/netwatch.py --jsonl /tmp/netwatch.jsonl \
    --dns-name swscan.apple.com --duration 3600

# what the node saw, second by second
python3 -c "import json; print([r['payload']['state'] for r in \
    map(json.loads, open('/tmp/netwatch.jsonl')) if r['type'] == 'netwatch_tick'])"

# what the OS said at the same second
grep -n 'NSURLErrorDomain Code=-1009' /var/log/install.log
```

Every tick probes DNS (`--dns-name`), the node's `host:port` (`--target`), one
or more numeric endpoints (`--internet`, comma-separated) and optionally the
gateway (`--gateway-port`). States, in precedence order:

| State | Meaning | Record emitted |
|---|---|---|
| `no_route` | No local address / no default route -- the literal cycle | `network_cycle` |
| `transport_exhausted` | Nothing on any configured endpoint answered | `network_transport_exhausted` (`pause`) |
| `dns_failure` | Resolution failed while the path still answered | `network_cycle` |
| `degraded` | All probes answered, one exceeded `--slow-ms` | `netwatch_tick` |
| `ok` | Everything answered promptly | `netwatch_tick` |

Endpoints are judged `open`, `refused` or `unreachable`, and **a refusal counts
as the path working** -- a reset means the packet arrived and something
answered, so a node whose `--target` port is simply closed is not reported as
an outage. The `--internet` default therefore spans two operators, because
carrier/CGNAT gateways routinely reset TCP to addresses they intercept and a
single blocked address must not look like a total outage. A loopback `--target`
is recorded as `target_authoritative: false` and is never treated as evidence
about the link.

`dns_failure` is the signature of a gateway re-publishing its RA/RDNSS/DHCPv6
data (and of NAT64 re-provisioning): established flows keep working while every
*new* connection fails, which is why a download or upgrade job notices first
while the rest of the machine looks fine.

Records reuse the node audit chain's field names (`timestamp` / `type` /
`payload` / `seq`) so the timeline lines up with `node_audit.jsonl`, but they
are **plain JSONL**: unlike `audit.py` records they are not hash-chained and
must not be treated as tamper-evident. Exit status is `1` when a cycle was
seen, so the sampler can gate a soak run or a cron job.

### Integration architecture

The `shugonet_bridge.py` module follows the same pattern as `robotics_handler.py`
and `mobile_nodes.py`:

1. **Action types** are defined in `policy.py` and added to `KNOWN_ACTION_TYPES`
2. **Execution handler** dispatches network actions to the Shogunet runtime
3. **Fallback severities** are registered in the deterministic fallback controller
4. **Handler registration** occurs during `DecisionEngine` initialization

The bridge uses duck-typed contracts, so the Shogunet runtime can be swapped
with any compatible implementation.

## Continuous agent daemon

The "press go" entry point for continuous synthetic functional agency::

    python3 continuous_agent.py --interval 2.0 --max-iterations 1000

This runs the README orchestration loop (OBSERVE → GATE → DECIDE →
EXECUTE → EVALUATE → RECORD → CONSOLIDATE) as a bounded daemon:

- **Iterations** - `--max-iterations` caps loop passes (unlimited by default).
- **Deadline**  - `--max-seconds` caps wall-clock runtime.

- **Budget**    - every task flows through the governor's per-task step budget
 and deadline; a pathological task cannot stall the loop.

- **Safety**    - the fallback controller can latch PAUSED / SAFE_STATE /
  HALTED at any moment; when  PAUSED the loop sleeps instead of spamming
  refusals.
 Loop errors are reported to the fallback controller as
  `continuous_loop_error` triggers, so a stuck loop latches a safe
  state deterministically.


From Python::

    from continuous_agent import ContinuousAgent
   agent = ContinuousAgent(models=[...], interval=2.0, max_iterations=100)
   agent.start()
   agent.await_stop()

The daemon emits telemetry spans per loop pass (via `telemetry.py`) and
exposes a `status()` snapshot (loops, tasks, successes, failures,
governor state, fallback mode, Tier 1 backlog, Tier 2 fact count)
for dashboards and the Shogunet network bridge.



## Verifiable embeddings & privacy hardening

- **Ethics checks are now real**: `can_explain` requires a non-stub model
 and a writable audit chain; `detect_bias` scans task content for
  sensitive-attribute stereotypes and loaded language;`is_privacy_compliant`
  requires audit capability and rejects secret-key leakage in content;
  `can_audit` requires an operational audit chain (an empty fresh chain is
 auditable; a missing file is not). The policy gate enforces all four
  before any model call or execution.

- **Deterministic hashing embeddings**: `vector_db.hashed_embedding()`
  replaces the all-zero placeholder vectors with a dependency-free hashing
  bag-of-words embedding (matching `SemanticMemory._embed`，so
  environment observations are now actually searchable in cosine space
 an injected `embedding_fn` lets callers plug in real embedding models.

## Fleet-shared memory (PostgreSQL + pgvector)

By default Tier 2 semantic memory is local SQLite (`semantic_memory.db`).
For multi-agent deployments - the persistence half of the Shogunet memory
mesh - several agents can share one PostgreSQL-backed knowledge base::

    pip install 'shugocore[postgres]'

Then point `memory_db_path` at a DSN (a one-line config change; the engine
routes it through `pg_memory.open_semantic_memory()`)::

    from pg_memory import open_semantic_memory
    from decision_engine import DecisionEngine

    engine = DecisionEngine(models=models,
                            memory_db_path="postgresql://user:pw@db:5432/fleet")

- **Drop-in parity** - `PgSemanticMemory` implements the full `SemanticMemory`
  surface (facts, entity graph, salience reinforce/decay/prune), so it can
  also be passed explicitly via `DecisionEngine(semantic_memory=...)` or
  `MemoryManager(semantic=...)`.
- **Embedding parity** - identical deterministic hashing embeddings to the
  SQLite backend, so facts written by one agent on one backend are
  retrievable with the same similarity scores by another agent on the other.
- **Server-side vector search** - cosine distance is computed by pgvector
  (`<=>`); for fleet scale add an HNSW index (recipe in the module
  docstring).
- **Fail-closed** - missing psycopg2 or an unavailable pgvector extension
  raises with actionable instructions at construction; there is no silent
  local-fallback that would pretend to persist fleet memory.
- **Tier 2 only** - Tier 0/1 stay per-agent and Tier 3 stays read-only
  local, preserving the memory invariants (N0-N1) across the fleet.

One-time server prerequisite: `CREATE EXTENSION vector;`
([pgvector](https://github.com/pgvector/pgvector)).

## Installation

Requires Python 3.9+.

**From PyPI (recommended):**

```bash
pip install shugocore
```

**From the GitHub release (identical artifacts):**

```bash
pip install https://github.com/SamurAI-Official/ShugoCore/releases/download/v1.10.0/shugocore-1.10.0-py3-none-any.whl
```

**From source:**

```bash
git clone https://github.com/SamurAI-Official/ShugoCore.git
cd ShugoCore
pip install -r requirements.txt          # core dependency: requests
python decision_engine.py                # run the built-in demo
```

Optional extras:

- `websocket-client` - rosbridge (WebSocket) transport for Termux-based Android nodes
- `torch` - enables CUDA/accelerated device selection (CPU-only mode without it)
- `chromadb` - enables persistent vector storage in `vector_db.py` (stub mode without it)
- `shugonet` - Shogunet networking runtime for multi-agent fleets (`shugonet_bridge.py`)
- `psycopg2-binary` - PostgreSQL + pgvector fleet-shared Tier 2 memory (`pg_memory.py`)

> Published on [PyPI](https://pypi.org/project/shugocore/) - the `v1.10.0`
> wheel built from this tree is also attached to the
> [GitHub release](https://github.com/SamurAI-Official/ShugoCore/releases/tag/v1.10.0).

## Quickstart

```python
from decision_engine import DecisionEngine

models = [
    {'id': 'gpt-4', 'type': 'text', 'weight': 0.5, 'backend': {'type': 'stub'}},
    {'id': 'deepseek', 'type': 'text', 'weight': 0.3, 'backend': {'type': 'ollama'}},
    {'id': 'llama', 'type': 'text', 'weight': 0.2, 'backend': {'type': 'ollama'}},
]

engine = DecisionEngine(
    models=models,
    vector_db_config={'type': 'chroma'},   # stub mode without chromadb
    news_api_key=None,                     # or set SHUGOCORE_NEWS_API_KEY
    memory_db_path='semantic_memory.db',   # Tier 2 storage
   audit_path='audit_chain.jsonl',        # tamper-evident audit chain
)

# Tier 3 invariants gate every task before execution
result = engine.execute_task({'type': 'test', 'content': 'say hello'})

# Side-effecting actions need an operator consent grant AND an approval:
engine.consents.grant('api_call', granted_by='operator')
engine.approvals.attach_operator(lambda request: True)  # operator channel

# Decisions carry long-term context retrieved from Tier 2
decision = engine.make_decision({'type': 'test', 'content': 'say hello'})
print(decision['memory_context'])

engine.shutdown()  # flushes episodic memory into Tier 2, stops maintenance worker
```

### Autonomous operation

```python
# Generate, execute, learn, consolidate - with a hard iteration cap
tasks = [engine.autonomy.generate_task("test", "collect environment readings")]
engine.autonomy.autonomous_learning_cycle(tasks, max_iterations=10)

# Adapt to new environment data; observations persist in Tier 2
engine.autonomy.adapt_to_environment({"mode": "field", "temperature": 22})

# Review which Tier 2 facts proved durable enough to become permanent rules
candidates = engine.memory.review_promotion_candidates(min_salience=2.0,
                                                       min_access_count=3)
for fact in candidates:
    print(fact["content"], fact["salience"], fact["access_count"])
    # Promotion is an explicit, operator-attributed privileged step:
    # engine.memory.promote_to_core("rule_key", "operator-approved rule",
    #                               authorized_by="operator")
```

## Testing

```bash
python -m unittest discover -s tests -v     # 1247 tests, no native deps
python -m compileall -q .                   # byte-compile every module
ruff check .                                # syntax errors + undefined names
bandit -q -r . -x ./.venv,./.llama_build,./platforms,./dist,./build,./tests -lll
```

CI runs the full suite on Python 3.9-3.13 plus a blocking `lint` job (`ruff`,
`E9`/`F821`) and a `security-scan` job (`bandit` high severity blocking, medium
advisory); dependency advisories are surfaced by `pip-audit`. See
[`SECURITY.md`](SECURITY.md) for the disclosure process and the invariants to
attack.

Beyond security and integration regression tests (v1.2.0), the suite includes
hardware-facing stress suites:

- **Lifecycle** - 200 create/pause/resume/destroy cycles, concurrent
  pause/resume races, monitor resilience against bridge exceptions and
  garbage sensor data, power edge cases (boundary thresholds, plugged-in
  overrides, non-numeric battery values), and 25 node start/stop cycles
 asserted leak-free at the thread level.
- **ROS 2 transports** - bridge death mid-run, post-shutdown publish,
  malformed and non-object JSON packets, concurrent publish bursts under the
  rate limiter, emergency-stop bypass, round-trip payload fidelity, and
  rosbridge reconnect after socket drop.
- **Thermal** - ladder demotion at each level, 1000-cycle oscillation soak,
  combined thermal + power violations, streak/recovery semantics.
- **Model execution** - launcher detection and generation exercised against a
  real loopback HTTP double (`tests/fake_llama_server.py`) that speaks the
  llama.cpp, Ollama and LM Studio wire protocols with injectable failure
  modes: hang, HTTP 500, malformed JSON, empty choices, artificial latency.
- **Android control plane** - the capability-acknowledgement contract, the
  Chaquopy-safe JSON boundary calls, DECIDE-cycle semantics (every cycle
  terminates in a record), policy enforcement points, and the agent's honest
  stage trail — exercised against the same Python tree that is bundled into
  the APK (`tests/test_android_control_plane.py`).

A `sensor_node` soak runs for 5 seconds at 20 Hz against the pure-Python fake
bridge and asserts monotonic heartbeats plus bounded, non-runaway output -
this is the startup-routine safety check for real hardware.

To validate model execution against a genuine llama.cpp server:

```bash
SHUGOCORE_LIVE_LLAMA_URL=http://127.0.0.1:8080 python -m unittest tests.test_live_llama -v
```

## Memory configuration

`MemoryManager` knobs (tuned when constructing `MemoryManager` directly;
`DecisionEngine` uses these defaults):

| Parameter | Default | Meaning |
|---|---|---|
| `consolidation_interval` | 10.0 s | Background worker tick |
| `consolidation_threshold` | 25 events | Episodic backlog that triggers consolidation |
| `failure_promotion_threshold` | 3 | Repeated failures promoted as procedural insights |
| `pattern_promotion_threshold` | 5 | Repeating events promoted as patterns |
| `decay_half_life_hours` | 72.0 | Salience half-life since last access |
| `prune_min_salience` | 0.05 | Deletion floor for decayed memories |

## Project structure

```
ShugoCore/
├── decision_engine.py        # orchestration entry point; single gated path
├── human_interaction.py      # human-interaction contract: observation bus + presence
├── autonomy.py               # autonomous task generation and learning cycles
├── model_manager.py          # model registry and performance tracking
├── subconscious.py           # structured-decision prompts via backends
├── model_backends.py         # Ollama HTTP / OpenAI-compatible / stub adapters
├── execution_layer.py        # verdict-verified, allowlisted execution
├── policy.py                 # capability registry, approval broker, consent
├── security.py               # secrets, redaction, rate limiting, breakers
├── audit.py                  # hash-chained audit log (+ verifier CLI)
├── reinforcement_learning.py # reward signals and weight updates
├── task_manager.py           # bounded queued task execution
├── state_machine.py          # execution interlocks (stall / budget / breaker)
├── fallbacks.py              # deterministic fallback controller
├── vector_db.py              # optional ChromaDB integration
├── logging_manager.py        # structured, redacted logging
├── memory_system.py          # four-tier memory architecture
├── ros2_interface.py         # ROS 2 abstraction (stub + rclpy)
├── robotics_handler.py       # robotics execution handler (e-stop, watchdog)
├── gazebo_simulation.py      # Gazebo/Ignition simulation layer
├── moveit_planner.py         # MoveIt 2 motion planning layer
├── simulation/               # physics simulation framework
│   ├── base.py               # BaseSimulation interface
│   ├── mujoco_sim.py         # MuJoCo physics backend
│   ├── stub_sim.py           # Deterministic stub fallback
│   ├── scenarios.py          # Standardized test scenarios
│   └── robots/               # Robot model definitions
│       ├── base.py           # RobotModel interface
│       ├── berkeley_humanoid_lite.py
│       ├── reachy2.py
│       └── unitree_g1.py
├── acceleration.py           # NPU / DSP / GPU / CPU accelerator policy
├── android_bridge.py         # JavaBridge + rosbridge transports, payload codec
├── android_runtime.py        # Android lifecycle, power/thermal monitor
├── android_node.py           # on-device node roles + launcher detection
├── mobile_nodes.py           # host-side mobile fleet management
├── shugonet_bridge.py        # multi-agent networking via Shogunet
├── telemetry.py              # telemetry hooks
├── token_budget.py           # context budgeting
├── version.py                # SemVer, frozen for the 1.x series
├── clients/android/          # reference Kotlin bridge client + Gradle shell
├── platforms/android/        # Android node app: 5-tab control-plane UI (ui/),
│                             #   runtime/ layer (log bus, node state, sensor
│                             #   capabilities), embedded llama.cpp JNI server,
│                             #   bundled Python agent (src/main/python)
├── docs/android_integration.md  # Android integration guide
├── tests/                    # security, integration & hardware-stress tests
└── requirements.txt
```

Runtime artifacts (`runtime/`: `semantic_memory.db`, the audit chain, logs and a
node's local `mesh_peers.json`) are local and gitignored.

## Roadmap

Done in v1.30.4:

- ✅ Pluggable embedding backends for Tier 2 (`Embedder` protocol; hashing
  default + optional sentence-transformer)
- ✅ PostgreSQL + pgvector storage option for shared multi-process
  deployments (`SHUGOCORE_MEMORY_DSN` / `SHUGOCORE_MEMORY_BACKEND=postgres`)
- ✅ Entity/relation graphs alongside vector similarity in Tier 2
  (`link_entities`, `query_subgraph` on both backends)
- ✅ Per-agent memory policies (`MemoryManager(policy=...)`:
  `shared_rw` / `shared_read` / `isolated`)
- ✅ Remote audit log shipping (`LogSink` Protocol + HTTPS/file sinks via
  `SHUGOCORE_AUDIT_HTTPS_URL` / `SHUGOCORE_AUDIT_FILE_PATH`)
- ✅ Human-approval console surface (`GET /api/v1/approvals`,
  `POST /api/v1/approvals/<id>/approve|deny`)
- ✅ Operator consent surface (`GET /api/v1/consent`,
  `POST /api/v1/consent/<action_type>`,
  `POST /api/v1/consent/<action_type>/revoke`) with TTLs, plus a
  SECURITY-pane Grant/Revoke control in the desktop control plane
- ✅ Fleet dashboard endpoint (`GET /api/v1/fleet`); a full desktop UI tab
  still needs building
- ✅ Bounded sensor live-stream endpoint (`GET /api/v1/sensors`); the
  on-device SENSORS tab live rendering still needs building
- ✅ Android client bearer-token support so a token-protected desktop
  server can be paired without `--allow-unauthenticated`
- ✅ Per-model backend pools with health-based routing (`BackendPool`)
- ✅ CI trusted publishing to PyPI via GitHub Actions
  (`.github/workflows/release.yml`, OIDC, no static token)
- ✅ Android llama.cpp-compatible host server for Termux (`TermuxLlamaServer`
  launcher helper; self-hosted path)
- ✅ NPU capability-probe rungs (Qualcomm Hexagon QNN / MediaTek APU) on the
  acceleration ladder — real-device bring-up against those rungs remains
- ✅ Bandit B608 fixed in `pg_memory.py` (identifier composition via
  `psycopg2.sql`; `bandit -lll` now clean)
- ✅ Signed release artifacts + SBOM publication (Sigstore + SPDX SBOM in the
  release workflow)

Remaining:

- Desktop UI tab / Android SENSORS live rendering for the fleet + sensor
  endpoints added above
- Operator approval surface on the Android SECURITY tab (the `ApprovalBroker`
  now exposes the pending queue over the server API; wiring it into the app's
  SECURITY tab is follow-on work)
- Deep NPU bring-up (QNN / MTK inference integrations against real silicon)
- Canonical desktop fleet dashboard UI (parsing the server-hosted
  `/api/v1/fleet`)

### In progress: distributed reasoning across nodes

- ✅ **Option 2 (layer split over llama.cpp RPC) proven on hardware.** A
  peripheral phone runs the vendored `ggml-rpc-server` (NDK arm64 build) and the
  host's `llama-server --rpc` assigns layers to it: with all 24 layers of a 0.5B
  on a Tab S9 FE the host's RSS fell **677 MB -> 206 MB** while the phone held
  **402 MB** resident. `mesh_rpc.py` carries the launcher (fail-closed, audited
  LAN exposure) and capacity planning from *measured* headroom;
  `docs/layer_split_rpc.md` holds the numbers and the constraints (the RPC
  socket is unauthenticated, the device advertises total rather than free
  memory, and throughput is transport-bound: 140 -> ~2 tok/s over Wi-Fi).
- ✅ **Packaged and running from the APK.** `SHUGOCORE_RPC_SERVER` builds the
  peripheral server for arm64-v8a and ships it as
  `lib/arm64-v8a/libshugocore_rpc_server.so` beside `libggml-rpc.so`; the
  installer extracts it to `nativeLibraryDir` as an executable file, where the
  device loads the RPC backend and its runtime-selected CPU kernels (verified on
  a Tab S9 FE). Running it needs `LD_LIBRARY_PATH=<its directory>`, which the
  launcher sets.
- ✅ **Device smoke phase + benchmark.** `rpc_node_up` asks the app to start the
  peripheral (debug broadcast, since the shell cannot exec the app's lib dir),
  then asserts the socket is reachable and stops it cleanly — passes on the Tab
  S9 FE. `tests/mesh_rpc_bench.py` reproduces the local/half/all table in one
  command (plus an optional `--usb` forward; note it only buys latency over a
  real cable). Thermal refusal stays open work.
- ⏳ Option 1 (KV-cache / context split) stays prototyped offline in `kv_mesh/`
  with its design in `docs/kv_cache_mesh_split.md`.

## Contributing

Issues and pull requests are welcome. Please keep changes consistent with
the architecture's invariants: Tier 0/1 stay per-agent, Tier 2/3 stay
shareable, and nothing in the standard execution path may mutate Tier 3.
Hardware-facing changes must keep the lifecycle, thermal, transport and
model-execution stress suites green (`python -m unittest discover -s tests`).

