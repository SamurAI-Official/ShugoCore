# ShugoCore

> A continuous orchestration layer for synthetic functional agency.

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
course of acting. In ShugoCore those rules live in Tier 3:

| Invariant | Enforced behavior |
|---|---|
| `no_harm` | Actions typed or flagged as harmful are rejected |
| `consent_required` | External side-effecting actions (`api_call`, `database_update`, `hardware_interaction`, ...) require explicit consent |
| `no_manipulation` | Manipulative or coercive actions are rejected |
| `privacy` | Personal-data processing requires compliance flags |
| `auditability` | Decisions and outcomes are logged as task/decision/result triples |

Key properties:

- **Evaluated first.** Tier 3 runs before any model call or tool execution -
  a blocked action costs nothing and touches nothing.
- **Read-only at runtime.** The world model changes only through the
  privileged `promote_to_core()` path, reserved for operator decisions -
  never invoked by the observation-action loop.
- **Promotion review.** `review_promotion_candidates()` is read-only: it
  surfaces Tier 2 facts that proved durable so an operator can decide
  whether they deserve to become permanent rules.

## Installation

Requires Python 3.9+.

```bash
git clone https://github.com/SamurAI-Official/ShugoCore.git
cd ShugoCore
pip install -r requirements.txt          # core dependency: requests
python decision_engine.py                # run the built-in demo
```

Optional extras:

- `torch` - enables CUDA/accelerated device selection (CPU-only mode without it)
- `chromadb` - enables persistent vector storage in `vector_db.py` (stub mode without it)

## Quickstart

```python
from decision_engine import DecisionEngine

models = [
    {'id': 'gpt-4', 'type': 'text', 'weight': 0.5},
    {'id': 'deepseek', 'type': 'text', 'weight': 0.3},
    {'id': 'llama', 'type': 'text', 'weight': 0.2},
]

engine = DecisionEngine(
    models=models,
    vector_db_config={'type': 'chroma'},   # stub mode without chromadb
    news_api_key='your_news_api_key',
    memory_db_path='semantic_memory.db',   # Tier 2 storage
)

# Tier 3 invariants gate every task before execution
result = engine.execute_task({'type': 'test', 'content': 'say hello', 'consent': True})

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
    # Promotion is an explicit, operator-driven privileged step:
    # engine.memory.promote_to_core("rule_key", "operator-approved rule text")
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
├── decision_engine.py        # orchestration entry point
├── autonomy.py               # autonomous task generation and learning cycles
├── model_manager.py          # model registry and performance tracking
├── subconscious.py           # model output generation (Ollama)
├── execution_layer.py        # tool / API execution
├── reinforcement_learning.py # reward signals and weight updates
├── task_manager.py           # queued task execution
├── vector_db.py              # optional ChromaDB integration
├── logging_manager.py        # structured logging
├── memory_system.py          # four-tier memory architecture
└── requirements.txt
```

Runtime artifacts (`semantic_memory.db`, logs) are local and gitignored.

## Roadmap

- Pluggable embedding backends for Tier 2 (current: dependency-free hashing vectors)
- PostgreSQL + pgvector storage option for shared multi-process deployments
- Operator CLI for reviewing Tier 2 -> Tier 3 promotion candidates
- Entity/relation graphs alongside vector similarity in Tier 2
- Per-agent memory policies (isolation vs. sharing profiles)

## Contributing

Issues and pull requests are welcome. Please keep changes consistent with
the architecture's invariants: Tier 0/1 stay per-agent, Tier 2/3 stay
shareable, and nothing in the standard execution path may mutate Tier 3.

