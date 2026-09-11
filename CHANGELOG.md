# Changelog

All notable changes are documented here. This project adheres to
[Semantic Versioning](https://semver.org). The 1.0.0 public API surface is
frozen: no breaking changes across any 1.x release.

## [1.28.2] - 2026-09-09 — personality governor + KV-cache mesh split design

### 16 KB sanitizer budget (`mobile_nodes.py`) — Android compatibility
- `_MAX_SNAPSHOT_BYTES`: 4096 -> `16 * 1024`. Structured payloads (NRR
  frame descriptors/results, batched detections) can legitimately exceed
  the old 4 KB cap while remaining bounded; unbounded blobs (>16 KB) are
  still refused before they reach memory or decisions.

### Capability-aware compute routing (`mobile_nodes.py`) — NRR pattern
- Pairing manifests now carry a bounded `compute_caps` block (`fp16`,
  `int8`, `vram_mb`, `workloads`) sanitized at `pair()` time -- the NRR
  capability-matrix pattern applied to the ShugoCore fleet layer.
- `MobileNodeRegistry.nodes_for_workload(workload)` lists paired nodes
  advertising a workload; `MobileComputeBroker.request_compute()` refuses
  (fail-closed, audited) unknown workloads and devices that never
  advertised the requested workload. `KNOWN_WORKLOADS` =
  `("nrr_render", "vision")`.

### NRR contract shim (`nrr/`) — new, offline / capability-gated
- Descriptor-level mirror of the NRR frame contract
  (SamurAI-Official/NRR `specification/frame_contract.md`): the primary
  sends a lightweight `NRRFrameDescriptor` (resolution, pixel format,
  model/ref ids, temporal `frame_index`/`delta_time`) -- never raw pixels.
  The peripheral worker runs `nrr_render()` locally and returns an
  `NRRRenderResult` (output handle + render stats).
- `nrr/schema.py` / `descriptor.py` / `result.py`: versioned dataclasses
  with fail-closed validation; `nrr/protocol.py`: topic tails + message
  makers under the existing `/shugocore/mobile/{device_id}/` namespace;
  `nrr/adapter.py`: `NRRTransportAdapter` (capability-gated dispatch over
  `MobileComputeBroker`, inbound result validation, `worker_stub()` that
  answers `not_supported` until a real NRR backend exists).
- No NRR binary linked; no pixels cross the mesh; unpaired devices refused
  on both directions; every refusal audited.

### Temporal skip-gate for reasoning (`attention_layer.py`)
- `AttentionLayer.should_regenerate(observation)` /
  `mark_regenerated(observation)`: rolling SHA-256 over the
  decision-relevant observation slice (speech source, face count, recent
  speech, attention state, scene verdict, mesh peer count). Unchanged
  observations inside the 30 s dedup window return
  `(False, "observation_unchanged")` so the primary can reuse the last
  decision instead of re-running a minute-scale generation -- the NRR
  temporal-coherence pattern applied to LLM invocations. Fresh speech,
  instruction verdicts, transcripts, and events always force regeneration;
  hashing errors fail open to regeneration.

### NRR detector proposal (`docs/nrr_detector_proposal.md`) — new
- Cross-repo, non-blocking: proposes a `neural_detector` model type, an
  `NRRDetectionArray` output struct (label/confidence/bbox/frame_index/
  scene_verdict, no pixel bytes), and `int8_quantization` /
  `perception_output` capability flags for the NRR spec, with the
  ShugoCore-side convergence path (`nrr_detect` workload, dual-envelope
  migration, local SceneContext fallback).

### Personality governor (`personality/governor.py`) — new
- The personality model is no longer only a prompt-shaping input (`as_profile()` -> system prompt). It is now a **first-class reasoning signal** the decision engine consults on every proposed action: `proposed action -> governor.annotate() -> PersonalityVerdict -> governor.apply() -> (possibly rerouted) action`.
- **`PersonalityGovernor`** wraps the living `PersonalityModel` and exposes a small structured policy derived deterministically from its trait vector + frozen policy (no LLM calls, no I/O -- `annotate()` is O(text-length) string ops, safe for the 1 Hz hot path).
- **`annotate()`** scores a proposed action on three axes and returns a `PersonalityVerdict` (`pass` | `modify` | `reroute`): **verbosity** (sentence count vs a trait-derived budget; over-budget -> truncate), **tone/register** (contraction density vs the formality trait), and **appropriateness** (frozen-policy `never_say` / boundaries hits -> reroute to a safe fallback). A **proactivity** check reroutes self-initiated speaks to `ask_user` when the proactivity trait is below threshold.
- **`apply()`** is a pure function of action + verdict: it can modify params (truncate text) or reroute the action type (e.g. `speak` -> `ask_user`). It never re-consults the model.
- **Safety invariant (non-negotiable):** the personality governor governs a *different axis* than the safety governor (`ExecutionGovernor`) and the policy gate (`ApprovalBroker` / `ConsentRegistry` / `CapabilityRegistry`). Those remain dominant on the harm / consent / approval axis. The personality governor can only RESTRICT or MODIFY, never AUTHORIZE: a personality PASS does not override a policy BLOCK; a personality MODIFY cannot reclassify a side-effecting action as safe; a personality verdict never unlocks consent or approval.

### Decision engine (`decision_engine.py`)
- New optional constructor param `personality_governor`. When attached, both decision paths consult it: `_make_conversational_decision()` runs full annotate + apply (personality can modify/reroute conversation actions); `make_decision()` (tool path) runs annotate-only (`advisory_only=True`) so tool actions stay gated exclusively by the safety governor while personality advises/audits.
- Every governed decision carries a `personality_verdict` field (the `PersonalityVerdict.to_dict()`) and journals a `personality_verdict` memory event -- the personality signal is as auditable as the safety signal. When no governor is attached, the engine behaves exactly as before (opt-in, fully backwards compatible).

### On-device app
- `shugocore_agent.py` constructs a `PersonalityGovernor` from the living `PersonalityModel` at bootstrap and injects it into the `DecisionEngine`. Because the governor holds a reference to the living model (mutated in-place on growth), it tracks new generations automatically.

### KV-cache / context mesh split (`kv_mesh/`, `docs/kv_cache_mesh_split.md`) — new
- **Design doc** (`docs/kv_cache_mesh_split.md`) -- Option 1: split the transformer KV cache and context windows across peripheral Android devices (host computes, peripherals contribute RAM).  Covers the shard model (`KVShard`, `ContextShard`, `ShardSpec`), memory accounting (KV size = L*S*2*H*D*B), three split strategies (sequence-parallel context split, KV-offload, head-split), the kv contract protocol topics under the existing `/shugocore/mobile/{device_id}/...` namespace, the latency budget (viable for the 0.5B on-device target on a 2-3 node mesh), and the safety surface (consent-gated, no persistence past TTL, checksummed, fail-closed to host memory).
- **Offline prototype** (`kv_mesh/`) -- proves the protocol and memory accounting WITHOUT real distributed inference: `shard.py` (shard data types + partition into sequence-split or layer-split shards), `allocator.py` (`KVAllocator` assigns shards by advertised RAM, enforces capacity caps, rebalances on node join/leave, fail-closed), `protocol.py` (message types + topic routing for the kv contract), `simulator.py` (`MeshSimulator` + `SimNode` run assign/get/put/evict cycles over simulated nodes with RAM caps).
- Reuses the existing fleet layer: pairing/TTL, topic ACL, and the consent-gated compute-offload path.  A KV shard is just another contract topic; storing one is a privacy-relevant action gated the same way as other mobile compute.  Real on-device multi-node KV splitting (multi-instance llama.cpp + high-bandwidth activation transport) is explicitly a follow-on phase.
- `shugocore_agent.py` constructs a `PersonalityGovernor` from the living `PersonalityModel` at bootstrap and injects it into the `DecisionEngine`. Because the governor holds a reference to the living model (mutated in-place on growth), it tracks new generations automatically.

## [1.28.1] - 2026-09-09 — device smoke harness verified end-to-end on two Android devices (9/9 phases)

### Device smoke harness (`tests/android_device_smoke.py`) — now passes 9/9 on Tab S9 FE + A51
- **Transcript injection** — the payload is base64-encoded into a `text_b64` extra: `adb shell` word-splits every remote argument, and an earlier multi-word `--es text "…"` arrived as separate tokens (the bare `a` became the intent package and the broadcast was dropped). The receiver decodes it, so multi-word transcripts arrive intact.
- **Liveness** is proven by the 1 Hz `Decision made for task` agent-loop line in logcat (`python.stderr`) — there is no `python3` binary on-device (embedded Chaquopy runtime), so probing one via run-as was a false metric.
- **File reads** use `exec-out run-as cat <file>`: `sh -c` under `run-as` *hangs* on this device family (observed 90 s subprocess timeouts), and a bare `shell cat` is SELinux-denied. The personality phases read the real `personality_model.json` with `json.loads` (the old `shugo_core_prod_personality_model.json` name + `literal_eval` never matched what the app writes).
- **Restart phases** wait for the agent loop to actually resume (`wait_for_agent_loop`) before injecting — observations injected during Chaquopy re-initialisation were dropped ("no agent").
- **Timer teardown phases** use an 8-second timer so expiry deterministically lands *after* force-stop; **memory probes** use the deterministic `recall …` command instead of clarify-path phrasing; the **growth phase** drives the 25-turn `GROWTH_EVERY` cadence until `generation` advances, then asserts `compare_models` drift > 0 offline (repo root added to `sys.path` for the import).
- `run()` decodes logcat with `errors="replace"` — the ring can contain arbitrary bytes and previously crashed the probe with `UnicodeDecodeError`.

### On-device app
- **`ShugoCoreService.kt`** — re-added the runtime-registered transcript-injection broadcast receiver (`RECEIVER_EXPORTED`, `BuildConfig.DEBUG`-gated). A manifest-declared receiver was *blocking* delivery on one device (`BroadcastQueue: Background execution not allowed`) and silently dropped on another; the runtime receiver is the single reliable implicit-broadcast path on modern Android and is used exclusively now (manifest `INJECT_TRANSCRIPT` receiver removed).
- **`AndroidManifest.xml`** — removed the duplicated `ShugoCoreService` / `SensorPublisherService` service declarations and the shadowing manifest receiver.
- **`shugocore_agent.py`** (on-device copy) — accepted human observations and every spoken response (`_speak_direct`) are mirrored to stderr (`HUMAN-OBS …`, `SPEAK: …`) so headless probes and TTS-muted test devices can observe the decision path in `python.stderr` logcat — the agent's in-memory `self.log` buffer never reaches logcat on its own.

## [1.28.0] - 2026-09-08 — audio-source discrimination, conversational agent loop, subsystems + personality on device, device smoke harness

### Audio-source discrimination (new)
- **`PerceptionState.kt`** — scene taxonomy: `instruction_directed` (wake-word + command words), `verified_person`, `unattributed_audio`, `ambient_noise`, `person_present_ambient_noise`, `person_present_silent`. Voice energy alone can never grant ATTENDING (fixes video-playback false-positive that could mask consent).
- **`DeviceMeshManager.kt` / `SensorPublisherService.kt`** — shared-mesh sensor publisher, 200 ms streaming with adaptive thermal back-off, merged sensor/batch messages.
- **`ShugoCoreService.kt`** — synchronous mesh-peer push into the agent observation context on message arrival.

### Conversational agent loop (new)
- **`shugocore_agent.py`** — `_handle_conversational_input`, `_speak_direct`, `_select_conversational_model`, memory question answering, fact extraction per transcript, timer checking, growth observation hooks, graceful `shutdown()`.
- **`decision_engine.py` / `attention_layer.py` / `human_interaction.py` / `subconscious.py`** — wake-word-gated attention, speech directedness, intent extraction (`intent_subject` / `intent_verb`), TTS lifecycle hardening. All mirrored into `platforms/android/app/src/main/python/`.

### Subsystems + personality tree (new)
- **`subsystems/`** (command_router, dialogue, fallback, intent, memory, tools), **`personality/`** (loader, model, growth, prompt), **`conversation/manager.py`**, **`prompts/builder.py`**, **`config/personality.json`** — now committed at repo root and mirrored under `platforms/android/app/src/main/python/` so the on-device Chaquopy tree covers the full import graph.
- **Tests** — `test_subsystems_phase3/4/5`, `test_personality_model`, `test_personality_growth` (256 tests passing with control-plane + lifecycle suites).

### Device smoke harness (new)
- **`tests/android_device_smoke.py`** — dependency-injected, read-only on-device probe (`--device`, `--repeat`, `--phases`, `--tag`, `--list`, `--json`); 9 phases covering service-alive, timers, facts, restarts, teardown, personality genesis/growth. Split sources in `tests/smoke_part_*.py`, verified by `tests/build_smoke.py`.

### Android packaging
- **`AndroidManifest.xml`** — debug inject receivers (`INJECT_TRANSCRIPT`, `DEBUG_INJECT_SENSOR`) for headless on-device driving via adb.
- **`build.gradle`** — `versionCode 14`, `versionName "1.28.0"`.

## [1.27.0] - 2026-09-07 — peripheral-mode confirmation, sensor-agent state, mesh latency

### Peripheral-mode confirmation (new)
- **`CompanionPane.kt`** — reads `companion_mode` from the top-level node snapshot (not the Python `agent_status`, which is empty when the agent is stopped). The toggle now correctly reflects the active mode.
- **Live status line** — the COMPANION pane now shows `"Streaming sensors to <name> — connected"`, `"Peripheral — connecting…"`, `"Peripheral — link down; will retry"`, or `"Primary agent — N sensor agent(s) connected"`.
- **`ShugoCoreService.getNodeSnapshot()`** — exposes `sensor_agent_state`, `sensor_agent_target`, and `last_sensor_push_ms` so the UI can confirm a mode switch completed and show streaming freshness.

### Sensor-agent state networking (new)
- **`SensorPublisherService.kt`** — peripherals now broadcast `device_announce` (role + camera/mic/compute capabilities) on startup and `heartbeat` every 2 s so the primary knows sensor agents and their liveness.
- **`DeviceMeshManager.kt`** — `getSensorAgents()` returns peripheral peers that are online and not stale; `pushSensorAgents()` serializes and pushes the live snapshot immediately on every inbound sensor/message.
- **`ShugoCoreService.kt`** — `onSensorAgentsChanged` calls `pyAgent.update_mesh_peers()` synchronously, decoupling mesh awareness from the 1 Hz tick.
- **`shugocore_agent.py`** — `update_mesh_peers()` mirrors mesh data into both `self.telemetry` and `self.last_observation` so `get_status()` surfaces near-real-time mesh state.

### Latency reduction
- **Stream interval** 1000 ms → 200 ms (5×) on `SensorPublisherService`, with adaptive thermal back-off (>70 °C → 500 ms).
- **Merged sensor message** — camera + mic payloads sent in one `sensor/batch` message per interval (one serialize + one RFCOMM write instead of two).
- **Real-time mesh push** — mesh peer state reaches the agent's observation context on message arrival, not on the next tick.

## [1.20.0] - 2026-09-06 — attention verification layer, camera hardening, ShugoNet transport

### Attention verification layer (new)

- **`AttentionLayer`** (`attention_layer.py`): state machine with 4 states (UNKNOWN / ABSENT / ATTENDING / DIVERTED) that fuses face detection, gaze direction, speech directedness, and TTS state into a single verdict. Configurable freshness windows per signal. Thread-safe, provider-side (never imported by the decision core).
- **`ConsentRegistry.has_grant_for_attended_action()`** — consent-gated actions (speak, robot motion) now require attention to be ATTENDING or UNKNOWN (fail-open for uncertainty, fail-closed for ABSENT/DIVERTED).
- **`_gate_decision`** augmented with attention state from the task context.
- **`VERIFY_ATTENTION`** pipeline stage added between OBSERVE and GATE.
- **`decision_source`** in `get_status()` — tracks whether the last cycle was model-driven, no_action, policy_block, or rule_fallback.
- **`get_attention_state_json()`** — lightweight JSON endpoint for the Kotlin service to set camera attention mode.

### Camera stays active for gaze tracking (`VisionProvider.kt`)

- **Gaze extraction** from `FaceDetector.Face.pose(EULER_Y)` — stamps `PerceptionState.gazeTowardCamera` when yaw < 20° off-center. Gaze direction included in `HumanInteractionBus` visual observations.
- **Dynamic throttle** — `attentionMode` flag switches between 1 fps (normal) and 200 ms (attention mode) for higher gaze-tracking frame rate.
- **`PerceptionState.gazeTowardCamera`** and `speechDirectedAtAgent` signals added.

### ShugoNet TCP/JSON transport (`agent_runtime.py` — new)

- **`ShugonetAgentRuntime`** — minimal TCP/JSON transport implementing the `send()`, `query()`, `sync()`, `list_agents()`, `status()` contract expected by `ShugonetExecutionHandler`. NDJSON framing over TCP with background server thread for inbound connections.
- **CLI entry point** — `python3 agent_runtime.py --port 9000 --agent-id shugo-peer --peer peer2 host port`.
- **14 new tests** — 603 total, all passing.

### Intent extraction

- `human_context()` in `human_interaction.py` now includes `intent_subject` and `intent_verb` fields — lightweight heuristic from the most recent human conversation turn.
## [1.19.0] - 2026-09-06 — causal trace, multimodal fusion, unified perception, robot look (+ hardening)

### Hardening (v1.18 bugs fixed)

- **TTS per-utterance state machine** (`TtsProvider`): replaced `AtomicInteger pending` with `ConcurrentHashMap<String, Phase>` + `AtomicLong`. Late `onDone`/`onError` callbacks after `stopAll()` do `map.remove(id)` — `null` return = already cancelled → **pure no-op**. This fixes BOTH the negative-counter bug (queue-limit guard rot) and the hard-to-trigger late-callback that could re-arm the half-duplex barge-in gate mid-utterance. New `isFailed` flag for terminal TTS init detection.
- **Boot greeting lifecycle** (`ShugoCoreService`): split `bootGreetingDone` (too eager) into `bootGreetingScheduled` (set at agent init) + `bootGreetingDelivered` (set **only** after `tts.speak()` returns). Retries ~12 × 3 s up to 40 s, stops early on `tts.isFailed`.
- **Face echo-gate** (`NodeStatusHeader`): LISTENING now requires `lastMicActivityMs > ttsLastEndMs + 400 ms` — residual speaker audio after SPEAKING ends no longer triggers a false LISTENING.

### Unified perception state (`PerceptionState.kt`)

- Every provider stamps typed `PerceptionSignal<T>(value, tsMs, confidence, source)` — `micActive`, `voiceDetected`, `humanSpeech`, `transcription`, `visualPresence`. Faces uses `humanSpeech` for LISTENING ("a human is talking to it"), not mic-stream activity.

### Causal ID chain (conversation → turn → observation → decision → execution → response)

- Every `HumanObservation` carries an `observation_id` (`obs-N`); every `AgentResponse` carries a `response_id` (`rsp-N`) linked to its turn.
- `InteractionBus` manages `conversation_id` (UUID, rotated on USER_RETURNED), `turn_id` (per human-speech exchange, drained on agent response).
- Agent shell injects IDs into the decision context; `make_decision` mints `decision_id` (`dec-N`); `_execute_gated` mints `execution_id` (`exe-N`) and injects `_trace {execution_id, decision_id}` **before** `canonical_hash` — the policy token binds the lineage (tamper-evident by construction).
- `"Why did Shugo say that?"`: deterministic offline join over Tier-1 journal + audit chain, from response back to the exact observation.

### Multimodal fusion (vision × speech × memory)

- `HumanContext` fuses visual presence, voice/speech signals, bounded conversation turns, and Tier-2 entity-graph facts (`facts_about(...)` — the "Markus was doing Y" leg). Each modality with freshness window + confidence.
- Fusion slots: `gaze`, `attention_target`, `environment` — honest `None` until the real sources are wired.

### Robot transport extended

- `robot_look` (head pan/tilt) added to `ROBOTICS_ACTION_TYPES` (consent-gated — moving a camera is a physical act). Executor publishes to `/head_controller/follow_joint_trajectory` with safety bounds (±86° pan, ±57° tilt).
- Verb normalization: gripper accepts `grasp`/`release` params.
- Every robotics execution inherits causal IDs through `_execute_gated`.
## [1.18.0] - 2026-09-06 — voice & presence: Shugo speaks in complete sentences and shows its face

### Hearing — the recognizer IS the listener (self-cutoffs and clipped sentences fixed)

- **Persistent recognition** (`AudioProvider`): the v1.17 VAD→handoff design
  cold-started the recognizer after every onset (clipped sentence beginnings)
  and default endpointing closed the utterance on the first pause (clipped
  ends). The on-device `SpeechRecognizer` now holds the mic continuously and
  restarts AT ONCE on the same instance after each result — it is already
  listening when the user starts talking. Our energy VAD survives only as the
  honest FALLBACK (no on-device recognizer → speech-detected events, as in
  1.14).
- **Endpointing hints**: `COMPLETE_SILENCE 1600 ms / POSSIBLY_COMPLETE
  900 ms / MIN_UTTERANCE 2500 ms` — a mid-sentence pause no longer ends the
  utterance.
- **Partial transcripts** stream into `PerceptionState.lastPartialTranscript`
  (LOG liveliness at most once per second); the journal still records only
  FINAL transcripts.
- **Honest idle cycling**: the recognizer's no-speech timeout (NO_MATCH /
  SPEECH_TIMEOUT) is normal empty-room behavior — logged at info level
  ("listening again"), restarted with a fixed 750 ms gap instead of an
  ever-growing error backoff (which would leave the listener asleep most of
  the day). Genuine faults keep the doubled backoff; missing permission
  degrades honestly to VAD-only.

### Speech — the agent never interrupts itself, never speaks a fragment

- **Half-duplex echo gate** (`ShugoCoreService`): the speaker sits
  centimetres from the mic, so the v1.15 barge-in hook (any onset →
  `stopAll()`) made Shugo cut its own sentences short. Onsets are now
  suppressed while TTS is audibly speaking and for a 400 ms dead time after
  (`PerceptionState.ttsSpeaking` / `ttsLastEndMs`); the human still wins the
  channel after an utterance completes. Barge-in re-enablement returns when
  acoustic echo cancellation proves strong enough on-device.
- **Sentence-aware output bound** (`human_interaction.truncate_sentence`):
  speak/ask_user texts bound at 400 chars (was 200) cut at a sentence end,
  else a word boundary, else the hard limit — never a fragment Shugo did
  not choose.
- **Voice persona in the decision prompt** (`subconscious`): when the
  executor set carries `speak`/`ask_user`, the prompt states Shugo's persona
  and the one-warm-complete-sentence rule (the small model needs it said to
  finish its sentences). Generated from the real action schema as before —
  the prompt↔executor alignment test still enforces it.
- **`task_in_flight`** surfaced in `get_status()` (drives the THINKING face).

### Presence — the Shugo face

- **`ShugoFaceView`**: an abstract Canvas face (two eyes + mouth, no assets)
  beside the SHUGOCORE title, visible from every tab. Honest states only,
  driven by real runtime signals: OFFLINE (agent stopped), IDLE (periodic
  blink), LISTENING (mic activity fresh), THINKING (decision cycle in
  flight), SPEAKING (TTS audibly on the speaker, mouth animating). Never
  decorative: the face only shows what is actually happening.
- **Boot greeting**: one warm self-introduction per service run
  ("Hi, I'm Shugo. I'm online and listening."), spoken only once the TTS
  engine is genuinely ready — personality, not noise.

### Interface

- **Tab shade follows clicks** (`TabBar`): the tap listener updated
  `onSelect` but never `setSelected`, so the highlight froze on SERVER
  forever. Fixed.

### Verified on device (Galaxy Tab S9 FE, Qwen2.5-1.5B Q4_K_M)

- Agent self-names and speaks complete warm sentences through the real
  gate: `{"spoken": "I'm Shugo, your friendly local assistant. Ready to
  help with your tasks!", "delivered": true}` — steady tick-spaced cadence
  (no self-cutoff signature).
- Recognizer holds and cycles the mic continuously; AGENT-tab validation
  shows HEARING OK / SPEECH OK.
- Tab shade tracks taps (AGENT, LOG verified); face renders on every tab.

## [1.17.0] - 2026-09-06 — closed-loop validation: the loop is proven, not assumed

### Conversation events — the Record stage of the conversational loop

- **Round-trip records** — when a spoken answer pairs with a pending
  question (1.16 pairing), the bus now records
  `{question, answer, round_trip_s, ts}` in a bounded deque (8); the agent
  shell drains completed exchanges each tick via
  `drain_conversation_events()`.
- **Journal metadata only** — the shell journals `conversation_event` to
  Tier 1 as latency + word counts (`round_trip_s`, `question_chars`,
  `answer_chars`, `privacy_scope: local`). Raw words never enter memory —
  the 1.12 transcripts-never-enter-memory rule preserved and now enforced
  by an asserted word-absence test.
- **`stats()`** gained `conversation_events` and `last_conversation_event`.

### `pipeline_health()` — the SHUGOCORE LIVE monitor

- **One honest liveness view over the closed loop**: `sensors` / `vision` /
  `hearing` judged from the bus's own buffer freshness (60 s / 60 s / 120 s),
  `speech` / `model` / `memory` from agent-side truths passed in by the
  shell. States: `ok` (fresh evidence), `stale` (evidence expired), `down`
  (provider absent), `unknown` (no evidence yet — never fabricated).
- **Honest overall**: agent-truth-ok alone is not loop evidence (a healthy
  model with no human sensor data reads `unknown`, not `ok`); any `down`
  stage makes the whole pipeline `down`.
- **`get_status()`** exposes it as `pipeline` (JSON-boundary safe); the
  AGENT tab gained the **Validation** section rendering all six stages with
  evidence-based colors.

### Tests — 15 new (`tests/test_closed_loop.py`), 579 total

- Round-trip record/drain semantics (drain consumes — journal, not
  archive), TTL expiry never pairs, bounded deque, ask-response arming.
- Pipeline-health state matrix: unknown → ok → stale over time; down on
  absent provider; model-truth-alone is not loop evidence.
- Agent integration: ask → answer → journal metadata-only (privacy
  invariant), quiet ticks journal nothing, status pipeline truthful,
  `get_status_json` boundary.

## [1.16.0] - 2026-09-06 — multimodal: the agent asks, listens, and remembers the conversation

### User-state fusion (`human_interaction.py`)

- **Fused `user_context`** — one view the agent reasons over: `person_present`,
  `speech_recent`, `last_transcript`, plus **reserved `gaze` /
  `attention_target` / `environment` fields (None until the XR providers
  arrive — the schema is stable before the sources)**, and a mean confidence
  over fresh observations.
- **Bounded conversation memory** — the bus keeps the last 12 turns
  (human speech observations + agent speech responses, chronological),
  exposing the last 6 in `human_context()` so every tick's task context
  carries the recent dialogue.
- **Question/answer pairing** — a question (`ask_user`, or any speech
  response marked `expects_answer`) arms a pending-question slot (TTL
  120 s, debounced); the next speech observation carrying words is paired
  with it (`answer_to` on the observation payload, `last_answer` in stats)
  and disarms the slot. Expired questions never pair.

### The `ask_user` action — uncertainty becomes a question

- **New internal action class** `ASK_USER_ACTION_TYPES`: the agent that is
  uncertain ASKS the operator through the same TTS edge instead of guessing
  (OBSERVE → GATE → ASK USER → LISTEN → DECIDE). No consent, no approval,
  always journaled (`agent_question` event).
- **The safety line, explicit**: a spoken "yes" is DATA the agent may reason
  over — it is never a consent record. Consent stays with the operator's
  explicit registry entry; asking never unlocks a gated action.
- **Registered like speak**: honest `not_implemented` on nodes without a
  speech provider; `register_handler` allowlist extended; the model's
  action schema includes `ask_user` exactly where a real executor exists.

### Dialect fix (latent bug found while wiring)

- `_execute_speak` read only `params.text`, but the 1.15 parser
  normalization routes top-level `text` into `params.utterance` — the
  top-level-text-only dialect would have been refused. The executor now
  accepts `text` **or** `utterance`; `ask_user` accepts `question`/`text`/
  `utterance` the same way.

### Truth rows

- **AGENT tab `Last exchange`** — the last question the agent asked and the
  last answer it heard (`Q: … → A: …`), `—` until a real exchange exists.
- 14 new tests (conversation bounds, pairing + TTL expiry, fusion fields,
  ask_user gate/dispatch/journal honesty, utterance fallback) — 564 total.

## [1.15.0] - 2026-09-06 — speech: the agent talks back (local TTS)

### The first SpeechOutput provider

- **New `runtime/TtsProvider.kt`** — the roadmap's closing side:
  agent response → TTS → speaker → human. Honest liveness (`isSpeaking`
  only while an utterance is truly on the speaker), a bounded queue
  (max 3 pending — no runaway monologue), and **barge-in**: the audio
  provider's VAD onset hook stops playback instantly, so the human always
  wins the audio channel.
- **The `speak` internal action** — new `SPEECH_OUTPUT_ACTION_TYPES` policy
  class: addressing the local operator through the device speaker is not
  egress, so it needs no consent or approval — but the text is sanitized
  and bounded, the execution is journaled, and without an attached
  provider the dispatcher answers `not_implemented` (actions are never
  simulated).
- **Chaquopy reverse callback** — the service registers a `SpeakBridge`
  listener with the Python agent; `_execute_speak` sanitizes, calls the
  bridge, and records the `AgentResponse` on the interaction bus. The
  decision core stays provider-agnostic: it only proposes `speak`; the
  Kotlin edge performs the output.

### Truth rows and model schema

- **AGENT tab `Speech` row** — what the agent last said (`last_spoken`
  from the interaction bus), `—` until it has actually spoken.
- **`Test speech` control** — drives one `speak` action through the REAL
  policy gate + execution path into TTS: the full chain is verifiable
  without waiting for the model to choose speech.
- **Model schema grows automatically** — `available_action_types()` now
  includes `speak` on speech-capable nodes, so the prompt offers it only
  where a real executor exists (desktop builds without TTS never see it).

### Proposal parser: scan past the prose echo (found live, on-device)

- During verification the 1B model **spontaneously proposed `speak`** —
  but the output led with a prose-echo object (`{"text": …}`) and the
  parser returned that actionless stub immediately, never reaching the
  real decision behind it. `_parse_proposal` now **remembers the first
  actionless stub and keeps scanning**: a real action later in the text
  wins; a stub-only output still yields a well-formed null proposal
  (failure accounting unchanged); the unknown-action hard reject is
  preserved so degenerate candidates can never mask an invalid proposal.
- `"speak"` added to the engine's known action types via
  `SPEECH_OUTPUT_ACTION_TYPES` — before this, the model's genuine speech
  proposals would have been hard-rejected as unknown even if scanned.

## [1.14.0] - 2026-09-06 — hearing: VAD-gated on-device speech

### The first AudioInput provider

- **New `runtime/AudioProvider.kt`** — the roadmap's flow, verbatim:
  microphone → VAD → STT → transcript → `HumanObservation(type=speech)`.
- **VAD before STT (non-negotiable)**: an energy RMS gate over 20 ms frames
  (16 kHz mono) with an adaptive noise floor holds the mic; only speech
  onset (3 consecutive loud frames) wakes the recognizer — ambient noise
  never invokes the STT engine. A barge-in hook (`onSpeechOnset`) fires at
  onset, ready for the 1.15 TTS provider to cancel playback.
- **On-device STT only**: `SpeechRecognizer.createOnDeviceSpeechRecognizer()`
  — transcripts never leave the device. If the platform cannot supply an
  on-device recognizer, hearing degrades HONESTLY to VAD-only
  `speech_detected` observations and logs why.
- **Utterance-triggered**: one observation per recognized utterance
  (`payload={transcript, stt:"on_device"}`) or per detected-but-untranscribed
  speech (`payload={speech_detected:true}`); errors back off exponentially
  (1 s → 30 s) so a failing recognizer can't spin.
- **Honest liveness**: every mic read/recognizer audio event stamps
  `PerceptionState.lastMicActivityMs` — the SENSORS tab MICROPHONE row reads
  ACTIVE only while audio truly flows.
- **`stats()` gained `last_transcript`** (the most recent recognized words;
  None until STT produces any) — the AGENT tab's Interaction section gains a
  **Hearing** row. Mic lifecycle follows RECORD_AUDIO permission reality,
  synced every housekeeping tick like vision.

### Tests

- Transcript-stats test (VAD-only events don't set it; STT does) — suite:
  **533 passing**.

## [1.13.0] - 2026-09-06 — vision: person presence

### Camera perception (the first Vision provider)

- **New `runtime/VisionProvider.kt`** — person presence from the FRONT camera
  at ~1 fps, posted as `HumanObservation(type=visual,
  payload={person_present, face_count})` through the same interaction bus any
  future camera (robot, Quest, glasses) will use.
- **Zero ML dependency**: person detection uses the platform
  `android.media.FaceDetector` over an upright, 320px, RGB_565 frame
  (CameraX `ImageAnalysis` + `ImageProxy.toBitmap()`); swappable behind the
  provider for richer vision models later. CameraX (core/camera2/lifecycle
  1.3.4) is the app's first new dependency since v1.9 — deliberately.
- **Edge-triggered + heartbeat**: observations post on state CHANGE and every
  45 s while present; skipped frames never flood the bus.
- **Hysteresis**: `person_present=true` on the first confident face;
  `false` only after 20 s of continuous absence — honest presence, no
  flicker.
- **Contract semantics extended**: a visual observation carrying
  `person_present: false` now implies absence (drives USER_LEFT through the
  bus's debounced state machine), so vision alone — without a separate
  presence event — moves the presence lifecycle.
- **`human_context()` / `stats()`** gained `person_present` (from the freshest
  visual observation, None when vision went stale >60 s) and
  `vision_recent` — DECIDE now knows whether a person is in front of the
  device as ordinary task context.
- **Honest liveness**: every analyzed frame stamps `PerceptionState`, so the
  SENSORS tab CAMERA row reads ACTIVE only while frames truly arrive
  (previously hardwired IDLE).
- **Lifecycle**: the provider follows camera-permission reality — synced
  every housekeeping tick by `ShugoCoreService` (grant → start, revoke →
  stop), stopped in onDestroy. Tapping CAMERA in the SENSORS tab requests the
  runtime permission; granting starts perception.
- **UI**: the AGENT tab's Interaction section gains a **Vision** row
  (PERSON / NO PERSON / — when vision has not reported).

### Tests

- 3 new bus tests (visual presence/absence semantics, vision freshness
  fields) — suite: **532 passing**.

## [1.12.0] - 2026-09-06 — human interaction foundation

### The Human Interaction contract (provider-side)

- **New `human_interaction.py`**: `HumanObservation` (type ∈ visual / speech /
  presence / gesture / interaction, timestamp, source, confidence, sanitized
  payload, `privacy_scope="local"` by construction) and `AgentResponse`
  (speech / visual / action / acknowledgement — schema reserved for the
  v1.15/1.16 speech and action capabilities). Camera, microphone, gaze or UI —
  the agent receives *observations*, not devices.
- **`InteractionBus`**: bounded (256-entry) thread-safe ring mirroring the
  LogBus pattern, plus a debounced presence state machine emitting
  USER_PRESENT / USER_LEFT / USER_RETURNED (a flapping provider cannot flood
  the journal).
- **The provider rule, enforced**: the decision core (`decision_engine`,
  `subconscious`, `execution_layer`, `policy`) must never import the
  interaction module — the engine receives human context only as task
  `context` data. `tests/test_human_interaction.py` AST-parses the core
  modules and fails the suite if the rule is ever violated.

### Agent + Android integration

- **`AndroidAgent.publish_human_observation[_json]`** — the Chaquopy ingestion
  twin of `update_telemetry_json`: sanitize → validate → bus → Tier 1
  `human_observation` event (memory-internal by construction, like
  `record_observation`: no consent, no approval, no egress; only
  type/source/confidence reach the journal).
- **`_get_observation()`** now enriches every tick with `human_context`
  (presence, recency, speech-recent), so DECIDE sees the human as data.
- **Android `runtime/HumanInteractionBus.kt`** — the device-side edge: posts
  observations to the agent, keeps a bounded local record, parses presence
  events back. Wired in `ShugoCoreService` (publisher set on agent init,
  cleared in onDestroy).
- **First honest human source**: `MainActivity.onUserInteraction()` posts
  `interaction` observations (source `ui_touch`, rate-limited to 1 per 5s) —
  pre-camera, the only non-decorative signal available.
- **UI truth rows**: the NODE STATUS header gains **HUMAN** ("seen Ns ago" /
  "idle Ns"), fed only by real accepted events; the AGENT tab gains an
  **Interaction** section (presence state machine + recorded-observation
  count from the Python bus).

### Tests

- `tests/test_human_interaction.py` — 25 tests: schema validation, payload
  sanitization (control-char injection, length caps, key caps, junk
  flattening), bus boundedness + 8-thread safety, presence state machine with
  a deterministic clock, agent ingestion (the JSON boundary never raises),
  status/observation surfaces, and the provider-rule import guard.
- `human_interaction.py` + `security.py` added to the tree-sync canonical
  module list; suite: **529 passing**.

## [1.11.0] - 2026-09-05 — model protocol unification + structural fixes

### Model protocol (decision prompt ↔ real executor set)

- **The decision prompt is now GENERATED from the engine's real action
  schema** (`DecisionEngine.available_action_types()`), ending the three-list
  drift the model-facing protocol had suffered: the prompt can no longer
  advertise actions the policy/execution layers cannot honor.
- **New internal action `record_observation`** (non-side-effecting: writes to
  the agent's own Tier 1 only — no consent, no approval, no egress). It gives
  the model an always-executable action and demonstrates the full
  OBSERVE→GATE→DECIDE→EXECUTE→EVALUATE→RECORD cycle on-device.
- **Null-proposal accounting fixed**: a well-formed `{"action_type": null}`
  no longer resets the consecutive-failure counter (previously a model that
  only ever said null could loop NO_ACTION forever, never reaching the
  fallback). Only *executable* proposals reset it; null proposals count as
  failures and the journal detail says so.
- **Rule-based fallback now emits `record_observation`** (previously
  `multi_step_process` with no steps, which could only refuse): after 3
  consecutive failures the loop becomes productive again with a real
  execution, evaluation and record.

### Observability

- **MODEL TEST panel on the SERVER tab**: one controlled decision round-trip
  through the real backend with Parse VALID/INVALID, failure class
  (no_response / invalid_protocol / valid_protocol / valid_protocol_null),
  latency, model, and the truncated raw response — the three failure classes
  can no longer collapse into one visible outcome.
- **Detail row on the AGENT tab** fed by `last_cycle_result.detail`.

### Structural

- **One shared MemoryManager**: the Android agent shell now passes its
  MemoryManager into the DecisionEngine instead of letting the engine build
  a second one. Agent observations and engine decisions/executions land in
  ONE Tier 1 (the control plane's cycle enrichment can finally see engine
  events), `android_observation` events reach the episodic journal, and the
  Tier 3 consent checker is rebound to the engine's consent registry.
- **`tests/test_android_tree_sync.py`**: byte-identity guard — every shared
  root↔bundled module must be identical, failing the suite on single-sided
  edits (the drift class that once shipped a stale DecisionEngine).
- CHANGELOG normalized: strictly descending releases, duplicate `[1.4.0]`
  header merged.

## [1.10.0] - 2026-09-05 — validation-ladder phases 0–1

### Phase 0 — Agent pane truth

- **Fixed the phantom engine error**: `Ui.str()`'s `"—"` fallback also applied
  to *empty* values, so an empty `engine_error` rendered as a permanent red
  `engine: —` line in the AGENT tab. Empty/absent errors now render nothing.
- **New truth rows** in the AGENT tab Status section: `Engine` (the real
  engine class name, e.g. `DecisionEngine`) and `Backend` (the serving
  endpoint, e.g. `127.0.0.1:11434`) — matching the designed
  Decision Engine / Engine / Backend presentation.
- Verified on S9 FE: `Decision Engine: READY`, `Engine: DecisionEngine`,
  `Backend: 127.0.0.1:11434`, no error line.

### Phase 1 — Cycle outcome contract

- **`CYCLE_OUTCOMES`** formalized in the agent shell: `SUCCESS`, `NO_ACTION`,
  `POLICY_BLOCK`, `GOVERNOR_BLOCK`, `TASK_FAILURE`, `IN_FLIGHT`,
  `BACKEND_FAILURE`, `ENGINE_FAILURE`. The critical invariant: **NO_ACTION is
  not an error** — a model that answers but proposes nothing executable is a
  healthy, recorded cycle.
- **`BACKEND_FAILURE` vs `NO_ACTION`** distinguished by transport-class call
  errors (`transport_error` / `model_unavailable` / `invalid_model`) recorded
  per-model by `SubconsciousModel.note_call_error()` and surfaced through
  `no_viable_action` results — the three failure classes (no response /
  invalid protocol / engine rejection) no longer collapse.
- **Engine results now carry `outcome`, `stages`, `executed`, and
  `result_status`** (governor-trail backed): the agent renders the stages
  that actually ran instead of inferring them, and `TASK_FAILURE` trails can
  honestly include `EXECUTE`.
- `tick()` produces a **`last_cycle_result`** snapshot in `get_status()`;
  `last_evaluation` now carries the outcome name (`no_action`, `policy_block`,
  `backend_failure`, …) with honest coloring (blocks amber, failures red,
  `no_action` neutral). The network-policy block trail now includes its
  `RECORD` stage (it always journaled the block; the trail now says so).
- **9 new tests** — one per outcome class plus real-engine
  unreachable-backend classification and legacy-result mapping (498 total).

## [1.9.0] - 2026-09-04

### Added — Android node control plane (5 tabs + node status header)

The single-screen engineering-proof app is now the **management console for an
embodied AI node**: an always-visible NODE STATUS header plus SERVER / AGENT /
SENSORS / SECURITY / LOG tabs. The UI remains zero-dependency programmatic
Views, factored into `ui/` pane classes over a shared `runtime/` layer —
every row renders real runtime state, never a decorative green dot.

- **NODE STATUS header** — `● AGENT ONLINE` plus Model / Inference
  (LOCAL / BACKUP host / OFFLINE) / Memory / Sensors n-of-n / Policy /
  Network / Temperature / Battery, aggregated from a new
  `ShugoCoreService.getNodeSnapshot()` binder call.
- **SERVER tab** — inference section (engine, model, quantization parsed from
  the GGUF filename, endpoint), the four health indicators
  `MODEL LOADED / INFERENCE READY / API READY / AGENT READY`, live server
  stats (requests, tokens, last latency — new `LocalApiServer` counters), the
  v1.8.1 model-catalog download/select dialog, the backup-server URL field,
  and START/STOP SERVER.
- **AGENT tab** — subsystem statuses, current cycle (cycle #, last tick wall
  time, last decision / action / evaluation straight from the Tier 1 head),
  the OBSERVE→GATE→DECIDE→EXECUTE→EVALUATE→RECORD→CONSOLIDATE pipeline with
  the stages that actually ran highlighted, memory tiers 0–3 (Tier 3 always
  READ ONLY), and START/STOP AGENT (tick-loop pause; node stays up).
- **SENSORS tab** — the capability acknowledgement system:
  `Android hardware → permission → capability declaration → data stream →
  ShugoCore → agent ACK`. `runtime/SensorCapabilityManager` enumerates the
  eight capabilities with honest stream state (a stream is ACTIVE only when
  sensor data actually arrived); tapping a capability requests its runtime
  permission (new manifest permissions CAMERA / RECORD_AUDIO / location /
  Bluetooth, requested on-demand only); the agent ACK section renders what
  the Python side actually acknowledged.
- **SECURITY tab** — the capability governor: device permissions (Android) vs
  agent capabilities (authority toggles, fail-closed DISABLED by default —
  *Android permission ≠ agent authority*), network posture (localhost always
  on; LAN default on; Internet default DENIED and **actually enforced**),
  tools (fail-closed DENIED defaults) and policy flags (FAIL CLOSED / AUDIT /
  CONSENT).
- **LOG tab** — live feed from a central `runtime/LogBus` (500-entry ring
  buffer) fed by both Kotlin events and the Python agent (seq-monotonic
  `recent_logs(after_seq)` polling), with [ALL] [MODEL] [AGENT] [SENSOR]
  [POLICY] [MEMORY] [ERROR] filters.

### Changed — STOP SERVER semantics (server + agent coupling)

- **STOP SERVER** halts the on-device inference stack and — per the node
  design — **stops the agent too, unless a backup (desktop) server URL is
  configured**. With a backup URL the agent keeps running, re-pointed live to
  the backup via the new `AndroidAgent.set_backend_url()` (updates the cached
  backend adapters and the model config in one place). **START SERVER**
  restarts the inference stack and brings the agent back when both were
  stopped together. START AGENT guarantees a backend exists first.

### Added — Python agent control-plane surface (`shugocore_agent.py`)

- `update_capabilities(declarations)` — the explicit capability
  acknowledgement registry (receiving a declaration never grants authority).
- `update_policy(agent_caps, internet, lan)` — authority + network posture,
  **enforced in `tick()`**: a backend outside the loopback address is checked
  against the posture (LAN ranges / `.local` vs public Internet) and a
  blocked target stops the tick at GATE with a `policy_block` event — the
  agent never calls the engine.
- Enriched `get_status()`: tier 0/1/2 counts, Tier 3 state, engine_ready,
  last decision/action/evaluation (derived from the Tier 1 head — real
  events, not assumptions), pipeline stage list, backend URL, capability
  acks and the live policy map. All v1.8.1 keys preserved.
- Honest pipeline stage tracking: a stage is reported as run only when the
  code path for it actually executed (e.g. a refused cycle never claims
  EXECUTE ran).
- Bounded, seq-numbered log buffer + `recent_logs()` for the LOG tab.

### Tests

- `tests/test_android_control_plane.py` — 15 tests: capability
  acknowledgement contract, ack-is-not-authority, backend re-pointing,
  localhost/LAN/Internet policy gates, fail-closed refusal before the engine,
  status enrichment with legacy-key compatibility, honest stage tracking and
  the log buffer. Suite: 61 platform tests + 15 new, all green.

### Deferred (follow-ups)

- ApprovalBroker operator-approval UI flow.
- Camera/mic/GPS/Bluetooth stream state stays IDLE until the agent actually
  opens those devices (honest); per-sensor data-rate display for them comes
  with the first real consumers.

### Fixes

- **Crash on launch (`IllegalStateException: The specified child already has a
  parent`)** — the four pane constructors added the inner column returned by
  `Ui.pane()` directly to themselves, but that column is already parented inside
  the pane's `ScrollView`. This threw before the first frame in
  `MainActivity.onCreate`, so the app opened and instantly closed. Each pane now
  adds the `ScrollView` (the pair's first element) and fills the inner column.
  Verified on-device: clean crash buffer, activity resumed, service + Chaquopy
  Python runtime initializing, sensor listeners registered.
- **Decision Engine permanently ABSENT on-device** — `decision_engine.py`
  imported `open_semantic_memory` from `pg_memory` at module top level, and
  `pg_memory` imports `psycopg2`, which is not bundled in the Chaquopy Android
  build. Every `DecisionEngine` construction therefore died with `ImportError`
  before any engine existed, leaving the agent shell "running" with no decision
  core. The import is now a lazy `_get_pg_memory()` accessor (root and bundled
  copies in sync); Android persists events via the audit JSONL chain, not
  Postgres, so nothing else changes.
- **Python↔Kotlin boundary silently dropping data (empty LOG tab, dead
  telemetry/capability pushes)** — Chaquopy hands Python non-iterable Java
  proxies for Kotlin collections, so the log poll produced nothing, telemetry
  and capability pushes failed, and every failure was invisible in the UI.
  All boundary crossings are now JSON strings: `get_status_json`,
  `recent_logs_json`, `update_telemetry_json`, `update_capabilities_json`.
  The first log poll seeds `last_log_seq` from the status head so the LOG tab
  shows current activity instead of flooding with history.
- **Failures now honest and visible** — `_bootstrap()` never raises (a failed
  subsystem degrades to a reporting agent instead of a zombie "RUNNING"), any
  engine/init error is persisted to `engine_error.txt` / `agent_error.txt` in
  the app's files dir, surfaced in `get_status` as `engine_error` / `init_error`,
  and rendered as real error text in the AGENT pane instead of a bare ABSENT.
- Verified on S9 FE after a clean rebuild (stale incremental APK assets were
  also masking the Python fixes): Decision Engine READY, gated pipeline firing
  `decision_requested` every tick, `episodic_journal.jsonl` growing,
  `semantic_memory.db` at 1.1 MB, no error files, zero entries in the crash
  buffer.
- **telemetry.py OTel path was dead code on OTel-equipped hosts** — `start_span`
  constructed `_OtelSpan(span)` without its required `name`/`attributes`
  arguments, the resulting `TypeError` was swallowed by the `except Exception`
  fallback, so every span silently degraded to the no-op path, real OTel spans
  were started but never ended (leaked), and `recent_spans()` was populated only
  by accident. The OTel branch now mirrors into the diagnostic ring buffer and
  always ends the underlying span; `tests/test_v1.py` covers both tracer paths
  deterministically.
- **Stale pip wheel was shadowing the bundled Python on-device** — the Chaquopy
  block pip-installed `shugocore==1.8.1`, which resolved the prebuilt wheel in
  `dist/` (built from pre-1.9.0 source). On-device, that wheel shadowed the
  bundled `src/main/python` tree, so engine-code changes shipped in the APK but
  the runtime kept executing the old 1.8.1 copy (the DECIDE/RECORD fix was
  invisible until this was found). The self-install is removed: the bundled
  tree is the single source of truth for on-device Python (verified it covers
  the entire on-device import graph; robotics/mobile/shugonet handlers degrade
  cleanly via their optional try/except imports). A fresh 1.9.0 wheel was
  rebuilt into `dist/` (a git-ignored build-output dir) and the stale 1.8.x
  wheels were deleted, so the shadowing failure mode cannot recur.
- **DECIDE-cycle semantics aligned with the design** — the designed cycle is
  DECIDE → (action proposed) → policy gate → EXECUTE → EVALUATE → RECORD, with
  the no-viable-action branch going straight to RECORD → next cycle. Three
  branches violated "every cycle ends in a record": the no-viable branch
  returned an error with no journal entry at all (the most frequent terminal
  branch on-device, where stub outputs rarely propose), the multi-step path
  journaled no overall outcome, and governor pre-pipeline refusals
  (paused/halted/re-entrant) returned without recording. All three now record
  (`no_viable_action`, `tool_execution` with `multi_step_process` +
  per-step statuses, `governor_block` kind `begin_task`), and the AGENT pane's
  stage trail honestly shows `RECORD` on refused/error engine cycles (still
  never EXECUTE/EVALUATE without a real execution). Tests cover all paths.

## [1.8.1] - 2026-09-04

### Added — On-device model download & selection UI

- **`inference/ModelDownloader.kt`** — curated catalog of 5 ungated Hugging
  Face GGUF quantizations (Qwen2.5-0.5B/1.5B/3B-Instruct, Llama-3.2-1B-Instruct,
  SmolLM2-1.7B-Instruct, all Q4_K_M) with exact LFS byte sizes used for
  progress and integrity checks; every `resolve/main` URL verified reachable
  without auth (HTTP 200).
- **Resumable downloads** — `HttpURLConnection` with HTTP-Range resume,
  `.part` → atomic rename, byte-size verification, cancellation, and up to 5
  automatic retries on dropped streams; files land in `filesDir/models`, the
  directory `findModelFile()` already scans.
- **`MainActivity` "Models…" dialog** — per-model `recommended` (chosen by RAM
  tier via `CapabilityDetector`) / `downloaded` / `ACTIVE` tags, listing of
  sideloaded `.gguf` files, select & load / delete flows, a live %/MB progress
  bar, and download cancellation.
- **`ShugoCoreService.loadOnDeviceModel()`** binder API hot-loads the selected
  model and starts `LocalApiServer` on `127.0.0.1:11434` **without restarting
  the agent** — the agent's default `OllamaBackend` URL is the same loopback
  endpoint, so on-device inference activates on the next generate call.
  `findModelFile()` now prefers the persisted `selected_model`.

### Added — Sensor engagement test cycle

- `AndroidAgent.update_telemetry()` / `sensor_test_cycle(steps)` / enriched
  `get_status()` in `shugocore_agent.py`; Kotlin pushes
  `ThermalMonitor.getTelemetryMap()` (battery, charging, CPU temp, RAM,
  accelerometer XYZ, thermal state) every tick before `tick()`.
- Binder APIs `getAgentStatus()` / `runSensorTestCycle()` /
  `getDeviceRecommendation()`; `MainActivity` polls agent status at 1 Hz
  (fixes the frozen memory display) and surfaces the device recommendation.
- `tests/test_sensor_engagement.py` — 6 tests covering backend registration,
  agent factory, telemetry→observation mapping, stub fallback, the sensor
  cycle, and determinism; all passing.

### Fixed — "Start Agent does nothing" on Android

- Root cause: `android_inference` (and the engine closure) was never bundled
  into the Chaquopy source set, so `create_backend({"type": "android"})`
  raised `ValueError`, was silently caught, and left `pyAgent = null`.
  `py-modules` now includes `android_inference` + `shugocore_agent`, and all
  21 engine modules are bundled into `app/src/main/python/`.
- `DecisionEngine` is imported lazily inside a guarded `_initialize_engine` —
  missing engine dependencies now degrade to the stub observation loop instead
  of crashing agent construction.
- Kotlin compile fixes: `Service.START_STICKY` (previous constant is
  API-34-only), `PyObject.toJava(Map::class.java)` (replaced the non-existent
  `toJavaMap()`), vararg spread for `create_agent(soc, api_url)` (the array
  was being passed as a single argument, silently dropping `api_url`),
  null-safe model-dir scan, and inference init moved off the main thread.

### Fixed — Backend resilience

- `OllamaBackend` default timeout raised 30 → 120 s: cold model loads on a
  desktop host exceeded 30 s and made every decision fall back.

### Added — Desktop server mode (macOS / Linux / Windows)

Users without a high-end 2020+ Android phone can now run the full agent on a
desktop and pair the phone as a lightweight node:

- **`shugocore_server.py`** — new stdlib-only HTTP server (`shugocore-server`
  console script). Speaks the Ollama wire contract (`/api/generate`,
  `/api/chat`, `/api/tags`, `/health`) on one port, so the phone's existing
  `AndroidBackend` connects with zero client changes, plus the engine API
  (`/api/v1/status`, `POST /api/v1/task`) that routes through the same
  policy-gated `execute_task` pipeline as a local call.
- **Backends** — `--backend ollama|llamacpp|openai|stub` with `--backend-url`;
  the same backend config is shared between the HTTP server and the engine's
  model registry so `/api/v1/task` decisions use the identical backend.
- **Android desktop mode** — `create_agent(soc, api_url)` accepts a desktop
  server URL; `MainActivity` gains a "Desktop server URL" field persisted to
  `SharedPreferences` and `ShugoCoreService` passes it to the agent. On-device
  llama.cpp remains the default when the field is blank.
- **Fixes** — the Android agent's engine model config now includes the
  required `id`/`type` keys (previously a silent `KeyError` on device); agent
  tick cadence reduced from 100 ms to 1 s (avoid hammering the LLM + battery).
- **Tests** — `tests/test_shugocore_server.py` exercises every endpoint with
  real HTTP (health, tags, generate, streaming, chat, task success/policy
  block, status, 404, CORS preflight).
- **Docs** — `docs/desktop_server.md` with per-OS setup (macOS brew, Linux
  systemd + ufw, Windows firewall rule) and README section.

### Fixed — Android build toolchain

- **Gradle wrapper** pinned from 9.3.0 back to **8.11.1**. Gradle 9 removed the
  `Project.exec(Action)` overload that AGP 8.7.3's `NativeModelBuilder` / CMake
  file-API invocation depends on, which crashed every Studio sync with
  `java.lang.NoSuchMethodError: Project.exec(Action)`. 8.11.1 is the Gradle
  version AGP 8.7.3 is tested against (and KGP 2.0.21's matched max, so no
  version warnings).
- **Daemon JDK** pinned to **JDK 21** via `~/.gradle/gradle.properties`
  (`org.gradle.java.home`). The only JDK on the machine was Temurin 25, on which
  Gradle 8.x cannot run; JDK 21 comes from `brew install openjdk@21`.
- **app module** now applies `org.jetbrains.kotlin.android`. The `.kt` sources
  (`MainActivity.kt`, `LlamaCppBridge.kt`, `LocalApiServer.kt`,
  `ThermalMonitor.kt`, `CapabilityDetector.kt`) were never compiled because the
  Kotlin Gradle plugin was missing from `plugins {}`.
- Pinned `ndkVersion "27.0.12077973"` (matches the installed NDK r27) and added
  `buildPython "python3"` for deterministic, macOS-friendly builds.

## [1.8.0] - 2026-09-03

### Fixed — Android native inference is now real end-to-end

The Android stack previously stopped at placeholder JNI calls and stubbed
HTTP responses. The full chain now runs real tokens:

`HTTP → LocalApiServer → LlamaCppBridge → JNI → llama.cpp → GGUF`.

- **`platforms/android/app/src/main/cpp/llama_jni.cpp`** — rewritten against
  the pinned llama.cpp (b10795) C API: `llama_model_load_from_file` /
  `llama_init_from_model` session creation, vocab-based
  `llama_tokenize` / `llama_token_to_piece`, `llama_decode` batching via
  `llama_batch_get_one`, and a proper sampler chain
  (`penalties → top_k → top_p → temp → dist`) sampled with
  `llama_sampler_sample`. Per-session state (`ShugoSession`: model, context,
  vocab, KV position, pending-UTF-8 buffer) replaces the previous mutable
  globals; `nativeDrain` flushes partial multi-byte characters at
  end-of-generation so streamed text is never corrupted mid-codepoint.
- **`CMakeLists.txt`** — rewritten: valid CMake syntax, static llama/ggml
  linked into `libllama_jni.so`, optional Vulkan GPU offload
  (`-DSHUGOCORE_VULKAN=ON`), no `-march=native` (broke NDK + emulator
  builds), and dual-target support — Android NDK builds and host CI
  validation builds (`-DSHUGOCORE_JNI_INCLUDE=...`).
- **`LocalApiServer.kt`** — replaced JDK-internal `com.sun.net.httpserver`
  (absent on Android) with a minimal HTTP/1.1 implementation on
  `ServerSocket`, loopback-only. `/api/generate` and `/api/chat` now parse
  JSON bodies, apply sampling options (`temperature`, `top_k`, `top_p`,
  `repeat_penalty`, `seed`, `num_predict`), stream NDJSON chunks when
  `stream: true`, and return Ollama-shaped responses (`response` /
  `message.content`, `done`, `eval_count`, `total_duration`) so ShugoCore's
  `OllamaBackend`/`AndroidBackend` work unmodified.

### Verified

- Host build of `libllama_jni.dylib` (full llama.cpp + JNI bridge) compiles
  and links with zero warnings against the pinned headers; all 8 JNI symbols
  (`nativeInit`, `nativeFree`, `nativeTokenize`, `nativeDetokenize`,
  `nativeDrain`, `nativeEvalPrompt`, `nativeGenerateToken`, `nativeReset`)
  exported with names exactly matching the Kotlin `external fun`
  declarations.
- Version metadata aligned at 1.8.0 across `version.py`, `pyproject.toml`,
  and `build.gradle`.

## [1.7.0] - 2026-09-03

### Added — Android native layer

- **`platforms/android/`** — Complete Android application shell with native
  llama.cpp inference via JNI bindings.
- **`LlamaCppBridge.kt`** — Kotlin JNI wrapper for llama.cpp with token
  streaming, batching, and resource management.
- **`LocalApiServer.kt`** — OpenAI-compatible HTTP API server running on
  127.0.0.1:11434, enabling ShugoCore backends to work unmodified on Android.
- **`CapabilityDetector.kt`** — Hardware capability detection (SoC, NPU, GPU,
  RAM) for automatic model/quantization selection.
- **`ThermalMonitor.kt`** — Battery and thermal state monitoring with
  inference throttling and emergency shutdown.
- **`ShugoCoreService.kt`** — Foreground service managing inference backend
  lifecycle, thermal throttling, and periodic agent execution.
- **`MainActivity.kt`** — Minimal UI for starting/stopping the agent service.
- **`llama_jni.cpp`** — Native JNI bindings for llama.cpp with Vulkan GPU
  offload support.
- **`shugocore_agent.py`** — Python agent entrypoint for Chaquopy runtime.
- **`android_inference.py`** — Android backend compatible with
  `OllamaBackend` interface.

## [1.6.0] - 2026-09-03

### Added — dream consolidation and memory write gates

- **`DreamConsolidation`** — Periodic reflective pass that compresses episodic
  experiences into durable identity insights. Inspired by GrowBot's "dream" phase.
  Runs at a slower cadence than regular consolidation (every N ticks).
- **Dream write-permission discipline** — The dream is the SOLE writer of
  Tier 3 (CoreIdentity) mutations during normal operation (code-enforced).
  Commits are clamped: max 1 sentence added per dream, identity never falls
  below minimum length.
- **Insight extraction** — Automatically extracts actionable insights from
  episodic events: recurring failure patterns (≥2 occurrences) and consistent
  success strategies (≥3 occurrences).
- **Memory write-gate enforcement** — Code-enforced write permissions per memory
  tier:
  - Tier 0 (Scratchpad): Only scratchpad writes
  - Tier 1 (EpisodicMemory): Only episodic record (append-only)
  - Tier 2 (SemanticMemory): Only consolidation/maintenance worker
  - Tier 3 (CoreIdentity): Only dream consolidation or explicit promotion
- **`check_write_permission()` / `enforce_write()`** — Runtime write-gate
  enforcement with `PermissionError` on violation.
- **Continuous agent integration** — Dream consolidation automatically runs
  during the continuous loop. Dream stats exposed via `status()`.
- **Tests** — 16 new tests covering dream consolidation, insight extraction,
  identity mutation clamping, and write-gate enforcement.

## [1.5.0] - 2026-09-03

### Added — robot simulation framework for public test data

- **`simulation/` module** — Physics-based robot simulation framework with
  pluggable backends (MuJoCo, stub fallback) for generating public benchmark data.
- **`MuJoCoSimulation`** — Full MuJoCo physics backend (`pip install 'shugocore[simulation]'`).
  Loads MJCF/URDF models, provides joint control, IMU sensing, and deterministic
  seeded simulation. Falls back to `StubSimulation` when MuJoCo is not installed.
- **`StubSimulation`** — Deterministic in-memory simulation for testing without
  physics dependencies. Integrates joint commands into positions with simple
  kinematics.
- **Robot model loaders** — Support for multiple open-source robot platforms:
  - `BerkeleyHumanoidLite` — 24-DOF humanoid from UC Berkeley (MIT license)
  - `Reachy2` — Humanoid by Pollen Robotics (Apache-2.0 license)
  - `UnitreeG1` — Compact humanoid by Unitree Robotics (BSD-3-Clause license)
- **Standardized test scenarios** — Reproducible benchmark scenarios producing
  public JSONL test data:
  - `WalkToTarget` — Navigation efficiency and path planning
  - `BalanceTest` — Stability under perturbations
  - `EmergencyStop` — Safety response measurement
- **`run_benchmark()`** — Execute full benchmark suite on any robot, output
  results to JSONL for public dataset publication.
- **`SimulationResult`** — Standardized result format with serialization for
  public test data distribution.
- **`simulation` optional dependency** — `pip install 'shugocore[simulation]'`
  installs `mujoco>=3.0` and `numpy>=1.24`; no new required dependencies.
  installs `mujoco>=3.0` and `numpy>=1.24`; no new required dependencies.

## [1.4.0] - 2026-09-03

### Added — fleet-shared Tier 2 memory (PostgreSQL + pgvector)

- **`pg_memory.py`** — `PgSemanticMemory`, a drop-in PostgreSQL + pgvector
  backend for Tier 2 semantic memory, enabling the persistence half of the
  Shogunet memory mesh: several agents (or planning nodes) pointing at the
  same DSN see one consistent knowledge base. API parity with the SQLite
  `SemanticMemory` (store_fact / search / reinforce / decay / prune /
  get_fact / extract_entities / facts_about / related_entities /
  entity_names), so it plugs directly into
  `DecisionEngine(semantic_memory=...)` and `MemoryManager(semantic=...)`.
- **`open_semantic_memory()` factory** — single storage knob for operators:
  a `postgres://` or `postgresql://` DSN selects `PgSemanticMemory`; any
  other value preserves the historical local SQLite behavior.
  `DecisionEngine` routes `memory_db_path` through it, so switching a fleet
  to shared memory is a one-line config change.
- **Embedding parity** — the pg backend embeds with the same deterministic
  hashing embedding as the SQLite backend, so facts written by one agent on
  one backend are retrievable with identical similarity scores by another
  agent on the other backend.
- **Search pushed down to pgvector** — cosine distance (`<=>`) is computed
  server-side (`similarity = 1 - distance`), with an optional HNSW index
  recipe for fleet scale documented in the module docstring.
- **Fail-closed, no silent stub** — construction raises with actionable
  instructions if psycopg2 is missing (`pip install 'shugocore[postgres]'`)
  or the pgvector extension is unavailable (`CREATE EXTENSION vector;`).
  A fleet-shared memory that quietly failed to persist would violate the
  Tier 2 invariants, so none exists.
- **`postgres` optional dependency** — `pip install 'shugocore[postgres]'`
  installs psycopg2-binary; no new required dependencies for existing users.

### Hardened — fleet memory boundary

- Table identifiers (`table_prefix`) are strictly validated
  (`^[a-z][a-z0-9_]{0,40}$`) before interpolation into DDL/DML.
- Tier 2 only: the pg store never touches Tier 0/1 (per-agent) or Tier 3
  (read-only identity), preserving the memory invariants (N0-N1) across the
  fleet.

## [1.3.0] - 2026-09-03

### Added — hardening for Continuous Synthetic Functional Agency

- **Continuous agent daemon** (`continuous_agent.py`): a top-level orchestrator
  that embodies the OBSERVE → GATE → DECIDE → EXECUTE → EVALUATE → RECORD →
  CONSOLIDATE loop in a single entry point. Bounded iteration counts, bounded
  interval pacing, and graceful shutdown. CLI:
  `python3 continuous_agent.py --interval 2.0 --max-iterations 1000`.
- **HMAC-signed audit chains** (`audit.py`): `AuditChain` now accepts an
  optional `hmac_key` (operator-held, e.g. via `SecretResolver`). Entries carry
  an HMAC-SHA256 tag over the chain hash + payload, making history
  tamper-evident *and* authenticated when the audit file lives on shared
  storage. Verification is backward-compatible with unsigned (1.2.x) chains.
  Includes a `verify_audit_file()` helper and `python3 audit.py <file>` CLI.
- **Real embeddings for Tier 2** (`vector_db.py`): environment observations
  were previously stored with all-zero placeholder vectors; they now use
  deterministic n-gram hashed embeddings (`hashed_embedding()`), making
  similarity search meaningful without any third-party dependency.
- **Pluggable embedding backends** (`vector_db.py`): `VectorDB` accepts an
  injectable embedding function, so operators can swap in a learned encoder
  (sentence-transformers, OpenAI, etc.) without touching storage logic.
- **Shogunet optional dependency** (`pyproject.toml`): the networking runtime
  is now installable via `pip install shugocore[shogunet]`.

### Hardened — ethics surface

- `EthicalGovernor` placeholder predicates (`can_explain`, `detect_bias`,
  `is_privacy_compliant`, `can_audit`) no longer return hardcoded values.
  They now evaluate real signals: decision provenance/audit-trail presence,
  input attribute screening for protected-category bias, and data-subject
  consent coverage for privacy compliance.

### Fixed

- `pyproject.toml` version was left at 1.2.0 after the 1.2.1 bump; both now
  share the single source of truth in `version.py` values.

## [1.2.1] - 2026-09-02

### Added — Shogunet multi-agent networking integration

- `shugonet_bridge.py` — ShugoCore-side adapter for the Shogunet networking
  layer, following the same pattern as `robotics_handler.py` and
  `mobile_nodes.py`. Enables multi-agent collaboration over 5G, 4G, WiFi,
  LoRa, and Bluetooth with a codependent memory mesh.
  - `ShugonetExecutionHandler` dispatches network actions to the Shogunet
    `ShugonetAgentRuntime`.
  - `register_network_handlers()` registers network action types with the
    `ExecutionLayer` and `policy.KNOWN_ACTION_TYPES`.
  - `attach_network_fallbacks()` merges network trigger severities into the
    deterministic `FallbackController`.
  - Network action types: `network_send`, `network_query`, `network_sync`
    (side-effecting) and `network_list_agents`, `network_status`
    (read-only).
  - Network fallback triggers: `network_transport_exhausted` (pause),
    `network_peer_lost` (pause), `memory_sync_conflict_storm` (safe_state),
    `audit_chain_broken` (halt).
  - `network_topic()` helper for canonical `/shugunet/{agent_id}/{tail}`
    topic construction.
- `tests/test_shugonet.py` — 22 integration tests covering action type
  registration, handler dispatch, fallback severity integration, and
  execution-layer compatibility.
- `DecisionEngine` now accepts an optional `shogonet_handler` parameter
  for automatic handler registration at engine construction time.

## [1.2.0]

### Added — Multiphase stress-test suite (97 tests) and robustness fixes

- `tests/test_android_lifecycle_stress.py` (30 tests): lifecycle churn
  (200 full create/pause/resume/destroy cycles), concurrent pause/resume
  races, monitor-thread recreation, power/thermal edge cases and streak
  semantics, `SecureStoreSecretProvider` failure modes, node start/stop
  leak detection, bridge-death-mid-run, flaky sensor bridges, and a
  required 5-second sensor soak (monotonic heartbeats, bounded log).
- `tests/test_ros2_transport_stress.py` (27 tests): bridge death,
  post-shutdown publish/subscribe, garbage drains, concurrent publish
  bursts, rate-limiter + emergency-stop bypass, payload round-trip
  fidelity, NaN/Inf sanitization, cross-transport parity.
- `tests/test_thermal_stress.py` (20 tests): ladder transitions,
  1000-cycle oscillation soak, thermal/accelerator-failure interaction,
  garbage thermal values, recovery semantics.
- `tests/test_model_execution_stress.py` (20 tests) plus
  `tests/fake_llama_server.py`: a loopback llama.cpp/Ollama/LM Studio
  wire-protocol test double driving the real `requests` HTTP paths —
  launcher detection, timeouts, malformed/500/empty responses, and
  concurrent generation.

### Fixed — found by the stress suite

- `AndroidShugoCoreNode.stop()` leaked its memory-worker thread
  (`MemoryManager.shutdown()` was never invoked; one thread leaked per
  node instantiation).
- `JavaBridgeROS2Interface.spin_once` crashed on malformed payloads
  returned by `drainMessages` (unguarded JSON parse).
- `RosBridgeInterface.spin_once` crashed on valid-JSON-but-not-dict
  packets (e.g. a bare string).

### Changed

- Package version aligned at 1.2.0 across `pyproject.toml`,
  `version.py`, and the test pin.

## [1.1.0]

### Added - Android compute nodes (hardware-agnostic mobile integration)
- `acceleration.py`: NPU/DSP/GPU/CPU accelerator abstraction with per-workload
  preference ladders, failure-induced degradation, and thermal demotion
  (NNAPI/Hexagon/Jetson DLA/Intel NPU enumerators; deterministic CPU fallback).
- `android_bridge.py`: `JavaBridgeROS2Interface` (Chaquopy + jros2/Fast-DDS
  in-process) and `RosBridgeInterface` (rosbridge over WebSocket for Termux).
- `android_runtime.py`: app-lifecycle mapping, WakeLock/MulticastLock,
  Keystore-backed secrets, battery/thermal monitoring -> fallback triggers.
- `android_node.py`: on-device node roles - `sensor_node`, `compute_node`
  (LiteRT/NNAPI), `operator_node` (clamped teleop relay), `full_agent`
  (offline local model on Ollama/llama.cpp/LM Studio, endpoint-allowlisted).
- `mobile_nodes.py`: host-side fleet layer - pairing with TTL, topic ACL
  (`/shugocore/mobile/#` only), payload sanitization, heartbeat liveness,
  topic-based compute broker, execution handler.
- Policy/engine wiring: mobile action types (consent-gated compute offload),
  loopback model-endpoint allowlist, 6 new fallback triggers; fixed the action
  parser so robot_*/mobile_* proposals are no longer silently dropped.
- Offline-first: ShugoCore remains dependency-free (websocket-client optional
  for Termux only); `platforms/<android>` code is quarantined and the core is
  verified to import and run with platform modules hard-blocked.

## [1.0.0]

### Added — Engine hardening & determinism (Phase 1)
- `state_machine.py`: `ExecutionGovernor` with an allowed-transition matrix,
  re-entrancy guard (recursive tool->agent->tool loops are refused), per-task
  step budgets and wall-clock deadlines.
- `fallbacks.py`: `FallbackController` with deterministic, rule-based safe
  escalation (`pause` default, `safe_state`/`halt` for critical triggers).
- `token_budget.py`: dependency-free token estimator and `ContextBudget`
  with rigid per-section allocations; the Tier 0 scratchpad now evicts on a
  token ceiling and decisions trim memory context to budget.

### Added — Memory tiers (Phase 2)
- Tier 1 `EpisodicMemory` is now a crash-safe append-only journal
  (`episodic_journal_path`) with startup replay and age-based eviction.
- Tier 2 `SemanticMemory` gained an entity graph (`entities`,
  `fact_entities`): deterministic extraction, `facts_about` /
  `related_entities` graph queries, merged with vector similarity in hybrid
  retrieval.
- Tier 3 `CoreIdentity.system_prompt()` deterministically renders
  world-model invariants as immutable operational rules.
- Async maintenance worker gained a watchdog (health stats) and exponential
  backoff, escalating to the fallback controller on repeated failures.

### Added — Enterprise & developer surface (Phase 3)
- `version.py` (`__version__ = "1.0.0"`), `pyproject.toml` (installable
  package, `shugocore-verify-audit` console script).
- `telemetry.py`: optional OpenTelemetry hooks with a built-in no-op tracer
  (zero-dependency guarantee preserved).
- `benchmarks/run.py`: local-first benchmarks for tool-calling accuracy,
  memory compaction fidelity and step-execution latency.

### Security & integration (from the previous hardening work)
- Single gated execution path; hash-bound policy verdict tokens; consent
  registry; approval broker; capability allowlists; egress controls;
  redacted logging; tamper-evident audit chain; honest (`not_implemented`)
  execution.

## [0.4.0] - 2026-08-31
### Changed
- README expansion: project positioning, design principles, orchestration
  loop, safety model, install guide.

## [0.3.0] - 2026-08-31
### Added
- Tiered memory system (Tier 0-3) with consolidation pipeline, promotion
  review, Tier 2->Tier 3 ledger-backed promotion.

## [0.2.0] - 2026-08-31
### Fixed
- Integration bugs: optional torch/chromadb, autonomy loop guard, subprocess
  and URL-construction fixes, missing RL/vector/model APIs, ethics-gate
  demo task. Added `.gitignore`, `requirements.txt`.

## [0.1.0] - 2026-08-31
### Added
- Initial modules: decision engine, autonomy, execution layer, model
  manager, reinforcement learning, task manager, vector DB, logging,
  subconscious (Ollama subprocess), memory scaffolding.