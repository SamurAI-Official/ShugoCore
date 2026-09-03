# ShugoCore

> A continuous orchestration layer for synthetic functional agency.

[![PyPI](https://img.shields.io/pypi/v/shugocore)](https://pypi.org/project/shugocore/)
![Release](https://img.shields.io/badge/release-v1.2.1-blue)
![Tests](https://img.shields.io/badge/tests-350%20passing-brightgreen)
![Python](https://img.shields.io/badge/python-3.9%E2%80%933.12-blue)
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
7. CONSOLIDATE  a decoupled worker compresses episodes into Tier 2 facts,
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
| `audit.py` | Tamper-evident hash-chained audit log |
| `model_backends.py` | Pluggable model adapters (Ollama HTTP, OpenAI-compatible, stub) |
| `ros2_interface.py` | ROS 2 abstraction: abstract message types, Twist sanitization, stub and rclpy implementations |
| `android_bridge.py` | Android ↔ ROS 2 transports: JavaBridge (Chaquopy + jros2/Fast-DDS) and rosbridge (WebSocket); JSON payload codec |
| `android_runtime.py` | Android app lifecycle (`onCreate`/`onPause`/…), power + thermal monitor → fallback triggers, Keystore-backed secrets |
| `android_node.py` | On-device node roles (sensor / compute / operator / full_agent) and local llama.cpp launcher detection |
| `mobile_nodes.py` | Host-side mobile fleet: pairing with TTL, topic ACL, compute offload broker, clamped teleop relay |
| `shugonet_bridge.py` | Multi-agent networking via Shogunet: send/query/sync actions, fleet memory mesh |
| `acceleration.py` | Hardware acceleration ladder (NPU → DSP → GPU → CPU) with thermal demotion and failure degradation |
| `robotics_handler.py` | Robotics execution handler: verified Twist/trajectory dispatch, emergency stop, watchdog |
| `state_machine.py` | Strict interlocks for the observation-action loop |
| `fallbacks.py` | Deterministic fallback controller (stall / budget / breaker triggers → safe state) |
| `gazebo_simulation.py` | Gazebo/Ignition simulation layer |
| `moveit_planner.py` | MoveIt 2 motion planning layer |
| `telemetry.py` | Telemetry hooks |
| `token_budget.py` | Context budgeting |

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
| `ConsentRegistry` | Side-effecting actions (`api_call`, `database_update`, `hardware_interaction`) require operator-issued grants - a `consent` flag written by the acting agent itself is never trusted |
| `ApprovalBroker` | Side effects additionally require human approval; fail-closed (no operator channel attached, or TTL expiry, means denied) |
| Policy verdict token | The engine binds an allow verdict to the canonical hash of the exact decision; the execution layer refuses missing, non-allow, or mismatched tokens |
| `CapabilityRegistry` | https-only egress, host allowlists, HTTP-method allowlists, SQL statement-type allowlists, empty-by-default hardware command allowlists |
| Egress controls | Mandatory timeouts, per-host rate limiting, circuit breakers, response size caps, redirects disabled |
| Hash-chained audit log | Every block, approval and execution is appended to a tamper-evident JSONL chain - verify with `python3 audit.py verify audit_chain.jsonl` |
| Secret hygiene | API keys resolved from environment variables at execution time, never carried in decision dicts; every log record passes a redaction filter |
| Honest execution | Unimplemented side-effecting actions return `not_implemented` - never simulated success - so the reinforcement signal cannot reward no-ops |
| Mobile fleet isolation | Android publishers are confined to `/shugocore/mobile/#`; `operator_node` teleop is clamped and relayed - phones never write actuation topics |
| Local inference confinement | On-device model endpoints must be loopback HTTP(S) on an allowlisted port (`CapabilityRegistry.validate_model_endpoint`) |

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

On-device model execution uses standard local launchers: ShugoCore probes
llama.cpp (`/health`), Ollama (`/api/tags`) and LM Studio (`/v1/models`),
and every backend URL must pass the loopback + port allowlist check before
the first prompt leaves the process.

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

### Integration architecture

The `shugonet_bridge.py` module follows the same pattern as `robotics_handler.py`
and `mobile_nodes.py`:

1. **Action types** are defined in `policy.py` and added to `KNOWN_ACTION_TYPES`
2. **Execution handler** dispatches network actions to the Shogunet runtime
3. **Fallback severities** are registered in the deterministic fallback controller
4. **Handler registration** occurs during `DecisionEngine` initialization

The bridge uses duck-typed contracts, so the Shogunet runtime can be swapped
with any compatible implementation.

## Installation

Requires Python 3.9+.

**From PyPI (recommended):**

```bash
pip install shugocore
```

**From the GitHub release (identical artifacts):**

```bash
pip install https://github.com/SamurAI-Official/ShugoCore/releases/download/v1.2.1/shugocore-1.2.1-py3-none-any.whl
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

> Published on [PyPI](https://pypi.org/project/shugocore/1.2.1/) - the wheel
> and sdist there are byte-identical to the `v1.2.1` git tag and the GitHub
> release assets (sha256 digests recorded on both).

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
    #                                authorized_by="operator")
```

## Testing

```bash
python -m unittest discover -s tests -v     # 335 tests, no native deps
```

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
├── docs/android_integration.md  # Android integration guide
├── tests/                    # security, integration & hardware-stress tests
└── requirements.txt
```

Runtime artifacts (`semantic_memory.db`, logs) are local and gitignored.

## Roadmap

- Pluggable embedding backends for Tier 2 (current: dependency-free hashing vectors)
- PostgreSQL + pgvector storage option for shared multi-process deployments
- Entity/relation graphs alongside vector similarity in Tier 2
- Per-agent memory policies (isolation vs. sharing profiles)
- HMAC-signed audit chains and remote log shipping
- Human approval UI beyond the programmatic broker API
- Per-model backend pools with health-based routing
- CI trusted publishing to PyPI via GitHub Actions (OIDC, no static token)
- Android llama.cpp-compatible host server for Termux (self-hosted launcher path)
- NPU bring-up on real devices (Snapdragon Hexagon / Dimensity APU) against the acceleration ladder
- Fleet dashboard for mobile nodes: pairing state, thermal headroom, offload telemetry

## Contributing

Issues and pull requests are welcome. Please keep changes consistent with
the architecture's invariants: Tier 0/1 stay per-agent, Tier 2/3 stay
shareable, and nothing in the standard execution path may mutate Tier 3.
Hardware-facing changes must keep the lifecycle, thermal, transport and
model-execution stress suites green (`python -m unittest discover -s tests`).

