# ShugoCore v1.30.0 — fleet memory mesh + on-device structured inference

Minor release. **No public API change**; every addition is opt-in or a
backward-compatible default. Full suite: **1005 tests passing** (Python 3.9–3.13).

Two capabilities that were previously a façade are now real, and the
on-device agent loop is model-driven instead of rule-driven.

## ShugoNet memory sharing is real (was a stub)

A cross-agent `query` returned an invented `"stub from <agent>"` fact and
`sync` merely acknowledged, so a two-device "combined memory" could not exist.

- **Memory-backed `query`** — answers from the peer's `MemoryManager`
  (`retrieve_context`); with no memory backend the reply is empty, never made up.
- **Incremental `sync`** — the peer exports Tier 2 facts created after the
  caller's watermark; the caller merges them locally and advances a per-peer
  watermark, so repeats transfer nothing. Returns
  `{received, imported, duplicates}`.
- **Provenance + isolation** — only Tier 2 crosses the mesh (Tier 0/1 private,
  Tier 3 read-only identity); imports are idempotent and tagged
  `shared_from` / `shared_at`.
- **Bounded links** — `reconnect_peers()` (plus a bounded periodic pass) re-dials
  a peer whose startup dial raced the other node's listener.
- **Conflict guard** — duplicate-heavy syncs trip the existing
  `memory_sync_conflict_storm` fallback.
- **Join a mesh** — `SHUGOCORE_MESH_PEERS` on hosts, `mesh_peers.json` in the
  Android app data dir; on-device **"sync your memory with your peer"** merges a
  peer's memory and reports the imported count.
- Fixed a latent self-deadlock in `_PeerConnection.connect()`, a transport bug
  where request/response replies could never be read, and a phantom-port bug
  when the peer server failed to bind.

Verified between an A51 and a Tab S9 FE: **64** facts imported one way, **2** the
other, both recallable through each agent's own memory API.

## On-device structured inference

The Android model path had three independent faults, all fixed:

- **No `.gguf` staged** → the loopback API server never started and every call
  logged `URLError` forever. Model staging is now the documented prerequisite.
- **`SIGILL` crash loop** — the build forced `GGML_CPU_ARM_ARCH=armv8.2-a+dotprod`,
  which compiles dotprod instructions into kernels that are *not* runtime-gated;
  any arm64 SoC without `FEAT_DotProd` (Exynos 9611) aborted on the first
  Q4_K_M matmul. The default is the portable baseline;
  `-Pshugocore.dotprod=true` opts in for capable fleets.
- **Unparseable output** — the 0.5B model emitted loose dialects
  (`action_type: speak`, `name: {json}`) that never parsed, so 100% of decisions
  fell back to rules. The decision path now sends a **GBNF grammar derived from
  the engine's own action schema**, and llama.cpp constrains decoding to it.

Result on the A51: decisions are predominantly `proposal_source: <model>` with
schema-valid JSON, down from 100% `rule_fallback`. Measured ~30 s/decision
(Exynos 9611, 0.5B Q4_K_M, baseline); a Tab S9 FE with dotprod runs a **1.5B**
model at ~23 s/decision.

## Execution dispatch consults the handler registry

`ExecutionLayer._dispatch()` was a hardcoded chain that never looked the handler
registry up for network/mobile/robotics/custom types, so every pluggable handler
was advertised to the model (and to the new grammar) and then rejected with
`Unknown action type`. Dispatch now falls back to the registry; unregistered
types still fail closed with the same message.

## Tests

+20 tests: `tests/test_memory_sharing.py` (two-agent combined memory over real
TCP, provenance/idempotency, reconnect, conflict storm), `tests/test_grammar_decoding.py`
(grammar shape, backend mapping, capability fallback, prompt steering) and
dispatch-registry tests in `tests/test_shugonet.py`. New live harness:
`tests/two_agent_memory_smoke.py` (local two-agent mode, or `--remote host:port`
against an Android node via `adb forward`).

## Install

```
pip install shugocore==1.30.0
```
