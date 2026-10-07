# ShugoCore v1.30.4 — CSFA hardening: bandit B608, remote audit, fleet auth, etc.

## Bucket A — Safety-critical for true continuity

### A1 — Bandit B608 in `pg_memory.py` ✅
- All identifier interpolation now uses `psycopg2.sql.Identifier` via
  `sql.SQL(...).format(ident=...)`; values remain parameterized.
- `bandit -lll pg_memory.py` reports **No issues identified** (was 1 Medium,
  1 Low).
- The test mock (`tests/test_pg_memory.py`) gained a tiny `_flatten_sql`
  helper that walks the `Composable` tree (real psycopg2 cursors do this
  via `cursor.execute(Composable)` natively; the fake needed a hand-rolled
  walk to keep the literal SQL-string assertions valid).

### A2 — Remote audit log shipping (`audit.py`) ✅
- New `LogSink` Protocol + `HTTPSAuditSink` (NDJSON POST, bearer auth,
  optional extra headers, TLS verify) + `FileAuditSink` (NDJSON mirror to a
  second local file).
- Both share a bounded `_BaseThreadedSink` core: bounded queue (drop-oldest
  on overflow), bounded retries (max 3, exponential backoff), bounded
  flush interval (1s default), thread-safe stats counters.
- `AuditChain.append()` now dispatches to sinks *after* the local append
  succeeds; a sink failure can never block a decision (the chain is the
  source of truth, sinks are observational).
- `sinks_from_env()` wires the engine up via:
  - `SHUGOCORE_AUDIT_HTTPS_URL` + `SHUGOCORE_AUDIT_HTTPS_TOKEN` (+ optional
    `SHUGOCORE_AUDIT_HTTPS_VERIFY_TLS=false` for dev)
  - `SHUGOCORE_AUDIT_FILE_PATH`
- `chain.sink_stats()` returns observability counters per sink
  (`submitted`, `delivered`, `failed`, `dropped`, `pending`) so the
  fleet dashboard can surface mirror health.
- `chain.drain_sinks(timeout=)` flushes pending entries on shutdown.
- 16 new tests in `tests/test_audit_sinks.py` cover: local chain still
  works with no sinks; sink failure cannot corrupt local data; reserved
  headers cannot be overridden; HTTPS-only URL enforcement; env-driven
  construction; sink observability; multiple sinks all receive.

### A3 — Android bearer-token pairing ✅
The desktop server (`shugocore-server --host 0.0.0.0`) already enforces
`SHUGOCORE_SERVER_TOKEN` (fail-closed by default; `--allow-unauthenticated`
opt-in). The Android client side now matches: a `desktop_api_token`
SharedPreferences entry (typed as a password input on the SERVER tab)
forwards the token to `AndroidBackend`, which sends
`Authorization: Bearer <token>` on every HTTP request. This eliminates
the only legitimate reason for `--allow-unauthenticated` in production.

- `android_inference.py` — new `auth_token` parameter on `AndroidBackend`;
  every `generate` / `chat` / `list_models` / `get_health` HTTP call now
  passes the token to `_http_post` / `_http_get`. Empty/whitespace token
  is normalized to `None` (back-compat: existing installs keep working).
- `shugocore_agent.py` — `AndroidAgent.__init__` accepts an `auth_token`
  positional; `create_agent(...)` is wired 4-arg; `_initialize_engine`
  forwards the token to `AndroidBackend`.
- `ShugoCoreService.kt` — `fallbackAuthToken` reads `desktop_api_token`
  from the same `shugocore_prefs` file and passes it as the fourth
  positional arg to `py.getModule("shugocore_agent").callAttr(
  "create_agent", ...)`.
- `ServerPane.kt` — new password-masked `EditText` field below the URL
  field; persists on every text change.
- `MainActivity.kt` — new `onBackupTokenChanged(token)` callback
  implementation that trims and stores / removes the token in
  SharedPreferences.
- `NodeState.kt` — new interface method `onBackupTokenChanged(token)`
  on `ControlPlaneHost`.
- 3 new unit tests in `tests/test_android.py` cover token normalization,
  header presence on every HTTP call, and back-compat (no header when
  token is unset).
- All 1,036 tests pass; the Android tree-sync test passes after
  re-syncing the bundled Python sources into
  `platforms/android/app/src/main/python/`.

## Bucket B — Capability gaps in the CSFA loop

### B1 — Pluggable embedding backends (`vector_db.py`) ✅
- New `Embedder` Protocol (`embed` + `embed_batch`, runtime_checkable) with
  two implementations:
  - `HashingEmbedder` — dependency-free default; byte-for-byte identical
    vectors to legacy `hashed_embedding` (swapping never changes Tier 2
    search results).
  - `SentenceTransformerEmbedder` — optional, lazily-imported
    `sentence-transformers` wrapper (default `all-MiniLM-L6-v2`); missing
    dependency raises a clear `ImportError`.
- `make_embedder(name, dimension)` factory: unknown names and missing
  optional deps fall back to `HashingEmbedder` (never crashes the engine).
- `VectorDB` now accepts `config["embedder"]` ("hashing" / "sentence-transformer")
  or an injected `embedding_fn` callable/instance; explicit callable wins.
- 16 new tests in `tests/test_embedder.py` (protocol conformance,
  determinism, batch, factory fallback, mocked sentence-transformer, VectorDB
  wiring).

### B2 — Per-model backend pools with health routing (`model_backends.py`) ✅
- New `BackendPool`: a set of backends for one `model.id`; every call routes
  to the healthiest candidate. Health = `consecutive_failures / priority`
  (lower wins); failures ≥ `max_failures` open a per-backend circuit breaker
  for `cooldown_s`, then half-open for one trial. Deterministic tie-break by
  declared order — soaks reproduce byte-for-byte.
- `BackendPool` is a drop-in backend: `generate`, `chat`, `list_models`
  (union), `get_health`, plus `stats()` observability counters
  (`failures`, `successes`, `circuit_open_s`).
- `create_backend({"type": "pool", "backends": […], "max_failures": N,
  "cooldown_s": S})` builds a pool from per-backend configs; each entry may
  carry `priority`.
- 13 new tests in `tests/test_backend_pool.py` (fallback after failure,
  circuit breaker open/skip, deterministic tie-break, priority selection,
  union list_models, pool-via-create_backend).

### B3 — PostgreSQL/pgvector env switch (`decision_engine.py`) ✅
- New `_resolve_memory_source(memory_db_path)` helper: an operator can flip
  Tier 2 storage to the fleet-shared `PgSemanticMemory` backend purely via
  environment, without touching engine code:
  - `SHUGOCORE_MEMORY_DSN=postgres://user:pass@host:5432/db` → pgvector
    (highest priority; any other source string also honored via
    `open_semantic_memory` dispatch).
  - `SHUGOCORE_MEMORY_BACKEND=postgres` alone → fail-closed `ValueError`
    instructing the operator to set the DSN (a database address must never
    be invented).
  - No env → identical historical behavior (memory_db_path passthrough).
- 5 new tests in `tests/test_memory_source.py`.

### B4 — Entity/relation graph helpers (Tier 2, both backends) ✅
- `SemanticMemory.link_entities(fact_id, entities)` — explicitly attach
  relations beyond auto-extraction (planners can assert "fact N concerns
  X, Y"). Entities created on demand; linking idempotent.
- `SemanticMemory.query_subgraph(entity, depth, limit)` — bounded BFS
  relation walk returning `{root, depth, nodes:[{id,name,mention_count,
  hops}], edges:[{from,to,via_fact_id}]}`. Capped at `limit` nodes —
  a continuous agent can never traverse an unbounded relation graph.
- Same two methods added to `PgSemanticMemory` for API parity (SQL
  composed with `psycopg2.sql`, consistent with the B608 fix).
- 7 new tests in `tests/test_entity_graph.py` (idempotency, queryability,
  bounded shape, hop depth, hard limit, pg API parity over the mock
  connection).

## Bucket C — UX surface for human-in-the-loop and operations

### C1 — Human-approval console surface (`shugocore_server.py`) ✅
- `GET /api/v1/approvals` — pending side-effecting-action queue from the
  engine's `ApprovalBroker` (bounded to `APPROVALS_MAX_PENDING`, sanitized
  descriptions, `pending_total`, `ttl_seconds`). A bare engine reports
  `enabled: False` — never fabricated.
- `POST /api/v1/approvals/<id>/approve` and `.../deny` — operator-console
  resolution; idempotent (an already-resolved id returns `resolved: false`
  instead of poisoning the broker).
- **CSFA race fix in `ApprovalBroker._ask_operator` (policy.py):** the
  background human channel used to *overwrite* a programmatic
  approve()/deny() with its own late verdict. First resolution now wins
  (fail-closed either way) — the operator console and the human channel
  can race without corrupting the outcome.

### C2 — Fleet dashboard endpoint (`shugocore_server.py`) ✅
- `GET /api/v1/fleet` — bounded snapshot of paired mobile nodes via the
  engine's `MobileExecutionHandler` registry (device_id, alive, paired_by,
  expires_at, sanitized manifest). Handles registry via attribute or
  callable (`registry()`); a bare engine reports `enabled: False`.

### C3 — Bounded sensor live-stream endpoint (`shugocore_server.py`) ✅
- `GET /api/v1/sensors` — poll-based bounded sensor stream. Each poll
  records ONE sanitized sample (hosted agent's telemetry dict + selected
  status keys: memory_usage_mb, mesh_peer_count, mesh_peers, capabilities)
  into a fixed `SENSOR_STREAM_RING` (100) deque; returns the window so a
  dashboard can render a live stream without unbounded growth.
- 5 new tests in `tests/test_shugocore_server.py` cover disabled-bare-engine
  honesty, pending queue + resolution + idempotency, operator-late-verdict
  race pinning, fleet aggregation, and sensor-ring bounding/sanitization.

## Bucket D — Long-horizon infrastructure

### D1 — Termux llama-server launcher (`android_inference.py`) ✅
- `find_llama_server()` locates a llama-server binary: `$PREFIX/bin/llama-server`
  (Termux package), `shutil.which` (PATH / adb shell), or the explicit
  `SHUGOCORE_LLAMA_SERVER` env override.
- `TermuxLlamaServer` owns one child: fail-closed `start()` (refuses when no
  binary or no model), `running()` process liveness, idempotent `stop()`.
  The agent can thus work against a pkg-installed llama.cpp with zero
  native JNI code in the Python layer.
- 8 new tests in `tests/test_android.py` (env override, Termux prefix,
  PATH fallback, absent -> None, fail-closed refuses, spawn/stop lifecycle,
  spawn-failure handling, extra-arg pass-through).

### D2 — NPU bring-up rungs on the acceleration ladder (`acceleration.py`) ✅
- `enumerate_linux` now probes Qualcomm Hexagon NPU (QNN HTP / cdsprpc
  vendor libraries: `libQnnHtp.so`, `libcdsprpc.so`, …) and MediaTek APU
  (`/dev/accelerator*`, `libmvpu.so`, `libapu_mdl_drv.so`), emitting
  `npu` AcceleratorDevices with a `detected_by` detail. The CPU rung remains
  the always-available floor; these rungs only ADVERTISE real silicon for
  bring-up tooling (deep QNN/MTK inference integration remains).
- 5 new tests in `tests/test_acceleration.py` (each probe path, absence case).

### D3 — Per-agent memory policies (`memory_system.py`) ✅
- `MemoryManager(policy=...)` profiles:
  - `"shared_rw"` (default) — historical behavior: shared Tier 2/3 + write
    consolidation.
  - `"shared_read"` — shareable/queryable Tier 2 but the agent's
    consolidation worker never writes (drains Tier 1 in place; enforced in
    `check_write_permission` and `_consolidate_impl`).
  - `"isolated"` — fresh Tier 2/3 always; explicitly passed shared
    instances are refused with a warning (no cross-agent leakage possible).
- Unknown policies raise `ValueError` at construction (fail-closed).
- 7 new tests in `tests/test_memory_policies.py`.

### D4 + D5 — CI trusted publishing, SBOM, signed artifacts ✅
- New `.github/workflows/release.yml` (runs on `v*` tags):
  - Builds wheel + sdist,
  - generates an SPDX SBOM (anchore/sbom-action),
  - signs every artifact with Sigstore (sigstore/gh-action-sigstore-python),
  - publishes to PyPI via `pypa/gh-action-pypi-publish` with `attestations:
    true` — OpenID Connect trusted publishing, **no static PyPI token**,
  - attaches the SBOM + signatures to the GitHub release.

## Server hardening (landed with this release)

- **Strict loopback check** (`shugocore_server.py`): `_is_loopback_host`
  now requires a numeric IPv4 address inside `127.0.0.0/8` (via
  `ipaddress`); the old `startswith("127.")` prefix test wrongly
  trusted attacker-controlled DNS names like `127.evil.com` and
  `127.0.0.1.nip.io`. CORS origin validation uses the same helper.
- **Prefix-route auth-first**: suffix-trick paths (`/evil/health`,
  `/api/v1/task/health`) never reach real handlers — exact-route match
  only, 404 for unknown paths, and 401-first on anonymous probes of
  authed surfaces (no 404-vs-401 route oracle). Query strings are
  tolerated on real routes (`?x=1` routes like the bare path).
- **Approvals/fleet tolerance**: the approvals console skips malformed
  broker entries; the fleet registry tolerates bare engines and unknown
  node payloads instead of 500ing.
- 8 new tests in `tests/test_shugocore_server.py`; the Android
  bundled-tree mirror is re-synced byte-identical.

## Suite

- Full suite: **1,105 tests passing** (verified on this tree: ruff clean,
  bandit-high clean, CSFA host soak STABLE — 118 ticks, 6/6 checks)
  including the new audit-sink, embedder,
  backend-pool, memory-source, entity-graph, C-surface, memory-policy,
  Termux-launcher, and NPU-probe tests. The Android tree-sync guard passes
  after re-syncing all changed Python modules into
  `platforms/android/app/src/main/python/`.

