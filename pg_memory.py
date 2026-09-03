"""
ShugoCore PostgreSQL/pgvector Tier 2 backend (fleet-shared semantic memory)
===========================================================================

`PgSemanticMemory` is a drop-in replacement for `memory_system.SemanticMemory`
backed by PostgreSQL + the pgvector extension. It exists for shared
multi-process / multi-agent deployments: several ShugoCore agents (or planning
nodes) can point at the same DSN and see one consistent Tier 2 knowledge base,
which is the persistence half of the Shogunet memory mesh.

Guarantees and invariants
-------------------------
- API parity: implements the `SemanticMemory` public surface
  (store_fact, search, reinforce, decay, prune, get_fact, extract_entities,
  facts_about, related_entities, entity_names, close) so it can be passed to
  `DecisionEngine(semantic_memory=...)` or `MemoryManager(semantic=...)`.
- Embedding parity: uses the same deterministic hashing embedding as the
  SQLite backend (`vector_db.hashed_embedding` mirrors
  `SemanticMemory._embed`), so facts written by one backend are retrievable
  with identical similarity scores by the other.
- Fail-closed: if psycopg2 is missing, the DSN is unreachable, or the pgvector
  extension is unavailable, construction raises with instructions. There is NO
  silent stub mode - a fleet-shared memory that quietly failed to persist
  would violate the Tier 2 memory invariants (N0/N1).
- Tier 3 never lives here: this store only holds Tier 2 facts/entities.
  Core identity stays read-only, per-agent, as required by the architecture.

Operator notes
--------------
- Requires `CREATE EXTENSION vector` privilege once per database
  (https://github.com/pgvector/pgvector). Install the client dependency with
  `pip install 'shugocore[postgres]'`.
- Search uses exact cosine distance (`<=>`); correctness is never traded for
  speed. At fleet scale, add an index once:

      CREATE INDEX {prefix}_facts_embedding_hnsw ON {prefix}_facts
          USING hnsw (embedding vector_cosine_ops);
"""

import json
import logging
import re
import threading
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from memory_system import SemanticMemory, _utc_now_iso
from security import sanitize_text
from vector_db import hashed_embedding

try:
    import psycopg2  # type: ignore
    _HAS_PSYCOPG = True
except ImportError:
    _HAS_PSYCOPG = False

logger = logging.getLogger(__name__)

# Table (identifier) prefixes cannot be bound as SQL parameters, so the
# prefix is strictly validated before it ever touches an SQL string.
_PREFIX_RE = re.compile(r"^[a-z][a-z0-9_]{0,40}$")

_INSTALL_HINT = (
    "psycopg2 is required for PostgreSQL-backed Tier 2 memory; "
    "install it with: pip install 'shugocore[postgres]'"
)
_PGVECTOR_HINT = (
    "the pgvector extension is not available on this database; an operator "
    "must run 'CREATE EXTENSION vector;' once (see "
    "https://github.com/pgvector/pgvector) or choose SQLite storage"
)


def _vector_literal(vector: List[float]) -> str:
    """Format a float vector as a pgvector text literal: '[0.1,0.2,...]'.

    Casting this text to ``vector`` server-side means the client needs only
    psycopg2 - the ``pgvector`` Python package is not required.
    """
    parts = []
    for value in vector:
        value = float(value)
        if value != value or value in (float("inf"), float("-inf")):
            value = 0.0  # pgvector rejects NaN/inf; keep the literal valid
        parts.append(repr(value))
    return "[" + ",".join(parts) + "]"


class PgSemanticMemory:
    """Fleet-shared Tier 2 semantic memory (PostgreSQL + pgvector).

    Duck-type compatible with ``memory_system.SemanticMemory``. Thread-safe:
    a single connection guarded by an RLock, usable from the consolidation
    worker and the main loop (same model as the SQLite backend).
    """

    _SELECT_COLUMNS = ("id, content, kind, salience, access_count, "
                       "created_at, last_accessed, metadata")

    def __init__(self, dsn: str, dimension: int = 256,
                 table_prefix: str = "shugocore",
                 connection: Optional[Any] = None):
        if not _HAS_PSYCOPG:
            raise RuntimeError(_INSTALL_HINT)
        prefix = str(table_prefix)
        if not _PREFIX_RE.match(prefix):
            raise ValueError(
                "invalid table_prefix (must match ^[a-z][a-z0-9_]{0,40}$): "
                f"{table_prefix!r}")
        self.dsn = str(dsn)
        self.table_prefix = prefix
        self.dimension = max(16, int(dimension))
        self._t_facts = f"{prefix}_facts"
        self._t_entities = f"{prefix}_entities"
        self._t_fact_entities = f"{prefix}_fact_entities"
        self._lock = threading.RLock()
        if connection is not None:
            self._conn = connection
        else:
            self._conn = psycopg2.connect(self.dsn)
        self._initialize_schema()
        logger.info("PgSemanticMemory initialized (dim=%s, prefix='%s').",
                    self.dimension, prefix)

    # -- schema ----------------------------------------------------------------

    def _initialize_schema(self) -> None:
        with self._lock:
            cursor = self._conn.cursor()
            try:
                cursor.execute("CREATE EXTENSION IF NOT EXISTS vector")
            except Exception as exc:  # fail-closed: never degrade silently
                raise RuntimeError(_PGVECTOR_HINT) from exc
            cursor.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {self._t_facts} (
                    id            BIGSERIAL PRIMARY KEY,
                    content       TEXT NOT NULL,
                    kind          TEXT DEFAULT 'fact',
                    salience      REAL DEFAULT 1.0,
                    access_count  INTEGER DEFAULT 0,
                    created_at    TEXT,
                    last_accessed TEXT,
                    embedding     vector({int(self.dimension)}),
                    metadata      TEXT
                )
                """
            )
            cursor.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {self._t_entities} (
                    id            BIGSERIAL PRIMARY KEY,
                    name          TEXT UNIQUE NOT NULL,
                    mention_count INTEGER DEFAULT 0,
                    created_at    TEXT
                )
                """
            )
            cursor.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {self._t_fact_entities} (
                    fact_id   BIGINT NOT NULL,
                    entity_id BIGINT NOT NULL,
                    PRIMARY KEY (fact_id, entity_id)
                )
                """
            )
            self._conn.commit()

    # -- embedding helpers (identical algorithm to the SQLite backend) ---------

    @staticmethod
    def _tokenize(text: str) -> List[str]:
        return SemanticMemory._tokenize(text)

    def _embed(self, text: str) -> List[float]:
        """Deterministic hashing embedding, L2-normalized (cosine-ready)."""
        return hashed_embedding(str(text), self.dimension)

    @staticmethod
    def _cosine(a: List[float], b: List[float]) -> float:
        return sum(x * y for x, y in zip(a, b))

    # -- row mapping -------------------------------------------------------------

    @staticmethod
    def _row_to_fact(row: tuple,
                     similarity: Optional[float] = None) -> Dict[str, Any]:
        fact = {
            "id": int(row[0]),
            "content": row[1],
            "kind": row[2],
            "salience": float(row[3]),
            "access_count": int(row[4]),
            "created_at": row[5],
            "last_accessed": row[6],
            "metadata": json.loads(row[7]) if row[7] else {},
        }
        if similarity is not None:
            fact["similarity"] = float(similarity)
        return fact

    # -- CRUD ----------------------------------------------------------------

    def store_fact(self, content: str, kind: str = "fact",
                   salience: float = 1.0,
                   metadata: Optional[Dict[str, Any]] = None) -> int:
        """Insert a (sanitized, length-capped) fact and return its id."""
        now = _utc_now_iso()
        content = sanitize_text(content, 2000)
        kind = sanitize_text(kind, 32) or "fact"
        embedding = _vector_literal(self._embed(content))
        meta = json.dumps(metadata or {})
        with self._lock:
            cursor = self._conn.cursor()
            cursor.execute(
                f"""
                INSERT INTO {self._t_facts}
                    (content, kind, salience, access_count,
                     created_at, last_accessed, embedding, metadata)
                VALUES (%s, %s, %s, 0, %s, %s, %s::vector, %s)
                RETURNING id
                """,
                (str(content), str(kind), float(salience),
                 now, now, embedding, meta),
            )
            fact_id = int(cursor.fetchone()[0])
            self._link_entities(int(fact_id), content)
            self._conn.commit()
            return fact_id

    # -- entity graph (Tier 2 hybrid: vectors + explicit relations) ----------

    _ENTITY_PATTERNS = SemanticMemory._ENTITY_PATTERNS
    _ENTITY_STOPWORDS = SemanticMemory._ENTITY_STOPWORDS

    def extract_entities(self, text: str, limit: int = 8) -> List[str]:
        """Deterministic entity candidates (hosts, model ids, proper nouns)."""
        names: List[str] = []
        seen = set()
        for pattern in self._ENTITY_PATTERNS:
            for match in pattern.finditer(str(text)):
                name = sanitize_text(match.group(1), 64).strip().lower()
                if (len(name) < 3 or name in seen
                        or name in self._ENTITY_STOPWORDS):
                    continue
                seen.add(name)
                names.append(name)
                if len(names) >= limit:
                    return names
        return names

    def _link_entities(self, fact_id: int, content: str) -> None:
        now = _utc_now_iso()
        cursor = self._conn.cursor()
        for name in self.extract_entities(content):
            cursor.execute(
                f"SELECT id FROM {self._t_entities} WHERE name = %s",
                (name,))
            row = cursor.fetchone()
            if row:
                entity_id = int(row[0])
                cursor.execute(
                    f"UPDATE {self._t_entities} "
                    "SET mention_count = mention_count + 1 WHERE id = %s",
                    (entity_id,))
            else:
                cursor.execute(
                    f"INSERT INTO {self._t_entities} "
                    "(name, mention_count, created_at) VALUES (%s, 1, %s) "
                    "RETURNING id",
                    (name, now))
                entity_id = int(cursor.fetchone()[0])
            cursor.execute(
                f"INSERT INTO {self._t_fact_entities} (fact_id, entity_id) "
                "VALUES (%s, %s) ON CONFLICT DO NOTHING",
                (int(fact_id), entity_id))

    def facts_about(self, entity_name: str,
                    limit: int = 10) -> List[Dict[str, Any]]:
        """Graph query: facts explicitly linked to an entity."""
        name = sanitize_text(entity_name, 64).lower()
        with self._lock:
            cursor = self._conn.cursor()
            cursor.execute(
                f"SELECT f.id, f.content, f.kind, f.salience, "
                f"f.access_count, f.created_at, f.last_accessed, f.metadata "
                f"FROM {self._t_facts} f "
                f"JOIN {self._t_fact_entities} fe ON fe.fact_id = f.id "
                f"JOIN {self._t_entities} e ON e.id = fe.entity_id "
                f"WHERE e.name = %s "
                f"ORDER BY f.salience DESC, f.id DESC LIMIT %s",
                (name, max(1, int(limit))),
            )
            rows = cursor.fetchall()
        return [self._row_to_fact(row) for row in rows]

    def related_entities(self, entity_name: str,
                         limit: int = 10) -> List[Dict[str, Any]]:
        """Graph adjacency: entities co-occurring in the same facts."""
        name = sanitize_text(entity_name, 64).lower()
        with self._lock:
            cursor = self._conn.cursor()
            cursor.execute(
                f"SELECT e2.name, e2.mention_count, "
                f"COUNT(*) AS co_occurrences "
                f"FROM {self._t_entities} e1 "
                f"JOIN {self._t_fact_entities} fe1 ON fe1.entity_id = e1.id "
                f"JOIN {self._t_fact_entities} fe2 ON fe2.fact_id = fe1.fact_id "
                f"JOIN {self._t_entities} e2 ON e2.id = fe2.entity_id "
                f"WHERE e1.name = %s AND e2.name != %s "
                f"GROUP BY e2.id, e2.name, e2.mention_count "
                f"ORDER BY co_occurrences DESC LIMIT %s",
                (name, name, max(1, int(limit))),
            )
            rows = cursor.fetchall()
        return [{"name": row[0], "mention_count": int(row[1]),
                 "co_occurrences": int(row[2])} for row in rows]

    def entity_names(self, limit: int = 100) -> List[str]:
        with self._lock:
            cursor = self._conn.cursor()
            cursor.execute(
                f"SELECT name FROM {self._t_entities} "
                "ORDER BY mention_count DESC LIMIT %s",
                (max(1, int(limit)),))
            rows = cursor.fetchall()
        return [row[0] for row in rows]

    # -- similarity search (pushed down to pgvector) ---------------------------

    def search(self, query: str, top_k: int = 5, min_salience: float = 0.0,
               reinforce: bool = True) -> List[Dict[str, Any]]:
        """Vector similarity search.

        Distance is computed server-side by pgvector (``<=>`` cosine
        distance); ``similarity = 1 - distance`` so scores match the SQLite
        backend's cosine similarity on the same normalized embeddings.
        ``distance < 1`` is exactly ``similarity > 0``.
        """
        query_vector = _vector_literal(self._embed(query))
        with self._lock:
            cursor = self._conn.cursor()
            cursor.execute(
                f"SELECT {self._SELECT_COLUMNS}, "
                f"1 - (embedding <=> %s::vector) AS similarity "
                f"FROM {self._t_facts} "
                f"WHERE salience >= %s AND embedding <=> %s::vector < 1.0 "
                f"ORDER BY embedding <=> %s::vector LIMIT %s",
                (query_vector, float(min_salience),
                 query_vector, query_vector, max(0, int(top_k))),
            )
            rows = cursor.fetchall()
            results = [self._row_to_fact(row, similarity=row[8])
                       for row in rows]
            if reinforce:
                for row in rows:
                    self.reinforce(int(row[0]))
        return results

    def reinforce(self, fact_id: int, boost: float = 0.25,
                  cap: float = 10.0) -> None:
        """Strengthen a memory because it was re-accessed."""
        with self._lock:
            cursor = self._conn.cursor()
            cursor.execute(
                f"SELECT salience, access_count FROM {self._t_facts} "
                "WHERE id = %s",
                (int(fact_id),))
            row = cursor.fetchone()
            if not row:
                return
            new_salience = min(cap, float(row[0]) + boost)
            cursor.execute(
                f"UPDATE {self._t_facts} "
                "SET salience = %s, access_count = %s, last_accessed = %s "
                "WHERE id = %s",
                (new_salience, int(row[1]) + 1, _utc_now_iso(), int(fact_id)),
            )
            self._conn.commit()

    def decay(self, half_life_hours: float = 72.0) -> int:
        """
        Exponential decay of salience based on time since last access.
        Computed in Python with the exact algorithm of the SQLite backend so
        both backends produce identical salience trajectories; updates are
        batched in a single transaction.
        """
        if half_life_hours <= 0:
            half_life_hours = 1e-9
        now = datetime.now(timezone.utc)
        with self._lock:
            cursor = self._conn.cursor()
            cursor.execute(
                f"SELECT id, salience, last_accessed FROM {self._t_facts}")
            rows = cursor.fetchall()
            decayed = 0
            for fact_id, salience, last_accessed in rows:
                try:
                    accessed = datetime.fromisoformat(last_accessed)
                except (TypeError, ValueError):
                    continue
                hours = max(0.0, (now - accessed).total_seconds() / 3600.0)
                if hours <= 0:
                    continue
                new_salience = float(salience) * (
                    0.5 ** (hours / half_life_hours))
                if abs(new_salience - float(salience)) > 1e-9:
                    cursor.execute(
                        f"UPDATE {self._t_facts} SET salience = %s "
                        "WHERE id = %s",
                        (new_salience, int(fact_id)),
                    )
                    decayed += 1
            self._conn.commit()
            return decayed

    def prune(self, min_salience: float = 0.05) -> int:
        """Delete facts whose salience fell below the relevance floor."""
        with self._lock:
            cursor = self._conn.cursor()
            cursor.execute(
                f"DELETE FROM {self._t_facts} WHERE salience < %s",
                (float(min_salience),))
            count = cursor.rowcount
            self._conn.commit()
            return int(count)

    def get_fact(self, fact_id: int) -> Optional[Dict[str, Any]]:
        with self._lock:
            cursor = self._conn.cursor()
            cursor.execute(
                f"SELECT {self._SELECT_COLUMNS} FROM {self._t_facts} "
                "WHERE id = %s",
                (int(fact_id),))
            row = cursor.fetchone()
        return self._row_to_fact(row) if row else None

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.close()
            except Exception:  # pragma: no cover - close best-effort
                pass


def open_semantic_memory(source: str = "semantic_memory.db",
                         dimension: int = 256,
                         **kwargs: Any):
    """Open Tier 2 storage, dispatching on the source.

    - ``postgres://...`` / ``postgresql://...`` DSN -> fleet-shared
      :class:`PgSemanticMemory` (PostgreSQL + pgvector).
    - anything else -> :class:`memory_system.SemanticMemory` (local SQLite),
      preserving historical behavior (paths, ``:memory:``).

    This is the single knob fleet operators use; ``DecisionEngine`` and
    ``ContinuousAgent`` route ``memory_db_path`` through it.
    """
    text = str(source)
    if text.startswith(("postgres://", "postgresql://")):
        return PgSemanticMemory(dsn=text, dimension=dimension, **kwargs)
    return SemanticMemory(db_path=text, dimension=dimension)
