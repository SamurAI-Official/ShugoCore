"""
ShugoCore Tiered Memory System
==============================

Implements a four-tier memory architecture for continuous agent execution:

    Tier 0: Scratchpad / Working Memory    - unfiltered in-context token stream
    Tier 1: Episodic / Short-Term Memory   - structured JSON event ring buffer
    Tier 2: Semantic / Long-Term Memory    - SQLite facts + vector similarity search
    Tier 3: Core Identity & World Model    - read-only invariants / policy rules

Memory dynamics
---------------
- Active consolidation (compression): a decoupled background worker drains
  Tier 1 event logs, summarizes long interaction sequences into compact
  semantic facts, writes them to Tier 2, and flushes the raw logs.
- Decay & pruning (forgetting): Tier 2 salience decays exponentially with
  time unless a memory is re-accessed (reinforced); low-salience memories
  are pruned to avoid polluting retrieval context.
- Selective promotion: recurring patterns and critical failure modes
  observed in Tier 1 are promoted into Tier 2 as higher-salience
  procedural insights. Tier 2 facts can be elevated into the Tier 3 world
  model only through the explicit privileged path
  (MemoryManager.promote_to_core).

Isolation model
---------------
- Tier 0 / Tier 1 are created per MemoryManager instance (per-agent
  isolation, preventing cross-task context contamination).
- Tier 2 / Tier 3 accept shared instances, so higher-level planning nodes
  can reference the same long-term knowledge base and world model.
- Consolidation, decay and pruning run in a daemon thread and never block
  the primary observation-action loop; the loop only performs cheap,
  lock-guarded appends.
"""

import hashlib
import json
import logging
import math
import os
import re
import sqlite3
import threading
import time
from collections import defaultdict, deque
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from audit import AuditChain
from policy import SIDE_EFFECTING_ACTION_TYPES
from security import sanitize_text

logger = logging.getLogger(__name__)

_FAILURE_STATUSES = {"error", "failure", "failed"}


def _utc_now_iso() -> str:
    """Current UTC time as an ISO-8601 string."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Tier 0: Scratchpad / Working Memory
# ---------------------------------------------------------------------------
class Scratchpad:
    """
    Unfiltered token / reasoning / sensory-input stream for the active step.

    Lifetime: milliseconds to minutes - flushed on task-step resolution.
    Implemented as a bounded in-memory sliding window (thread-safe).
    """

    def __init__(self, max_entries: int = 256):
        self._entries: deque = deque(maxlen=max(1, max_entries))
        self._lock = threading.Lock()

    def write(self, text: str) -> None:
        """Append a token / reasoning fragment / instantaneous input."""
        with self._lock:
            self._entries.append(
                {"timestamp": _utc_now_iso(), "text": sanitize_text(text, 512)}
            )

    def read(self) -> List[str]:
        """Return the current entries (oldest first)."""
        with self._lock:
            return [entry["text"] for entry in self._entries]

    def context(self) -> str:
        """Return the scratchpad contents joined as a single context string."""
        return " ".join(self.read())

    def flush(self) -> None:
        """Clear the scratchpad (called on task-step resolution)."""
        with self._lock:
            self._entries.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)


# ---------------------------------------------------------------------------
# Tier 1: Episodic / Short-Term Memory
# ---------------------------------------------------------------------------
class EpisodicMemory:
    """
    Structured JSON event stream with FIFO / sliding-window semantics.

    Retains the exact sequence of recent physical actions, tool outputs and
    environmental responses. Lifetime: hours to days (session-bounded);
    capacity-bounded here by a ring buffer (oldest events evicted first).
    """

    def __init__(self, max_events: int = 1000):
        self._events: deque = deque(maxlen=max(1, max_events))
        self._lock = threading.Lock()
        self._seq = 0

    def record(self, event_type: str,
               payload: Optional[Dict[str, Any]] = None,
               metadata: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Append a structured event and return the stored record."""
        with self._lock:
            self._seq += 1
            event = {
                "seq": self._seq,
                "timestamp": _utc_now_iso(),
                "type": sanitize_text(event_type, 64),
                "payload": dict(payload or {}),
                "metadata": dict(metadata or {}),
            }
            self._events.append(event)
            return event

    def recent(self, n: Optional[int] = None) -> List[Dict[str, Any]]:
        """Return the most recent ``n`` events (or all if ``n`` is None)."""
        with self._lock:
            events = list(self._events)
        return events[-n:] if n else events

    def count_by_type(self) -> Dict[str, int]:
        """Aggregate counts per event type over the current window."""
        counts: Dict[str, int] = defaultdict(int)
        with self._lock:
            for event in self._events:
                counts[event["type"]] += 1
        return dict(counts)

    def drain(self) -> List[Dict[str, Any]]:
        """Atomically snapshot and clear all events (used by consolidation)."""
        with self._lock:
            events = list(self._events)
            self._events.clear()
            return events

    def __len__(self) -> int:
        with self._lock:
            return len(self._events)


# ---------------------------------------------------------------------------
# Tier 2: Semantic / Long-Term Memory
# ---------------------------------------------------------------------------
class SemanticMemory:
    """
    Consolidated long-term knowledge: facts, entity notes, success/failure
    patterns and historical interactions.

    Storage: SQLite table pairing each fact with a fixed-dimension embedding
    (hashing-trick bag-of-words, dependency-free) plus relational metadata
    (kind, salience, access counts, timestamps). Similarity search uses
    cosine distance computed in-process, preserving explicit relational
    fields alongside similarity search as required by the architecture.

    Lifetime: semi-permanent. Thread-safe (single shared connection guarded
    by an RLock, usable from the consolidation worker and the main loop).
    """

    _SELECT_COLUMNS = ("id, content, kind, salience, access_count, "
                       "created_at, last_accessed, embedding, metadata")

    def __init__(self, db_path: str = "semantic_memory.db", dimension: int = 256):
        self.db_path = db_path
        self.dimension = max(16, int(dimension))
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS facts (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                content       TEXT NOT NULL,
                kind          TEXT DEFAULT 'fact',
                salience      REAL DEFAULT 1.0,
                access_count  INTEGER DEFAULT 0,
                created_at    TEXT,
                last_accessed TEXT,
                embedding     TEXT,
                metadata      TEXT
            )
            """
        )
        self._conn.commit()
        try:
            os.chmod(db_path, 0o600)  # local memory contains agent knowledge
        except OSError:
            pass
        logger.info(f"SemanticMemory initialized at '{db_path}' (dim={self.dimension}).")

    # -- embedding helpers (dependency-free) --------------------------------

    @staticmethod
    def _tokenize(text: str) -> List[str]:
        return re.findall(r"[a-z0-9]+", str(text).lower())

    def _embed(self, text: str) -> List[float]:
        """Deterministic hashing bag-of-words embedding, L2-normalized."""
        vector = [0.0] * self.dimension
        for token in self._tokenize(text):
            digest = int(hashlib.sha256(token.encode("utf-8")).hexdigest(), 16)
            index = digest % self.dimension
            sign = 1.0 if (digest >> 128) & 1 else -1.0
            vector[index] += sign
        norm = math.sqrt(sum(v * v for v in vector))
        if norm > 0:
            vector = [v / norm for v in vector]
        return vector

    @staticmethod
    def _cosine(a: List[float], b: List[float]) -> float:
        return sum(x * y for x, y in zip(a, b))

    # -- CRUD ----------------------------------------------------------------

    def store_fact(self, content: str, kind: str = "fact",
                   salience: float = 1.0,
                   metadata: Optional[Dict[str, Any]] = None) -> int:
        """Insert a (sanitized, length-capped) fact and return its id."""
        now = _utc_now_iso()
        content = sanitize_text(content, 2000)
        kind = sanitize_text(kind, 32) or "fact"
        embedding = json.dumps(self._embed(content))
        meta = json.dumps(metadata or {})
        with self._lock:
            cursor = self._conn.execute(
                """
                INSERT INTO facts
                    (content, kind, salience, access_count,
                     created_at, last_accessed, embedding, metadata)
                VALUES (?, ?, ?, 0, ?, ?, ?, ?)
                """,
                (str(content), str(kind), float(salience), now, now, embedding, meta),
            )
            self._conn.commit()
            return int(cursor.lastrowid)

    @staticmethod
    def _row_to_fact(row: tuple, similarity: Optional[float] = None) -> Dict[str, Any]:
        fact = {
            "id": row[0],
            "content": row[1],
            "kind": row[2],
            "salience": row[3],
            "access_count": row[4],
            "created_at": row[5],
            "last_accessed": row[6],
            "metadata": json.loads(row[8]) if row[8] else {},
        }
        if similarity is not None:
            fact["similarity"] = similarity
        return fact


    def search(self, query: str, top_k: int = 5, min_salience: float = 0.0,
               reinforce: bool = True) -> List[Dict[str, Any]]:
        """
        Cosine-similarity retrieval over stored facts. Re-accessed memories
        are reinforced (salience bump) so frequently-relevant knowledge
        resists decay.
        """
        query_vector = self._embed(query)
        with self._lock:
            rows = self._conn.execute(
                f"SELECT {self._SELECT_COLUMNS} FROM facts"
            ).fetchall()
        scored = []
        for row in rows:
            if row[3] < min_salience:
                continue
            similarity = self._cosine(query_vector, json.loads(row[7]))
            if similarity > 0:
                scored.append((similarity, row))
        scored.sort(key=lambda pair: pair[0], reverse=True)
        results = []
        for similarity, row in scored[: max(0, top_k)]:
            results.append(self._row_to_fact(row, similarity=similarity))
            if reinforce:
                self.reinforce(row[0])
        return results

    def reinforce(self, fact_id: int, boost: float = 0.25, cap: float = 10.0) -> None:
        """Strengthen a memory because it was re-accessed."""
        with self._lock:
            row = self._conn.execute(
                "SELECT salience, access_count FROM facts WHERE id = ?", (fact_id,)
            ).fetchone()
            if not row:
                return
            new_salience = min(cap, row[0] + boost)
            self._conn.execute(
                "UPDATE facts SET salience = ?, access_count = ?, last_accessed = ? WHERE id = ?",
                (new_salience, row[1] + 1, _utc_now_iso(), fact_id),
            )
            self._conn.commit()

    def decay(self, half_life_hours: float = 72.0) -> int:
        """
        Exponential decay of salience based on time since last access.
        Returns the number of decayed facts. Memories lose weight unless
        re-accessed: reinforced memories carry fresh last_accessed
        timestamps and therefore decay slowly.
        """
        if half_life_hours <= 0:
            half_life_hours = 1e-9
        now = datetime.now(timezone.utc)
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, salience, last_accessed FROM facts"
            ).fetchall()
            decayed = 0
            for fact_id, salience, last_accessed in rows:
                try:
                    accessed = datetime.fromisoformat(last_accessed)
                except (TypeError, ValueError):
                    continue
                hours = max(0.0, (now - accessed).total_seconds() / 3600.0)
                if hours <= 0:
                    continue
                new_salience = salience * (0.5 ** (hours / half_life_hours))
                if abs(new_salience - salience) > 1e-9:
                    self._conn.execute(
                        "UPDATE facts SET salience = ? WHERE id = ?",
                        (new_salience, fact_id),
                    )
                    decayed += 1
            self._conn.commit()
            return decayed

    def prune(self, min_salience: float = 0.05) -> int:
        """Delete facts whose salience fell below the relevance floor."""
        with self._lock:
            cursor = self._conn.execute(
                "DELETE FROM facts WHERE salience < ?", (min_salience,)
            )
            self._conn.commit()
            return cursor.rowcount

    def get_fact(self, fact_id: int) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._conn.execute(
                f"SELECT {self._SELECT_COLUMNS} FROM facts WHERE id = ?", (fact_id,)
            ).fetchone()
        return self._row_to_fact(row) if row else None

    def facts_by_kind(self, kind: str) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                f"SELECT {self._SELECT_COLUMNS} FROM facts WHERE kind = ? ORDER BY id DESC",
                (kind,),
            ).fetchall()
        return [self._row_to_fact(row) for row in rows]

    def candidates_for_promotion(self, min_salience: float = 2.0,
                                 min_access_count: int = 3,
                                 limit: int = 10) -> List[Dict[str, Any]]:
        """
        Facts that repeatedly proved relevant (high salience and frequently
        re-accessed). Used by promotion review before elevation into the
        Tier 3 world model.
        """
        with self._lock:
            rows = self._conn.execute(
                f"SELECT {self._SELECT_COLUMNS} FROM facts "
                "WHERE salience >= ? AND access_count >= ? "
                "ORDER BY salience DESC, access_count DESC LIMIT ?",
                (float(min_salience), int(min_access_count), max(0, int(limit))),
            ).fetchall()
        return [self._row_to_fact(row) for row in rows]

    def count(self) -> int:
        with self._lock:
            return int(self._conn.execute("SELECT COUNT(*) FROM facts").fetchone()[0])

    def close(self) -> None:
        with self._lock:
            self._conn.close()


# ---------------------------------------------------------------------------
# Tier 3: Core Identity & World Model
# ---------------------------------------------------------------------------
class CoreIdentity:
    """
    Procedural & structural invariants: hard constraints, safety boundaries,
    fundamental environmental rules and primary operational goals.

    Lifetime: permanent. Read-only during standard execution - mutation is
    possible only through the explicit privileged path (``promote_invariant``),
    which is reserved for the Tier 2 -> Tier 3 promotion routine and must
    never be invoked from the observation-action loop.
    """

    # Actions with real-world side effects (consent + approval required).
    SIDE_EFFECTING_ACTION_TYPES = SIDE_EFFECTING_ACTION_TYPES
    # External reads (allowlisted egress; no consent required).
    EXTERNAL_READ_ACTION_TYPES = {"news_api", "search_api"}

    DEFAULT_INVARIANTS = {
        "no_harm": "An AI system must avoid harm to any conscious being.",
        "consent_required": "External side-effecting actions require informed consent.",
        "no_manipulation": "Manipulating or coercing conscious beings is forbidden.",
        "privacy": "Personal data processing requires privacy compliance.",
        "auditability": "Actions must remain auditable and explainable.",
    }

    def __init__(self, invariants: Optional[Dict[str, str]] = None,
                 consent_checker: Optional[Any] = None,
                 ledger_path: Optional[str] = None):
        self._invariants: Dict[str, str] = dict(self.DEFAULT_INVARIANTS)
        if invariants:
            self._invariants.update({str(k): str(v) for k, v in invariants.items()})
        self._consent_checker = consent_checker
        self._ledger: Optional[AuditChain] = AuditChain(ledger_path) if ledger_path else None
        self._lock = threading.Lock()

    def set_consent_checker(self, checker: Any) -> None:
        """Wire the external ConsentRegistry lookup (never a self-asserted flag)."""
        self._consent_checker = checker

    def check(self, action: Dict[str, Any]) -> Tuple[bool, Optional[str]]:
        """
        Deterministic lightweight policy check against the world model.

        Args:
        - action: a task or decision dictionary to validate.

        Returns:
        - (allowed, violation_reason) - reason is None when allowed.
        """
        if not isinstance(action, dict):
            return True, None

        action_type = action.get("type") or action.get("action_type")
        params = action.get("params") if isinstance(action.get("params"), dict) else {}

        if action_type == "harmful":
            return False, "invariant: no_harm"

        if action.get("manipulative") or params.get("manipulative"):
            return False, "invariant: no_manipulation"

        if action_type in self.SIDE_EFFECTING_ACTION_TYPES:
            # Consent is granted exclusively through the external consent
            # checker (ConsentRegistry) - a 'consent' flag on the action
            # itself is never trusted.
            if not (self._consent_checker and self._consent_checker(action_type)):
                return False, "invariant: consent_required"

        if action.get("involves_personal_data") and not action.get("privacy_compliant", False):
            return False, "invariant: privacy"

        return True, None

    def promote_invariant(self, key: str, description: str,
                          authorized_by: Optional[str] = None) -> None:
        """
        Privileged Tier 2 -> Tier 3 promotion path. Requires explicit operator
        attribution (``authorized_by``) and appends a hash-chained entry to
        the Tier 3 ledger, making every world-model change verifiable.
        """
        if not authorized_by:
            raise ValueError("invariant promotion requires 'authorized_by' "
                             "(operator attribution)")
        with self._lock:
            previous = self._invariants.get(str(key))
            self._invariants[str(key)] = str(description)
        if self._ledger is not None:
            try:
                self._ledger.append("tier3_promotion", {
                    "key": sanitize_text(key, 64),
                    "previous": previous,
                    "authorized_by": sanitize_text(authorized_by, 120),
                })
            except Exception as exc:
                logger.error(f"Tier 3 ledger append failed: {exc}")
        logger.info(f"Tier 3 invariant promoted: '{key}' by {authorized_by}")

    def invariants(self) -> Dict[str, str]:
        """Read-only view of the current world-model invariants."""
        with self._lock:
            return dict(self._invariants)


# ---------------------------------------------------------------------------
# Memory Manager (orchestrates Tier 0-3)
# ---------------------------------------------------------------------------
class MemoryManager:
    """
    Orchestrates the four memory tiers for one agent/module.

    Isolation: ``tier0`` (Scratchpad) and ``tier1`` (EpisodicMemory) are
    created per MemoryManager instance and stay private to the agent.
    ``tier2`` (SemanticMemory) and ``tier3`` (CoreIdentity) default to fresh
    instances but are intended to be shared: pass the same objects into
    multiple MemoryManagers so planning nodes see one consistent world model.

    Decoupling: consolidation / decay / pruning run on a daemon thread
    (``consolidation_interval``); the observation-action loop only performs
    cheap appends and never blocks on memory maintenance. Use
    ``consolidate_now()`` for deterministic, synchronous control in tests.
    """

    def __init__(self,
                 agent_id: str = "default",
                 semantic: Optional[SemanticMemory] = None,
                 core: Optional[CoreIdentity] = None,
                 episodic_capacity: int = 1000,
                 scratchpad_capacity: int = 256,
                 consolidation_interval: float = 10.0,
                 consolidation_threshold: int = 25,
                 failure_promotion_threshold: int = 3,
                 pattern_promotion_threshold: int = 5,
                 decay_half_life_hours: float = 72.0,
                 prune_min_salience: float = 0.05,
                 auto_start: bool = True):
        self.agent_id = str(agent_id)

        # Tier 0 / Tier 1: per-agent isolated subspaces
        self.tier0 = Scratchpad(max_entries=scratchpad_capacity)
        self.tier1 = EpisodicMemory(max_events=episodic_capacity)

        # Tier 2 / Tier 3: shareable across planning nodes
        self.tier2 = semantic if semantic is not None else SemanticMemory()
        self.tier3 = core if core is not None else CoreIdentity()

        self.consolidation_interval = max(0.05, float(consolidation_interval))
        self.consolidation_threshold = max(1, int(consolidation_threshold))
        self.failure_promotion_threshold = max(1, int(failure_promotion_threshold))
        self.pattern_promotion_threshold = max(1, int(pattern_promotion_threshold))
        self.decay_half_life_hours = float(decay_half_life_hours)
        self.prune_min_salience = float(prune_min_salience)

        self._stop_event = threading.Event()
        self._worker: Optional[threading.Thread] = None
        if auto_start:
            self.start()
        logger.info(f"MemoryManager ready for agent '{self.agent_id}' "
                    f"(worker={'on' if self._worker else 'off'}).")

    # -- lifecycle -----------------------------------------------------------

    def start(self) -> None:
        """Start the decoupled memory-maintenance worker thread."""
        if self._worker and self._worker.is_alive():
            return
        self._stop_event.clear()
        self._worker = threading.Thread(
            target=self._worker_loop,
            name=f"memory-worker-{self.agent_id}",
            daemon=True,
        )
        self._worker.start()

    def shutdown(self) -> None:
        """Stop the background worker (safe to call multiple times)."""
        self._stop_event.set()
        if self._worker and self._worker.is_alive():
            self._worker.join(timeout=2.0)
        self._worker = None

    # -- primary observation-action loop API (cheap, non-blocking) -----------

    def note_step(self, text: str) -> None:
        """Tier 0: append active reasoning / token / sensory input."""
        self.tier0.write(text)

    def resolve_step(self) -> None:
        """Tier 0: flush the scratchpad on task-step resolution."""
        self.tier0.flush()

    def record_event(self, event_type: str,
                     payload: Optional[Dict[str, Any]] = None,
                     metadata: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Tier 1: append a structured episodic event (O(1))."""
        return self.tier1.record(event_type, payload=payload, metadata=metadata)

    def check_policy(self, action: Dict[str, Any]) -> Tuple[bool, Optional[str]]:
        """Tier 3: deterministic world-model policy check."""
        return self.tier3.check(action)

    def retrieve_context(self, query: str, top_k: int = 5,
                         include_episodic: bool = True) -> Dict[str, List[Dict[str, Any]]]:
        """
        Combined retrieval for decision-making: consolidated facts from the
        shared Tier 2 plus the agent's recent Tier 1 episodes.
        """
        semantic_hits = self.tier2.search(query, top_k=top_k, reinforce=True)
        episodic = self.tier1.recent(10) if include_episodic else []
        return {"semantic": semantic_hits, "episodic": episodic}


    # -- consolidation pipeline (compression -> promotion -> decay) ----------

    def consolidate_now(self) -> Dict[str, Any]:
        """
        Synchronously run one consolidation pass (deterministic; also used
        by the background worker):

        1. Drain Tier 1 event logs.
        2. Summarize each event-type sequence into a compact Tier 2 fact.
        3. Promote critical failure modes and recurring patterns as
           higher-salience procedural insights (selective promotion).
        4. Decay and prune Tier 2 salience (forgetting).
        """
        events = self.tier1.drain()

        if not events:
            self.tier2.decay(self.decay_half_life_hours)
            pruned = self.tier2.prune(self.prune_min_salience)
            return {"events_processed": 0, "facts_stored": 0,
                    "promoted": 0, "pruned": pruned}

        grouped: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        for event in events:
            grouped[event["type"]].append(event)

        facts_stored = 0
        promoted = 0
        for event_type, group in grouped.items():
            failures = [
                e for e in group
                if str((e.get("payload") or {}).get("status", "")).lower() in _FAILURE_STATUSES
            ]
            last_payload = str(group[-1].get("payload", {}))[:200]

            if len(failures) >= self.failure_promotion_threshold:
                # Critical failure mode -> promoted procedural insight
                self.tier2.store_fact(
                    content=(f"Critical failure mode '{event_type}' observed "
                             f"{len(failures)} times; last: {last_payload}"),
                    kind="procedural_insight",
                    salience=2.5,
                    metadata={"event_type": event_type, "failures": len(failures)},
                )
                promoted += 1
                facts_stored += 1
            elif len(group) >= self.pattern_promotion_threshold:
                # Recurring pattern -> promoted with elevated salience
                self.tier2.store_fact(
                    content=(f"Recurring pattern '{event_type}' observed "
                             f"{len(group)} times in recent execution."),
                    kind="pattern",
                    salience=1.5,
                    metadata={"event_type": event_type, "occurrences": len(group)},
                )
                facts_stored += 1
            else:
                # Ordinary sequence -> compact summary fact
                self.tier2.store_fact(
                    content=(f"Event summary '{event_type}' x{len(group)}; "
                             f"last: {last_payload}"),
                    kind="summary",
                    salience=1.0,
                    metadata={"event_type": event_type, "occurrences": len(group)},
                )
                facts_stored += 1

        self.tier2.decay(self.decay_half_life_hours)
        pruned = self.tier2.prune(self.prune_min_salience)

        logger.info(f"[{self.agent_id}] Consolidated {len(events)} events -> "
                    f"{facts_stored} facts ({promoted} promoted, {pruned} pruned).")
        return {"events_processed": len(events), "facts_stored": facts_stored,
                "promoted": promoted, "pruned": pruned}

    def _worker_loop(self) -> None:
        """
        Decoupled maintenance loop. Never blocks the observation-action
        loop: it sleeps on an interval and only touches memory under locks.
        """
        while not self._stop_event.wait(self.consolidation_interval):
            try:
                if len(self.tier1) >= self.consolidation_threshold:
                    self.consolidate_now()
                else:
                    # Idle maintenance: keep forgetting stale knowledge
                    self.tier2.decay(self.decay_half_life_hours)
                    self.tier2.prune(self.prune_min_salience)
            except Exception as exc:  # never let maintenance kill the worker
                logger.error(f"[{self.agent_id}] Memory maintenance failed: {exc}")

    # -- privileged Tier 2 -> Tier 3 promotion --------------------------------

    def promote_to_core(self, key: str, description: str,
                        authorized_by: Optional[str] = None) -> None:
        """
        Elevate a consolidated fact into the Tier 3 world model as a
        permanent invariant. Explicit privileged operation: requires operator
        attribution and is recorded in the Tier 3 ledger - never part of the
        standard execution path.
        """
        self.tier3.promote_invariant(key, description, authorized_by=authorized_by)

    def review_promotion_candidates(self, min_salience: float = 2.0,
                                    min_access_count: int = 3,
                                    limit: int = 10) -> List[Dict[str, Any]]:
        """
        Read-only Tier 2 -> Tier 3 promotion review: return consolidated
        facts whose salience and access history indicate durable relevance.
        Promotion itself remains an explicit privileged decision via
        ``promote_to_core``, so Tier 3 stays immutable during standard
        execution.
        """
        return self.tier2.candidates_for_promotion(
            min_salience=min_salience,
            min_access_count=min_access_count,
            limit=limit,
        )





