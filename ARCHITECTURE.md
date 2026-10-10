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
| **Host node** | `scripts/desktop_agent.py` | calls `create_agent` — the headless non-Android fleet node (Windows/macOS/Termux) |
| **Control plane** | `clients/desktop/shugocore_desktop.py` | calls `create_agent` |
| **Containment harness** | `actuation_sandbox.py` | `build_engine()` (its own, `:272` — same name, different purpose) |
| **Benchmarks** | `benchmarks/run.py` | `_build_engine()` |

`scripts/agency_session.py` also calls `create_agent`, but it is the *agency
instrument* the claims matrix drives rather than a deployment root.

`scripts/desktop_agent.py` is on this list because **it is the only home of the
persona (phrasing) layer.** `create_agent(persona_shaper=...)` is optional and
deliberately off by default — "optional phrasing model elsewhere in the hive
(`persona.py`). Off unless an [explicitly provided]" (`shugocore_agent.py:221`) —
and the headless host node is the one entry point that supplies it, through
`--persona-url` / `--persona-model` / `--persona-recheck`. So the Android service,
the server, the daemon, the device node and the Tk control plane all run with
`decision_engine._apply_persona` inert. That is the design, not an oversight: a
shaper makes every spoken line a model round trip, so it is opt-in. The omission of
this entry point from an earlier version of this table is what made the persona
layer look orphaned.

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

## The personality model

Four layers, each with one job, and the boundaries between them are the point:
personality decides *how the node speaks*, never *what it does*.

| Layer | File | Job |
|---|---|---|
| `PersonalityProfile` | `personality/loader.py` | editable character config: `name`, `traits` (prose), `speech` (`style`, `formality`, `max_sentences`, `first_person`, `never_say`), `boundaries`, `proactivity`. Read from `personality.json`; missing or malformed falls back to defaults, so *the agent always has a character* |
| `PersonalityModel` | `personality/model.py` | the **living** model: a vector of `TraitState(value, confidence, evidence_count, last_gen)` with a generation counter, born via `genesis(profile)` and moved by `apply_delta(...)` |
| `PersonalityGovernor` | `personality/governor.py` | annotates a proposed decision and returns a `PersonalityVerdict(verdict, tone_score, verbosity_ok, appropriateness, adjustments, route_to, reason)` where `verdict ∈ {pass, modify, reroute}` |
| `PersonaShaper` | `persona.py` | phrases a spoken line through an OpenAI-compatible endpoint. **Optional and off by default** (see Composition roots) |

### Two files, and which is which

| File | Holds | Written by |
|---|---|---|
| `personality.json` | the *rendered profile* — what the prompt is built from | an operator (it is config) |
| `personality_model.json` | the *living model* — traits, confidences, generation, history | the growth cycle, via `PersonalityModel.save()` |

At boot the agent loads the living model (`PersonalityModel.load`), and only if
that is absent does it call `genesis(profile)` and save — so a restart continues a
character rather than resetting one. `traits` in the profile are then **replaced**
by `PersonalityModel.as_profile()` on every growth, which is what makes new growth
live rather than waiting for a restart.

### The growth cycle

`GROWTH_EVERY = 25` conversational turns is one generation. Within a window the
agent counts turns, feedback, questions, commands, new facts and tool failures
(`_growth_observe` / `_growth_note_intent` / `_growth_note_failure`), and at the
window's end `grow_from_memory()` turns those stats into deltas
(`synthesize_deltas`, with `extract_feedback` reading praise and complaints from
the transcript), applies them clamped by the model, reports the before/after
comparison and drift, persists, and refreshes the rendered profile.

`grow_from_memory` is explicit that it does not persist — *"The caller is
responsible for persistence"* — and the agent is that caller. Growth is judged by
`agency.personality_growth`, which requires **both** the turn-window marker and a
`grew gen N -> M` line with `M > N`: a generation that cannot be attributed to a
window is not evidence of anything.

### The policy is frozen at genesis

`genesis()` extracts a *policy* from the profile (the `never_say` list, the
boundaries) and it does not move afterwards — `test_policy_never_moves` and
`test_policy_survives_growth_across_restart` pin it. Growth can change warmth,
curiosity or formality; it can never grow a node out of its boundaries.

### Annotate-only, and where that is enforced

`DecisionEngine._apply_personality` runs the governor, attaches
`decision["personality_verdict"]`, and returns the decision — then
`_apply_persona` shapes the phrasing. The rule the code states is that personality
*"can only restrict/modify, never pre-approve"*, and the tool path is annotated
with `advisory_only=True`, which means **never modify**. Speech is the only
passenger. `tests/test_persona.py` pins it from both ends:
`test_a_tool_action_is_never_phrased` ("only speech goes near the persona model")
and `test_the_tool_path_stays_unshaped_even_for_speech` ("advisory_only means
'never modify'"). A shaper that raises is style-only, and an unavailable persona
leaves the line alone.

### `proactivity`: permission from config, readiness from growth

The three switches in `config/personality.json` — `greet_on_arrival`,
`ask_follow_up`, `offer_help` — are the operator's **permission** for the agent to
originate speech rather than only answer it. They were once parsed, merged and read
by nothing. They are now consumed in three places, and the third is the one that was
silently losing them:

| Where | What it does |
|---|---|
| `personality_system_prompt` | renders each switch as an instruction; `offer_help` switched off is stated outright — "Do not volunteer help that was not asked for" |
| `PersonalityModel` | freezes the block into `policy` at `genesis` and returns it from `as_profile()`. This is the step that mattered: the agent **replaces** `self.personality` with `as_profile()` at boot, so a block dropped there is config an operator set and nothing reads |
| `ShugoAgent.proactive_permitted` | gates what the agent itself says — fail-closed, so an unknown kind is never a permission |

The one behaviour with a real trigger is an **arrival**: the attention layer
transitioning into `attending` ("human present AND attending to the agent") from
anything else. One unprompted line is spoken per arrival, under `greet_on_arrival`;
with greetings switched off, that same trigger will instead offer help when
`offer_help` allows it. The *edge* is what fires, so someone already in frame is
greeted once rather than once per tick, and a 120 s cooldown rides out the
attending/diverted flapping that face detection produces. What gets logged is what was
*delivered*, not what was chosen: `_speak_direct` refuses on a node that may not
announce to the room, so a follower records the arrival as `(not delivered)` instead
of leaving a greeting in the log that never left the node. `ask_follow_up` is
deliberately not an utterance: a follow-up question belongs inside the model's own
reply, so it is consumed by the prompt rather than by injecting a second spoken line.

Permission and *readiness* are different questions, and only one of them is the
operator's. `offer_help` additionally requires the learned `proactivity` trait to
clear `PersonalityGovernor.policy["proactivity_threshold"]` (0.40) — the same value
the governor already thresholds for self-initiated speech. A fresh node sits at 0.30,
so a permitted-but-unready agent stays quiet until it has grown into the offer. A
greeting needs permission only: the operator asked for greetings by name, and it is a
social convention rather than volunteering. The name still collides with the learned
trait `model.traits["proactivity"]` — that is the readiness half, and it is what gates
the offer.

Measured (`runtime/phase0_evidence/proactivity_probe.py`): the switches survive boot
into `agent.personality`; an arrival greets exactly once across repeated ticks;
`greet_on_arrival: false` is silent; `offer_help` stays silent at trait 0.30 and
speaks at 0.50; the cooldown holds an immediate re-arrival; an unknown kind is denied.

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

The three-state verdict is the whole point, and the middle state is the one that
gets lost. A live checker reads a captured transcript, so it has to say what it can
know from *nothing*: `True` only when evidence exists and supports the claim,
`False` only when evidence exists and contradicts it, and `None` — `unproven` — when
the run never produced the evidence. Getting that third state wrong is not
symmetric. `model_backed` and `world_engagement` once answered an empty transcript
with `False`, recording a claim as *contradicted* because a machine was not set up.
Two others answered it with **`True`**: `phone_quiet("")` returned
`no local model calls` and `no_third_party_egress("")` returned
`no external host appears in the session at all`, so an empty log *proved*
`orchestration.top_down` and `privacy.no_third_party_egress`. Proving a claim
because nothing was seen is the one thing this instrument promises never to do.
All fourteen checkers now take the empty case through `nothing_to_judge()`, and
`tests/test_claim_matrix_agency.py` asserts the contract for every entry in
`LIVE_CHECKS` rather than one checker at a time.

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
