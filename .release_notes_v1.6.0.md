## v1.6.0 — Dream Consolidation, Memory Write Gates & Simulation Framework

### Added

**Dream Consolidation**
- `DreamConsolidation` class — periodic reflective pass that compresses episodic experiences into durable identity insights (inspired by GrowBot's "dream" phase)
- Insight extraction from patterns: recurring failures (≥2 occurrences) and consistent successes (≥3 occurrences)
- Clamped identity mutations: max 1 sentence added per dream, identity never falls below minimum length
- Dream is the SOLE writer of Tier 3 mutations during normal operation (code-enforced)

**Memory Write Gates**
- Code-enforced write permissions per memory tier:
  - Tier 0 (Scratchpad): Only scratchpad writes
  - Tier 1 (EpisodicMemory): Only episodic record (append-only)
  - Tier 2 (SemanticMemory): Only consolidation/maintenance worker
  - Tier 3 (CoreIdentity): Only dream consolidation or explicit promotion
- `check_write_permission()` / `enforce_write()` with `PermissionError` on violation

**Simulation Framework**
- `simulation/` module with MuJoCo backend and stub fallback
- Robot models: Berkeley Humanoid Lite, Reachy2, Unitree G1
- Test scenarios: WalkToTarget, BalanceTest, EmergencyStop
- `run_benchmark()` for public test data generation
- `pip install 'shugocore[simulation]'` for MuJoCo support

**Integration**
- Dream consolidation runs automatically in continuous agent loop
- Dream stats exposed via `status()`
- 174 tests passing

**Documentation**
- Updated README with simulation framework and file structure
- CHANGELOG entries for v1.5.0 and v1.6.0
