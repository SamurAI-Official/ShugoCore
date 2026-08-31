# ShugoCore

An autonomous AI orchestration framework: multi-model decision making,
tool/API execution, reinforcement learning, and a four-tier memory system
for continuous, context-stable operation.

## Architecture

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

## Memory Architecture

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

## Memory configuration

`MemoryManager` knobs (pass via `DecisionEngine` construction or direct use):

| Parameter | Default | Meaning |
|---|---|---|
| `consolidation_interval` | 10.0 s | Background worker tick |
| `consolidation_threshold` | 25 events | Episodic backlog that triggers consolidation |
| `failure_promotion_threshold` | 3 | Repeated failures promoted as procedural insights |
| `pattern_promotion_threshold` | 5 | Repeating events promoted as patterns |
| `decay_half_life_hours` | 72.0 | Salience half-life since last access |
| `prune_min_salience` | 0.05 | Deletion floor for decayed memories |

## Requirements

- Python 3.9+
- `requests` (see `requirements.txt`)

Optional:
- `torch` - CUDA/accelerated device selection
- `chromadb` - persistent vector storage in `vector_db.py` (stub mode without it)

```bash
pip install -r requirements.txt
python decision_engine.py
```

