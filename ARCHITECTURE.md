# Architecture

Where things live, what starts what, and which rules the code must keep. This is
the map the repository did not have: the framework was documented in prose across
a 77 KB README and a 250 KB CHANGELOG, and the lack of a single map is what let
four composition roots drift apart (see `state_paths.py` and
`tests/test_composition_parity.py`).

Read the README for *what ShugoCore is*; read this for *how it is put together*.

## The shape of the tree

| | |
|---|---|
| **Root** | flat `py-modules` — no top-level package. `pyproject.toml` enumerates them by hand; `tests/test_packaging_manifest.py` fails when the list and the tree disagree |
| **Subpackages** | `benchmarks`, `conversation`, `kv_mesh`, `nrr`, `personality`, `prompts`, `simulation`, `sound`, `subsystems` |
| **Clients** | `clients/desktop/shugocore_desktop.py` (the Tk control plane), `clients/android/` |
| **Platform** | `platforms/android` (Chaquopy app + a byte-identical Python mirror), `platforms/godot` (XR surface) |
| **Tools** | `scripts/`, `runtime/tools/`, and the root CLIs (`continuous_agent.py`, `android_node.py`, `check_apk.py`) |
| **Guards** | `tests/test_android_tree_sync.py` (mirror is byte-identical), `tests/test_packaging_manifest.py` (wheel matches tree), `tests/test_module_reachability.py` (nothing is stranded), `tests/test_composition_parity.py` (the roots agree) |

## Composition roots

**A composition root is anything that builds a `DecisionEngine`.** There are
several, deliberately — a desktop server, a daemon, an on-device node and an
agent embed the engine in different shapes — and they are the highest-risk seam
in the codebase, because a guarantee added to one is not automatically in the
others.

| Root | Entry | Reaches the engine via |
|---|---|---|
| **Agent** (canonical) | `shugocore_agent.create_agent` | `AndroidAgent` — used by the Android service, the desktop UI and the `scripts/verify_*` tools |
| **Server** | `shugocore_server.py` (`shugocore-server`) | `build_engine()` (`:1189`) |
| **Daemon** | `continuous_agent.py` (`python3 continuous_agent.py`) | `ContinuousAgent.__init__` (`:80`) |
| **Device node** | `android_node.py` (`--role`) | `AndroidShugoCoreNode` (`:388`) |
| **Control plane** | `clients/desktop/shugocore_desktop.py` | calls `create_agent` |
| **Containment harness** | `actuation_sandbox.py` | `build_engine()` (its own, `:272` — same name, different purpose) |
| **Benchmarks** | `benchmarks/run.py` | `_build_engine()` |

### The state-path rule

Where a node keeps its state is a property of the **node**, not of the process
that started it. `state_paths.anchor()` is the single answer:

* an absolute path is never second-guessed (the agent root, an operator's
  `--memory-db-path`, a fleet DSN);
* a relative name is anchored **once, at construction**, under the data dir when
  there is one, otherwise under the working directory at that moment;
* `:memory:`, SQLite URI filenames and Postgres DSNs are addresses, not
  locations, and pass through untouched.

`DecisionEngine` applies it to `memory_db_path`, `audit_path`,
`episodic_journal_path` and its log file, and exposes them — so every root
inherits the rule by reaching the engine. Before this, only the agent root
anchored anything: the others wrote `semantic_memory.db`, `audit_chain.jsonl`,
`node_audit.jsonl` and a `decision_engine.log` into whatever directory they were
started from.

**If you add a composition root, it gets the same treatment for free as long as
it constructs `DecisionEngine` — and `tests/test_composition_parity.py` will fail
if it produces relative state paths or an ungated engine.**


## The engine and what surrounds it

`DecisionEngine.execute_task` is the **single gated path**. Interactive tasks,
autonomous cycles and the task queue
(`TaskManager.set_executor(self.execute_task)`) all go through it. Nothing else
executes side effects.

| Concern | Module | Enforces |
|---|---|---|
| Governance verdicts | `policy.py` | the policy verdict token; ungated actions are refused |
| Operator consent | `policy.ConsentRegistry` | a side effect needs a grant only an operator can issue |
| Approval channel | `policy.ApprovalBroker` | pending/denied approvals refuse |
| Capability allowlists | `policy.CapabilityRegistry` | unknown hosts/commands refuse (fail-closed) |
| Execution | `execution_layer.py` | the only place a tool/API call happens |
| Audit | `audit.py` | hash-linked chain (optional HMAC), env-driven sinks |
| Memory | `memory_system.py`, `pg_memory.py` | Tier 0/1 private, Tier 2/3 shareable |
| Loop safety | `state_machine.py`, `fallbacks.py` | governor state, PAUSED/SAFE_STATE/HALTED |
| Cognition | `model_backends.py`, `subconscious.py`, `model_manager.py` | backend selection, aggregation |
| Hive | `mesh_election.py`, `mesh_rpc.py`, `mesh_model_host.py`, `delegation.py`, `agent_runtime.py` | one primary, layer-split, peer dialing, ShugoNet |
| Fleet | `mobile_nodes.py`, `fleet_deploy.py`, `fleet_onboard.py`, `node_identity.py` | pairing, rollout, onboarding, identity |
| Perception/actuation | `human_interaction.py`, `sound/`, `nrr/`, `robotics_handler.py`, `ros2_interface.py` | speech, audio, rendering, motion |
| Surfaces | `shugocore_server.py`, `clients/desktop/`, `platforms/godot/` | the Ollama wire contract, the engine API, the terminal, XR |

## Invariants (the security model, from `SECURITY.md`)

1. **Single gated path** — no root may bypass `execute_task`, the Tier 3 gate,
   consent, approval, the verdict token or the capability allowlist.
2. **Fail-closed** — a missing verdict, consent, approval channel, unknown host
   or unknown command refuses; it never falls back to allow.
3. **Tier 3 is read-only at runtime** — only `promote_to_core()` with operator
   attribution may mutate it.
4. **Auditability** — every block/approval/execution is appended to the chain,
   and an audit-write failure must be visible, never swallowed.
5. **Secret hygiene** — secrets resolve at execution time and never reach
   decision dicts, logs or HTTP responses.
6. **Network surfaces** — `shugocore-server` is loopback by default and refuses a
   non-loopback bind without a token.

`tests/test_composition_parity.py` asserts that every root's engine still carries
its gates; `actuation_sandbox.py` proves the containment property end to end
(17 scenarios, refusals never reach the wire).

## Derived artifacts — never edit the copy

| Artifact | Source | Guard |
|---|---|---|
| `platforms/android/app/src/main/python/**` | the repo-root modules | `test_android_tree_sync.py` — byte-identical, or the suite fails with the `cp` to run |
| the published wheel | `pyproject.toml` `py-modules` / `packages` | `test_packaging_manifest.py` |
| the Android APK | `platforms/android` + the mirror + fetched ORT/audio assets | `tests/test_nrr_jni_contract.py`, `test_sound_jni_contract.py`, `test_quest_build.py` |
| `version.py` / `pyproject.toml` / `build.gradle` / README badge / CHANGELOG | each other | `test_packaging_manifest.py`, `test_sound_provider_contract.py::TestVersionTruth` |

**Adding a module means:** add it to `py-modules`, mirror it if the on-device
import graph needs it, and keep it reachable (or justify it in
`test_module_reachability.py`).

## Verification: which command actually runs the suite

`.github/workflows/ci.yml` runs the tests **twice**, on purpose:

```bash
python -m unittest discover -s tests -v    # the TestCase suites
python -m pytest tests -p no:unittest -q   # the files unittest discovery cannot see
```

`unittest discover` collects `TestCase` subclasses only, so pytest-style files are
invisible to it, and a module-level `pytest.importorskip` is reported as an
*error* rather than a skip. Neither runner alone equals CI. Measured at the
1.30.27 review (they drift as the suite grows; `--collect-only` is one command):

| command | sees |
|---|---|
| `pytest tests` | 2250 (re-collects the unittest ones) |
| `pytest tests -p no:unittest` | 30 |
| `python -m unittest discover -s tests` | 2220 |

There is no single command that reproduces CI exactly; run both, as CI does.
`tests/test_suite_completeness.py` fails if a `tests/test_*.py` file is
collectable by *neither* runner — the way a suite silently stops running.

### The instruments

`claim_matrix.py` is the feature-verification instrument: 22 claims, each with
checks, a captured artifact under `runtime/evidence/`, and a verdict that is
`proven`, `unproven` (could not evaluate — never the same as failed) or `failed`.

```bash
python claim_matrix.py --only mesh.election      # one row, with its evidence
python claim_matrix.py --json                    # all of them
python capability_matrix.py --host-dir runtime/desktop
python actuation_sandbox.py                      # 17 containment scenarios
shugocore-verify-audit verify audit_chain.jsonl  # audit-chain integrity
```

Run them **per claim** rather than all at once: the whole-matrix run died
mid-sweep once and took its buffered output with it
(`runtime/phase0_evidence/run_claims.ps1`).
