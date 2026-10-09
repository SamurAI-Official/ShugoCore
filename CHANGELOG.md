# Changelog

All notable changes are documented here. This project adheres to
[Semantic Versioning](https://semver.org). The 1.0.0 public API surface is
frozen: no breaking changes across any 1.x release.

## [1.30.28] - 2026-10-07 — a world session for the world claims

### Three rows that could not be judged, and one that was judged wrongly

The claims matrix is this repository's feature-verification instrument: 22 claims,
each with checks, a captured artifact and a `proven` / `unproven` / `failed`
verdict. Three rows had never been evaluated; closing them turned up a fourth that
had gone from `proven` to `failed` for a reason that was not a defect at all.
**18 proven / 3 unproven / 1 failed → 21 / 1 / 0.**

- **`world.sandbox` was not evaluable on any machine.** It is judged from a
  `[WORLD]/[GOAL]/[ACTION]/[REPLY]` transcript, and nothing produced that shape
  for the sandbox: it writes one audit chain per scenario and prints a pass/fail
  table. `actuation_sandbox.py` gained `--transcript`, backed by
  `world_transcript(rows)`, which builds the transcript **from the run's own
  rows** — the status, wire count, chain count and the target's HTTP status all
  come from the row, and the reply says what the target actually answered
  (`HTTP 200`, 33 chain entries). A refusal writes no reply and no `(gated)`
  marker, so it cannot read as engagement; a run that omitted the scenario writes
  no `[WORLD]` line at all, so the parser reports *could not evaluate* rather than
  *contradicted*.
- **`world.robotics` was not missing evidence so much as missing a caller.**
  `DecisionEngine` registers a `RoboticsExecutionHandler` for every robotics
  action type whenever it is given one — and **no composition root ever
  constructs one**. (The *mobile* handler is wired by `shugocore_server.main()` in
  exactly the same way; robotics is not.) `tests/test_robotics.py` was the only
  place it was ever built, so the claim could not be evidenced because there was
  nothing to run. `runtime/tools/robotics_session.py` (new) drives it offline —
  the deterministic ROS 2, MoveIt 2 and Gazebo stubs — and pushes an operator's
  goal through the engine's own two gated steps, `_gate_decision` and
  `_execute_gated`. The engine's audit chain records the real execution beside the
  transcript. Scope is stated in the row: a simulation with the stub solver, which
  is what the claim says; not a real MoveIt 2 solver or a physical arm.
- **`mesh.quest3` is unproven for hardware, and now visibly so.** No Quest
  headset is attached on this machine and `quest_probe.py` says exactly that and
  exits 0, so the row is unproven rather than failed — there was nothing to fix.
  What it gained is a reason a reader can check: `tests/test_quest_probe.py`
  already proves the positive path works when a transcript exists
  (`a_reached_headset_is_proven_by_the_real_parser`) and proves the five ways it
  must not be proven, including
  `no_headset_attached_says_so_rather_than_inventing_one`. The instrument is
  ready and only the headset is missing — a different statement from "unproven,
  cause unknown", and the one the evidence supports.
- **`reasoning.model_backed` was red for a service, not for a defect.** The
  scenario's own budgets (180 s warm-up + 180 s hold + a 300 s subprocess timeout)
  exceeded the runner's fixed 300 s per command, so a cold or absent model server
  got the scenario *killed before it wrote anything* — and a row with no
  transcript was then judged `failed`. The warm-up request could also outlive its
  own budget, because `requests.post(timeout=300)` sat inside a 180 s loop whose
  deadline is only checked once the call returns. The request is now bounded by
  the remaining budget, the claim's budgets fit inside the cap
  (`--warm-seconds 60 --hold 60 --timeout 120`), and `model_backed` returns `None`
  — *could not evaluate* — when the transcript's own warm-up line says no model
  answered. Both directions are pinned: an unavailable endpoint is unproven, while
  a *warmed* session that announced nothing stays `False`, so the new branch
  cannot swallow a real failure.

### A test that passed here and failed on Linux

CI on the 1.30.27 tag failed on all five Pythons at
`python -m unittest discover -s tests`, while the same command passed locally.
The cause was a test of ours: `SqliteSentinelTestCase` asserted against a
`C:\somewhere` literal, and on Linux a Windows-style path is *relative*, so
`abspath` prepends the working directory. It was verified without a Linux host by
running the same arithmetic through `posixpath`
(`<cwd>/C:\somewhere/mem.db` versus `C:\somewhere/mem.db`), and fixed to use a real
temporary directory. That fix carries in this release: the published 1.30.27
*wheel* was never affected (tests are not in it, and `anchor` behaves correctly on
Linux), but its *sdist* does carry `tests/`, and `v1.30.27` could not be re-pointed
because PyPI is immutable and its Release workflow had already published.

## [1.30.27] - 2026-10-07 — four roots, one source of truth

### The runtime had four composition roots, and they had drifted

Four places build a `DecisionEngine` without going through the agent's
`create_agent`: `shugocore_server.build_engine`, `continuous_agent`, the
`android_node` device node, and the agent itself. Only the agent anchored its
state paths onto an absolute data dir and handed that dir to the engine as
`log_dir`. The other three passed relative names and no data dir, so their state
landed in whatever directory the process was started from.

This is not hypothetical: the repository root contained `semantic_memory.db`,
`audit_chain.jsonl`, `node_audit.jsonl` (227 KB), `node_journal_pixel8.jsonl`,
`node_journal_testphone.jsonl` and a **415 KB** `decision_engine.log` — the bug's
own output. The 1.30.25 release fixed a relative-path boot failure for one root
and left the others alone.

- **`state_paths.py` (new) is the single answer.** An absolute path is never
  second-guessed; a relative one is anchored once, at construction, under the data
  dir when there is one, otherwise under the working directory *at that moment*.
  Resolving per-use instead is what let a `chdir` relocate a live node's state —
  the failure `AndroidAgent`'s own comment describes. `:memory:`, SQLite URI
  filenames and Postgres DSNs are addresses rather than locations and pass through
  untouched; rewriting `:memory:` produced
  `sqlite3.OperationalError: unable to open database file` across the server suite
  until they were excluded.
- **`DecisionEngine` applies the rule to `memory_db_path`, `audit_path`,
  `episodic_journal_path` and its log file, and exposes them.** Every root reaches
  the engine, so this is the choke point that makes them agree — measured: **8
  unanchored state paths before, 0 after**, building every root from a throwaway
  directory.
- **`android_node.NodeConfig` gained `data_dir` and `journal_path`.** The node
  wrote `node_journal_<device-id>.jsonl` from a hardcoded relative f-string that
  ignored its own config entirely.
- **`continuous_agent`'s three path defaults now match its own CLI.** They were
  `None`, and `None` is not "leave it to the engine": it *overrode* the engine's
  real defaults, so the documented `ContinuousAgent(models=[...])` — straight from
  the module docstring — raised `ValueError` on construction while
  `python3 continuous_agent.py`, which defaults the same flags, worked. The
  engine's error message no longer blames `SHUGOCORE_MEMORY_BACKEND=postgres` for
  a caller who never set it.

`tests/test_composition_parity.py` (new, 15 tests) pins the promise from the
outside: every root's state resolves absolutely, the device node honours a data
dir, every root's engine still carries its gates, and the sentinels pass through.

### The claims matrix was not telling the truth

`claim_matrix.py` is this repository's own feature-verification instrument: 22
claims, each with checks, a captured artifact and a `proven` / `unproven` /
`failed` verdict. Driving it turned up three defects **in the instrument**, each
of which made it report something other than the truth.

- **"Contradicted" where it meant "could not evaluate."** `world_engagement` — one
  parser serving `world.robotics`, `world.xr` and `world.sandbox` — returned
  `False` for an absent transcript, while its own docstring says such a row *stays
  unproven*, and its sibling `quest3_reach` already returned `None` for an absent
  headset. So a machine with no headset reported the XR claim as **failed**. It
  returns `None` now, and reads the `verdict: world=...` line the producer writes,
  so "the surface never started" is distinguishable from "the session ran and did
  not engage". The latter is still `False`, pinned by a test, so `None` cannot
  become a blanket excuse.
- **Running the matrix destroyed the evidence it was judging.**
  `runtime/tools/xr_session.py` writes `runtime/evidence/world.xr.txt`, and that is
  the same file the `world.xr` live check reads. Its `--out` defaulted there, so a
  sweep on a headset-less machine overwrote the recorded successful session
  (2026-10-03, `presence=xr`, `replied=True`) with `world=none` — and then judged
  the result. The claim's verdict depended on whose machine ran it, and running it
  destroyed the record. The command now writes into
  `runtime/evidence/xr_session/`, and the live check keeps reading the record.
- **The probe's exit code, not the judged evidence, decided the row.**
  `run_claim` treats any non-zero command exit as `failed`, and `xr_session.py`
  exited 1 unless the session both acted *and* replied — exactly what a missing
  headset produces. It exits 0 whenever it wrote a transcript, and still 1 for its
  three setup failures (no Godot binary, no desktop server, a server that never
  answered), because those are the tool failing rather than the environment
  lacking hardware.

Result: **18 proven / 3 unproven / 1 failed → 19 proven / 3 unproven / 0 failed.**
`world.sandbox` stays deliberately unproven and now says why: it is judged on a
`[WORLD]/[GOAL]/[ACTION]/[REPLY]` transcript and nothing produces that shape for
the sandbox. Marking it proven from the sandbox's own table would be the exact
thing the matrix exists to prevent.

### Four modules nothing could reach, and two of them were defects

- **`check_apk.py` shipped a module that could not be imported.** It was a bare
  script declared as a `py-modules` entry, so *importing* it opened
  `platforms/android/app/build/outputs/apk/debug/app-debug.apk` and raised
  `FileNotFoundError` on any machine without that exact build. The body lives in
  `main(argv=None)` now, the archive is an argument, a missing APK reports and
  exits 2 instead of tracebacking, and a doubled `apk.close()` is gone.
- **`android_model_manager.ModelManager` is a stale copy of the live class** —
  same method set, but it never received `MODEL_PERFORMANCE_CAP`, so its
  performance tracking is unbounded where the live one is clamped. Nothing imports
  it. It is marked deprecated with the divergence spelled out rather than left as
  a trap, and **not** deleted: the 1.x policy removes deprecated functionality only
  after at least one minor release of deprecation.
- `talker` (a published placeholder with no code, by its own docstring) and
  `test_local_run` are recorded as decisions.

`tests/test_module_reachability.py` (new) keeps it that way: an unreachable root
module fails the suite unless justified; a justification that goes stale fails
too, so an excuse cannot outlive its reason; and a class name defined in two root
modules must be recorded.

### The map the repository was missing

`ARCHITECTURE.md` now states the composition roots, the state-path rule, which
module enforces which invariant, the derived artifacts that must never be
hand-edited, and which commands actually run the suite. Its absence is what let
the roots drift in the first place.

`SECURITY.md`'s verification block told a reader that
`python -m unittest discover -s tests -v` **is** the full suite. It is not: 30
tests are invisible to that runner, which is exactly why CI runs a second one.
Both commands are named now, with counts measured rather than remembered, and
`tests/test_suite_completeness.py` fails if a `tests/test_*.py` file becomes
collectable by neither runner — the way a suite silently stops running.

The root is clean: the eleven orphaned `.release_notes_v*.md` files (v1.2.0
through v1.30.5, tracked, referenced by nothing) moved to `docs/release-notes/`,
and the stale node state the relative-path defect left behind is gone.

## [1.30.26] - 2026-10-06 — the desktop window, driven and honest

### The desktop window could be watched but never used

The control plane opened and drew six panes, and no operator could do anything
with it: nothing registered a speech listener in GUI mode, so a typed turn was
handled, decided, and then delivered to nobody. Three defects, all found by
driving the window offscreen and pressing everything.

- **No speech listener, so the node had no mouth.** `register_speak_listener` was
  called only inside `run_terminal`, so in GUI mode `_speak_direct` returned
  False and every reply was dropped -- and `can_speak` stayed false, which also
  meant the hive would never place an answer on this node. The window now
  registers a `UiSpeaker` when it builds a node (via
  `AgentController.speak_listener`), and replies render in the window.
- **The AGENT tab had no way to talk to the node at all**, though
  `AndroidAgent.speak_test` and the README both name "the AGENT tab's Test
  speech control". Added both: a **Talk to the node** box that drives
  `handle_typed_input(text, source="desktop-ui")` -- the agent's own
  conversational seam, on a worker thread so a model round trip cannot freeze
  the window -- and the **Test speech** button that was documented but missing.
  The transcript prints `node> ...`, and a turn that is handled but silent says
  so instead of leaving the operator to guess between thinking, muted and no
  provider.
- **The SECURITY pane read an attribute the UI does not have.** `_consent_registry`
  used ``self.agent``, but the controller owns the node, so every 1 Hz poll
  raised and the whole pane reported "UI error" -- Grant and Revoke could not
  run. It reads `self.controller.agent` now, and the empty case says *why*
  (still starting vs not running) rather than "no consent registry".

**Selecting "Stub (offline)" broke every cycle.** `_backend_for` injected the
delegated URL into whatever backend config the registry held, including the stub,
which takes no constructor arguments -- so `create_backend` raised
`StubBackend() takes no arguments`, the engine logged `BackendError`, and the one
backend a person reaches for when there is no model to reach was the one that
could not work. A URL is now only injected into a transport that dials one. The
header also stops printing an endpoint beside "stub", which told the operator
about an address the node was deliberately not using.

Verified offscreen against a live LM Studio endpoint: `you> hello, who are you?`
answered by `zai-org/glm-4.6v-flash` with the reply landing in the transcript and
`Decision source` naming the model; `Test speech` executing through the gated
path; every button on every pane pressed with no UI error. `tests/test_desktop_surface.py`
pins all of it without a Tk root, so the suite still runs on a headless CI host.

### Closing the window, and the two checks that lied

With the tabs usable, the next things an operator does are quit -- and, on a
security pane, press the button that says it verifies something. Both
misbehaved, and the log pane quietly disagreed with itself.

- **Closing the window froze it, and hung on the way out.** `_on_close` ran
  `controller.stop()` on the Tk main thread: an up-to-8 s thread join followed by
  the agent's cleanup -- consolidation, the task-manager worker, the mesh
  runtime. The window drew nothing for the whole teardown and wore a
  "not responding" title while it worked. The teardown now runs off-thread, the
  header says *stopping the node ...* so the wait is explained rather than
  mysterious, and a main-thread watchdog destroys the window once the node is
  genuinely gone. Measured: close returns in 0.1 s. The poll timer is tracked and
  cancelled on the way out, and a closing window stops polling -- the pending
  `after` used to fire after `destroy()` and print a `TclError` on every exit.
- **Verify audit checked the field, not the node.** `applied` recorded only the
  backend and the model, so the button read the *editable* Data dir box. Edit
  that box after Start -- an ordinary thing to do -- and a security control
  reported **CHAIN INVALID** for a path the node had never written. The
  controller records the directory it actually opened now, and the check reads
  that first, with the field as the fallback before any node exists. Measured:
  with the field pointed elsewhere the chain still reads `chain OK`.
- **Verify audit hashed 57-102 MB on the main thread.** Measured 57 MB on an A16
  and 102 MB on a Tab -- seconds of frozen window per press. It runs on a worker
  thread now and reports *verifying ...* while it works, like Detect models.
- **The LOG pane's count disagreed with its own contents.** Entries filtered out
  by the level box were counted anyway, so "N seen" described arrivals rather
  than what the operator could read; it is "N shown" now. Re-render reads the
  controller's snapshot instead of reaching into `controller._logs`, which had
  stopped being the same stream the pane accumulates.
- **Validation › Audit chain printed `None`.** `str(...)` of an absent policy
  field put the literal word on the first screen. It reads *not reported* now,
  like its sibling rows.

Verified with an 18-check matrix driven through the real widget tree -- first
screen, a stopped node, mid-start, a stale Data dir, a turn plus Test speech,
level filters, and the close path: all pass, no `TclError`, close in 0.1 s.
`tests/test_desktop_surface.py` grows to 33 headless tests, so the suite still
runs on a host with no display.

## [1.30.25] - 2026-10-06 — one decision, one mouth

### The interactive pipeline, driven rather than read: three defects it was hiding

Running the operator terminal against a live model endpoint (`--terminal --backend
"LM Studio (OpenAI-compatible)" --url http://127.0.0.1:1234 --model
zai-org/glm-4.6v-flash`) answered **nothing**. The suite was green, which is the
point: every one of these is invisible to a unit test that never makes a real
turn, and each one silently degrades the node rather than failing it.

- **A relative `data_dir` booted a node with no engine.** `create_agent` stored the
  path verbatim, then `_bootstrap` chdir'd into it -- so every state path was
  resolved a *second* time against the new cwd, `runtime/node` became
  `runtime/node/runtime/node`, `decision_engine.log` could not be created, and
  `DecisionEngine` construction raised `FileNotFoundError`. The node logged
  "agent ready" and could never decide anything. `scripts/agency_session.py`
  carried a local `os.path.abspath` and a comment describing exactly this; the
  path is now absolutized once, in `AndroidAgent.__init__`, where it lives.
- **A conversational turn talked to the wrong endpoint.** Choosing a backend
  re-points the engine's model registry, but `_make_conversational_decision`
  asked `_backend_for({"id": name})` -- a synthesized dict with no `"backend"`
  key -- so `_backend_for` returned `None` and the call fell through to the
  subconscious's *global* backend. Measured: LM Studio on :1234 had a working
  model, the terminal was pointed at it, and every reply still failed against
  Ollama on :11434 with `BackendError`. The structured-decision path had always
  passed the real registry entry; conversation -- the path every reply a user
  sees -- was the one caller that did not. It now resolves the registry row
  (`_model_entry`), and the accepted proposal is tagged with the model id as
  `proposal_source`, so `/status` no longer reports `backed by none` after a
  model answered.
- **A node that had lost the mesh lease went mute to the operator at it.** The
  primary-only guardrail is deliberate -- a follower must not duplicate an
  action the primary is performing -- but it also swallowed the reply to a turn
  the node had just been handed: 5 of 5 typed turns answered with no words at
  all, with the model healthy behind them. The first fix was to exempt an
  operator turn; that was wrong (it let a follower produce user-facing speech
  with no lease), so the turn is now **forwarded** instead -- see the next
  section.

### One decision, one mouth: a follower forwards its turns

The guardrail above says a node without the lease does not decide and does not
speak. The defect was that the operator sitting at that node then got nothing. The
answer is not to exempt the node but to route the turn: a follower does all of its
*read-only, device-local* work -- memory, dialogue, growth, intent, and any
measurement only it can take -- and then sends the turn to the primary
(`orchestrate/turn`), which decides and routes the reply back as a delegated
action. The operator is answered at the device they used; "who speaks in this
hive" stays one node's decision.

- **The turn carries what only that node knows.** The follower's proximity facts
  (`terminal_active`, a face, recent speech), whether it can speak, and -- when a
  deterministic answer applies -- the answer itself as a *proposal*: a phone's
  battery is the phone's, and the primary decides whether to say it and where.
  The reply is routed back to the node that took the turn, so a keyboard operator
  is answered at their own screen rather than on whichever device a face was seen
  near.
- **`terminal_active` is now advertised** (`remote_terminal_active`, while the
  input is recent). It was a self-only fact, so a primary could not place a reply
  at a terminal it had not heard of; `response_routing.normalise_facts` already
  accepted the remote spelling, so no router change was needed.
- **An unreachable primary does not mean a mute terminal.** The origin waits,
  bounded (`_FORWARD_WAIT_S`), for the primary to confirm the hand-off; a refusal,
  a dead peer, or no answer clears the one-mouth invariant (the primary provably
  did not answer) and the node answers locally. The permission is per-turn and
  thread-scoped, so a concurrent tick's speech cannot inherit it, and it is
  granted *only* when a live primary failed to take the turn -- never for ambient
  speech.
- **No loops.** A reply travels as a delegated action, which executes directly and
  never re-enters the turn pipeline, so a forwarded turn cannot ping-pong.
- **A failed route no longer drops the utterance.** Found while isolating this
  verifier: the router had legitimately chosen a LAN peer (a face scores 0.35,
  above a keyboard's 0.30), the delegation was refused because that peer was not
  in this node's map, and `_execute_speak` returned the refusal -- so the reply
  vanished although it had already been decided. A route whose status is not
  `delegated` now falls through to this node's own speaker, and `ask_user`
  behaves the same way. `scripts/verify_interactive.py` also isolates the node
  from the mesh by default (`--allow-mesh` opts out): a live LAN makes reply
  placement evidence about the LAN rather than about the node under test.

`scripts/verify_hive_turn.py` is the evidence, on real TCP with two nodes: the
follower takes the turn and says nothing, the primary decides, and the reply
returns to the follower -- measured as `hub heard nothing locally -- one decision,
one mouth`. It exists because this machine has live nodes on its LAN, so "is this
node a follower?" is not something a single-node run can make deterministic.

### The device probe, run rather than trusted

The adb smoke probe was pointed at two real phones (a Tab S9 FE and an A16 5G on
the same LAN) for the first time from Windows, which is where these came from.

- **The probe could not run on Windows at all.** Its adb path was hardcoded to
  the macOS SDK (`~/Library/Android/sdk/platform-tools/adb`), so it died with
  `FileNotFoundError` before it could say one thing about a device. It now
  resolves adb from `SHUGOCORE_ADB`, then `ANDROID_HOME` / `ANDROID_SDK_ROOT`,
  then the per-OS default, then `PATH`, and takes `--adb` explicitly.
- **A correctly-governing node was reported as a broken one.** The Tab ticked
  every second and produced no decisions, which the probe waited five minutes to
  notice. It was not broken: a capable peer held the lease, so the Tab was
  deliberately not running its own model loop -- top-down orchestration by
  capacity, the fleet doing its job. The verdict was invisible over adb because
  `AndroidAgent.log()` writes only to the in-memory bus the UI polls. The mode
  now also reaches stderr (`ORCHESTRATION: subordinate: peer ... holds the
  lease`) on each *change*, and the probe reports a deferral as `exit 3` with the
  reason instead of blaming the device for a decision it was right not to make.
- **`decision_engine.log` had no rotation.** Measured on the A16: **113 MB** and
  climbing, one full decision dict per decision every few seconds, while the
  project promises capacity-bounded state everywhere. It now rotates by size
  (5 x 4 MB), so recent history survives a crash and the file cannot exceed
  ~20 MB. The audit chain is deliberately *not* rotated -- it is an append-only
  tamper-evident ledger, and that is a different decision with different stakes.

Measured on the A16 after these fixes: six phases passed across three rounds
(`service_alive`, `time_tool_query`, `measurement_honesty`, `fact_stores`,
`memory_question`, `personality_model_genesis`), decision cadence 1.3 s, thermal
status 0. The Python fixes in this release are not in the installed 1.30.24 APK,
so they are validated by the host suite and the live-endpoint runs rather than on
the phone; the harness changes are what make that phone run possible.

### Memory recall said the attribute twice

`my name is Ada` was recalled as *"You're Ada. You're name Ada."* -- not a
cosmetic quirk but durable corruption. `FactMemory._extract`'s `attribute` pattern
(`my <x> is <y>`) has two groups and the stored template re-injects `group(1)`,
but the value was built by joining *every* group -- so the attribute name landed
in its own value (`User's name is name Ada`, `User's sister is sister Ana`), in
`user_facts.json`, in prompt injection, and in every recall. The value now comes
from `group(2)` alone, so `my car is red and I like it` still trims to
`User's car is red` and a name asked for both ways collapses to one fact. The
existing suite asserted `fact_to_second_person("User's sister is Ana")` -- a clean
string the extractor never produced -- which is why this shipped green.


`scripts/verify_interactive.py` is the evidence and the regression net: it builds
one real node, warms the endpoint, drives typed turns through `handle_typed_input`,
and prints what was typed, what came back and what backed it. It fails only on a
silent turn -- a deterministic clock answer is reported as rule-backed, not
counted against the model. Against the live endpoint: **4 of 4 turns answered, 0
silent**, 2 by the model and 2 by deterministic tools; and a two-way turn
("my name is Ada" -> "what is my name?") answers from the model and recalls from
memory. `tests/test_interactive_pipeline.py` pins all three defects.

### The Quest 3 on the desk: what it is, and what it can actually do

The XR work had been reasoned about from the scaffold's side only. Checking the device itself
over adb changed two conclusions.

- **A Quest 3 is attached and has the client installed.** `adb devices -l` lists
  `2G97C5ZH5P01GZ  product:eureka  model:Quest_3` (Android 14, arm64-v8a), and
  `com.samurai.shugocore.xr` v1.0 is installed on it. The headset is on the fleet's LAN
  (`192.168.1.151`) and reaches this desktop (`192.168.1.152`) and the Mac (`192.168.1.162`):
  an HTTP request from the headset to either answers, while `ping` from the same headset
  reports 100% loss — Windows blocks ICMP, so a failed ping is not an unreachable host.
- **The client is unconfigured.** It has no `user://shugocore_xr.json`, so it still points at
  the shipped placeholder `http://192.0.2.10:11435` (TEST-NET-1). Nothing on the LAN serves the
  engine API either — the Mac answers on 11434 (Ollama) and its 9000 is the raw mesh transport,
  not HTTP. So the headset has never reached an agent, and no headset would until one is up.
- `runtime/tools/quest_probe.py` starts a LAN-bound, token-gated desktop server and lets the
  **headset** make the requests: its status, a task, and the same request with no token. The
  answer is read from the *server's* log, so neither side's word for it is the evidence.
  `mesh.quest3` is proven while the headset is attached, and reads `unproven` when it is not.
- **Found by doing it:** the agent answered the headset's message — it decided
  `{'action_type': 'speak', 'params': {'text': "I'm here — what did you want to talk about?"}}`
  — and the surface received `not_implemented: no speech provider registered for 'speak'`. A
  headless server has no loudspeaker, so **no remote surface (Quest, XR, phone, node) can
  receive the agent's answer**. That error is the whole of the missing "answered there" half,
  and it is now reproducible on demand instead of inferred.
- The matrix gained a third state for live checks: a checker may return `None` for "could not
  be evaluated" (its instrument was absent), which is `unproven` — never `failed`. A headset
  that has gone to sleep is not a claim that failed.



### A backend outage is the machine's weather, not the model's judgment

Three failed requests — a server still loading its weights, a refused connection — were counted
as "the model proposed nothing", which armed the proposer backoff: a 30 s+ window that outlives
the outage, during which the node skips the model and answers with rules. Measured live, that
is how a model-backed claim came back "every decision was backed by a rule rather than a model"
while the model was ready and answering.

- The engine now keeps two streaks: "the model answered and gave nothing usable" arms the
  backoff, "the model was never reached" does not. An unreachable backend keeps its
  substitution — two honest no-action cycles, then the safe rule action from the third, so the
  loop stays productive and the shell still reports `BACKEND_FAILURE` — but recovery is
  immediate once the server answers again.
- The model-backed scenario warms the endpoint before it measures: it tries *every* candidate
  name rather than the first listed (this machine lists a second 27B that cannot load — it asks
  for 64.74 GB — beside the one that works, in an order that is not stable), waits with a
  timeout long enough to **load** a model rather than only to serve one, and records in the
  transcript which model answered or that nothing did.
- The scenario's default prompt is a sentence the node must *decide* ("the charger is warm to
  the touch…"). The battery question was answered by the node's own command router, so the
  transcript showed a rule where this claim is about the model.

### A node can be asked to run a named task on itself

The hive could ask a peer to speak or to host an RPC peripheral, and nothing else: every other
job an operator does to a machine (check it against the fleet's declared state, pull the repo,
run the suite, rebuild the RPC server) happened by hand, outside the governance layer — no
consent, no approval, no audit. `dev_task` makes it an ordinary gated capability, and the shape
is the safety property: **the hub names a task, and the peer decides what the name means.**

- `dev_tasks.py` holds the registry (`node_consistency`, `run_tests`, `git_pull`,
  `build_rpc_server`, `fleet_status`), the peer's executor and the hub's handler. Nothing that
  crosses the mesh is an argv: the wire carries `{"task": "..."}` and nothing else, a request
  carrying `argv`/`command`/`cwd`/`shell`/`env` is refused *by name*, and `run_named_task` has
  no parameter to pass one through.
- The peer resolves the name against its own registry, refuses a task it cannot run with the
  missing piece named (`git`, `pytest`, the script), runs it `shell=False` with its own
  interpreter, and audits `dev_task_started` / `dev_task_finished`. `fleet.dev_task` is proven
  by a hub and a peer as two real processes: the transcript carries the peer's own line (argv,
  exit code) and its audit entries.
- Fixed: `ExecutionLayer.register_handler`'s allowlist predated the fleet action class, so
  `register_fleet_handlers` raised and `fleet_deploy`'s engine wiring was dead while looking
  installed — the engine consent-gated an action it then answered with "Unknown action type".
  A regression test pins it.
- Fixed: `register_fleet_handlers` took its types from the whole shared fleet class, so adding
  a member to that class would have installed the *deploy* handler under `fleet_dev_task`. A
  handler now names the types it serves.
- Fixed: `/nodes` called the node it runs on "unknown" — `get_status()` has no `node_id`, and
  the roster read it from there.
- The lab peer's port moved off 9001, which this mesh also uses for its relay; a node whose
  bind silently lost that race looked exactly like a peer refusing work. `devlab_node.py` now
  asks its own port whether anything is listening, and says so when the answer is no.
- An HTTP error from a model backend now carries the status and the server's own words. A bare
  `HTTPError` printed "the model call failed" while LM Studio had already said why, and a cold
  27B (unloaded after an idle gap) looked like a broken pipeline until it was asked directly.

### The fleet surface: `/nodes`, and a mesh proof that survives its own success

The two mesh rows were the last claims with no evidence, because nothing recorded what a hive
looks like from inside one of its own nodes.

- `runtime/tools/fleet_status.py` brings the desk node up the way the operator terminal does
  (the same `AgentController`, so the transport, the identity and the fleet token are the real
  ones), holds the lease at the hub's documented priority, listens, and writes
  `runtime/evidence/fleet_status.txt` in the shape the existing parsers already read. A live
  run: four peers heard -- the Mac at priority 10 among them -- `role=primary`, memory imported.
- The memory claim reads the **durable** count, not a session delta. `imported: 0` is a healthy
  sync on a converged fleet -- the client's own sync loop says as much -- so a claim that reads
  a session counter fails on the *second* run of a working hive. The transcript therefore states
  `shared_from` provenance from `FactStore.count_shared()` first: 32 facts held from peers while
  this session imported none, which is what "memory crosses the mesh" actually means.
- `/nodes` in the operator terminal answers with the roster this node itself has heard
  (`telemetry['mesh_peers']` plus its own election), marks who holds the lease, and says "none
  heard yet" rather than implying an empty fleet.
- `mesh.election` and `mesh.memory` are now **proven** from that capture.

### A local model actually backs the decisions, and the health surface stops flattering it

LM Studio serves this machine's model on 127.0.0.1:1234, and the node could not use it:
`OpenAICompatibleBackend.generate` raised `BackendError: environment variable OPENAI_API_KEY is
not set` *before* any HTTP call, because the OpenAI wire wants a key and a local server usually
has none. From outside it looked like a node whose every model call failed while the model sat
there loaded and answering.

- A keyless endpoint is now allowed **only** on loopback (`_is_loopback`): LM Studio,
  llama.cpp's server, vLLM and a local ShugoCore server all answer without one, and the
  requirement keeps its real purpose -- keeping a prompt from being sent somewhere the operator
  did not mean -- for every remote host. The `Authorization` header is omitted rather than sent
  empty when there is no key.
- The OpenAI-compatible default timeout moves 30 s -> 120 s, matching the Ollama backend: a 27B
  model took 65 s to load on its first call, and 30 s turned "slow" into "broken".
- With that, the model takes the decisions: `proposal_source: 'prism-ml/bonsai-27b'` with
  `action_type: speak` and `status: success`, and the node initiates speech of its own accord
  between turns.

**The health surface no longer flatters a fallback.** `pipeline_health` took `model_ready` as a
truth from the shell, and the shell passed `self.engine is not None` -- so a node with no model
served reported `model=ok` while every call failed. The pipeline now carries `backing` (a model
id, or the name of the rule that stood in) and the `model` stage is judged by whether a *model*
produced the last decision (`_model_backed`, which excludes the standing-rule names
decision_engine uses). The operator terminal prints backing as it changes, beside the routing
line it already printed, and `/status` shows the pipeline and what backed it.

That makes the claim provable rather than assertable: `reasoning.model_backed` runs the real
console against the local server and requires the transcript to show a model id backing its
decisions, with no cycle that could not reach it. On this machine: **proven** (`decisions
backed by shugocore-local`, after a turn whose reply the model wrote). The endpoint is
`SHUGOCORE_MODEL_URL`-overridable and the wire is OpenAI-compatible, so the same row works
against llama.cpp, vLLM or a ShugoCore server -- Ollama is one convenient option, not an
assumption.

**The world rows exist, and three of them are honestly unproven.** `world.robotics`,
`world.xr` and `world.sandbox` each run their own verification suite (ROS 2 transport, the
simulation layer, the Godot/OpenXR bridge scaffold, the actuation sandbox) and then judge a
*session* transcript in the shape a world producer writes -- `[WORLD] [GOAL] [ACTION (gated)]
[REPLY]` -- because interfaces passing their own tests is a different claim from an operator
being reached and answered in a world. No such session has been run, so they read `unproven`,
which is not `failed`: the matrix says what is missing instead of hiding it. `world.desktop` is
proven, and that parser learned something on the way -- `ungated` contains `gated`, so a
bypassed gate read as engagement until the word boundary was fixed.

### The hive can prove it is operable: agency claims, with transcripts as evidence

`claim_matrix.py` already held the system to its *safety* claims (election, consent,
containment, rollout, the audit chain). Nothing held it to the claims that decide whether it
is actually running: that it acts on a goal later with nobody typing, that a fact outlives the
process, that it grows, that it hears and answers, that it reports its own organs honestly.

Seven rows do now, in the same shape as the rest -- a command that runs a session, a pure
parser that judges the transcript, and an artifact left in `runtime/evidence/`. On this fleet:

| Claim | Verdict |
|---|---|
| `agency.timed_autonomy` -- accepts a goal, then acts on it unprompted | **proven** |
| `agency.memory_durability` -- a fact survives a restart | **proven** |
| `agency.personality_growth` -- a window of turns grows a generation | **proven** |
| `agency.perception_to_action` -- a *heard* phrase reaches the path and gets a reply | **proven** |
| `node.pipeline_health` -- every organ reported, in a vocabulary that can say `unknown` | **proven** |
| `privacy.no_third_party_egress` -- a session reaches no analytics host | **proven** |
| `world.desktop` -- the operator terminal reaches the agent, labelling words honestly | **proven** |

- `scripts/agency_session.py` is the producer: it builds the node the way a phone does, drives
  it through the typed-turn seam, ticks the real loop, and writes a transcript plus the node's
  own status JSON. Its exit code is about the *harness* -- whether the node behaved is the
  parser's verdict, and a parser returning False is a FAILED claim, never an unproven one.
- The parsers are pure functions of text, so unit tests re-check them without a node
  (`tests/test_claim_matrix_agency.py`, 25 tests), and the negative cases are the interesting
  ones: a reminder the operator had to repeat, a memory that only looked durable because one
  process answered it, a health surface that paints everything green, an analytics host in the
  transcript.
- As recorded: `[TIMER] timer fired` and `SPEAK: Timer is done!` with no turn between them;
  `My sister's name is Ana.` in a *new* process (`memory recall (question path)`);
  `grew gen 1 -> 2 (questions 40%; long session (25 turns); praised warmth); drift=0.0523`,
  persisted in `personality_model.json`; `HEARD ... accepted=True` followed by a spoken answer;
  a pipeline snapshot naming six organs with three of them `unknown`.

**The matrix found a real defect on its first run, which is what it is for.** The
`containment.actuation` transcript was full of
`HTTPSConnectionPool(host='us.i.posthog.com', port=443)`: Chroma reports usage to PostHog by
default, so an offline-first node's sandbox run was shipping analytics while the README claimed
privacy hardening. `vector_db.py` now sets `ANONYMIZED_TELEMETRY` and `CHROMA_TELEMETRY_ENABLED`
before importing the client (with `setdefault`, so an operator can still opt in deliberately),
and `privacy.no_third_party_egress` is the live check that keeps it that way: `failed` before
the fix, `proven` after, on its own transcript.

That parser had a blind spot its own tests caught: it scanned `http://` URLs, while a library
that cannot reach its analytics endpoint names the host in an *error*
(`host='us.i.posthog.com'`), not in a URL. Both forms are read now.

Also here: `/prove [ID]` in the operator terminal runs the matrix and prints its table, and the
terminal header carries the node's pipeline health, so operability can be *seen* from the
console rather than only in artifacts. Deliberately still unproven: `mesh.election` and
`mesh.memory` want a live hub status file; `reasoning.model_backed` waits for a language model
(none is served or downloaded on this fleet -- every answer here comes from the deterministic
fallback); and the robotics/XR world rows wait for a simulation and a headset.

### Vision: a camera that stops is not a camera that never started

Measured on the A16: CameraX bound, delivered analysed frames for 15 s, then closed with
`state=CLOSED, error=null` -- no exception, no crash, process alive. Nothing threw, so
nothing reacted; the first report came from the NRR camera probe three minutes later, by
which point a presence-based decision had three minutes of silently absent vision behind it.

- The watchdog that existed was one-shot: it checked `!firstFrameSeen` twenty seconds after
  binding, so a camera that stopped *after* delivering satisfied its own condition and was
  never looked at again. It now re-arms every 5 s for as long as the provider runs and judges
  by the **age of the last analysed frame**, not by whether one ever arrived.
- Its threshold is 90 s because the watchdog's own first version (45 s) flagged a healthy
  camera during a recovery: the number comes from what the watchdog measured about itself,
  not from a round figure.

**Presence measurement, with controls.** Both channels need the app in the foreground, and
audio lies about it. Backgrounded for 80 s the A16 produced **1 presence line**; screen off
for 110 s, **0** -- while the A51 and S9 FE, untouched, produced 10/17/10 and 18/21/14 over
the same windows. The world was unchanged, so the action caused the silence. Audio kept an
unbroken 1 Hz heartbeat throughout: 110 of 110 screen-off lines reported `rms=0.0000`
(median *and* maximum exactly zero) against zero zero-lines on the controls, whose medians
were ~0.045. Android silences background capture, so that heartbeat was reporting a dead
microphone as a quiet room -- the class of lie the sound contract exists to refuse.
`docs/vision_calibration_runbook.md` records the calibration attempts, the second clap
measurement, and why the distance claim fails.

### The operator terminal: typing to the agent, and a keyboard operator who can be answered

The hive had mouths on the phones and status panes on a desktop, but no console where an
operator could simply type to the agent and watch what it heard, decided and did:

    python clients/desktop/shugocore_desktop.py --terminal --say "hello"

- It is a second *front end* of the same node -- the same `AgentController`, the same tick,
  the same turn pipeline -- rather than a second agent. Typed words go in through the agent's
  own conversational path (`handle_typed_input`) carrying `source="terminal"`, so no journal,
  scene classifier or mesh agent can record them as something a microphone heard.
- The terminal registers a speak listener, which is what makes `can_speak` true and the node a
  response candidate. `can_speak` has always meant "the reply reaches a human", and printing
  counts.
- `response_routing` gains `terminal_active` (0.30). A keyboard operator has no face and no
  gaze, and `presence_present` (0.15) cannot cross the floor (0.20) by design -- so before
  this a screen could never be chosen, and the hive answered a phone in the room instead. A
  face (0.35) still outranks a keyboard, and the signal is set only while input is recent.
- `--say`, `--exit-after`, `--no-input`, `--mesh-token` and `--verbose` make it scriptable and
  headless; the Tk import is tolerant, so it runs over SSH on a host with no Tk at all.

Two faults the first smoke run found, both of which made it unusable rather than wrong: the
agent's logging setup configures the root logger, so a level set *before* boot was silently
overwritten and every INFO line appeared twice, once per second; and redirected stdout is
block-buffered, so a scripted run showed nothing until it exited -- while the mesh threads
kept the port bound, so the next run hung. Output is line-buffered now, and the terminal
exits through `os._exit(0)` after `controller.stop()`.

### The operator terminal has a voice, and the fleet's speech path is written down

`--terminal --voice` speaks replies aloud through PowerShell's `System.Speech` when the
host has one, passing the sentence in the environment rather than on a command line -- a
reply containing quotes, semicolons or a pipeline is a sentence to be spoken, not
something for a shell to parse. A host with no engine reports why in the header and keeps
printing: `can_speak` still means "the reply reaches a human", so response routing does
not move. `/say TEXT` drives the agent's own gated `speak_test()` -- the path the AGENT
tab's "Test speech" control uses -- so the terminal never becomes a second way for a node
to decide to talk. `synthesize()` writes the same sentence to a WAV instead of the
speaker, which is how a voice the test machine cannot hear is still measured (144,652
bytes of `Microsoft David Desktop` here).

Two fleet facts, learned by trying to make a phone speak into a terminal, are now in
`docs/android_integration.md`:

- A peripheral's ears reach a **paired Bluetooth primary**, not the mesh:
  `SensorPublisherService` streams over SPP, so a follower consumes its own transcripts
  locally and a desktop node on the node fabric never hears them. The desktop's role is
  orchestrator, not ear.
- Returning a phone from peripheral to primary does **not** restart its agent: the pane
  flips back while the header stays `AGENT OFFLINE` until the app is restarted.

### The node has an ear: hearing through its own recogniser

`--terminal --ear` listens on this node's own recogniser (PowerShell's
`System.Speech.Recognition`, the same stdlib-only subprocess boundary as the voice) and
hands each recognised phrase to the *fleet's* speech path -- the `HumanObservation(speech)`
a phone's recogniser posts, source `on_device_stt`. Attention stamps it as speech, routing
can pick this node for a spoken reply, and the journal records where the words came from.
Nothing marks them typed: typed turns keep their own seam, and keeping the two apart is
what the terminal is for.

- The microphone has one owner and **policy decides**, not the terminal:
  `ear_permission()` asks the agent's `apply_sound_policy()` and refuses when the mode is
  `sound` (the acoustic layer owns the capture) or `off`. An unreadable policy is a
  refusal, not permission.
- A node that hears itself would answer its own sentences forever, so a phrase arriving
  while the voice is speaking is dropped (`echo_dead_time`) -- the same dead time the
  phone fleet keeps.
- Honest degradation, twice over: no recogniser or no capture device is reported *with a
  reason* (a silent ear and a quiet room are otherwise the same observation), and an ear
  that stops says so instead of just going quiet.
- `transcribe_wave()` reads a WAV instead of the microphone, which is how the ear is
  proven on a machine that cannot talk to itself: the acceptance test synthesizes "check
  the battery level", listens to its own output, and requires the sentence back. Verified
  live here -- the round trip returns "Check the batter reliable", one phrase.
- `System.Speech.Recognition` has **no device chooser** (its only other inputs are a wave
  file, a stream and null), so live hearing uses whatever Windows calls the default
  capture device. On a desk whose endpoints are virtual -- this one has Oculus and Iriun
  devices -- the ear will hear nothing, and the WAV path is the check to run.

### The runtime tools ship, and two JNI contract tests stopped skipping

`/runtime/` was ignored wholesale, so the operator tools that live beside the data they
interpret were absent from a fresh clone while three tracked documents told readers to run
them (`runtime/deploy_1_30_24.py`, `runtime/fleet_correlation.py`,
`runtime/vis_phase_check.py`). The rule now ignores the *contents* and re-includes
scripts -- `/runtime/*`, `!/runtime/*.py`, `!/runtime/*.ps1`, `runtime/tools/*.py` -- so
logs, dumps, screenshots, mesh tokens and device backups stay out and 21 tools ship.
Everything in `runtime/` that is not a script is still ignored, so nothing new leaks in by
accident. One consequence is that the vision contract tests which read the correlation
tool's own source (`tests/test_vision_night_contract.py`) stop being skippable in a fresh
clone: the file they assert against is now part of the repository.

`tests/android_toolchain.py` asks for the NDK's `llvm-nm` the way the build asks for its
NDK: the environment, then `sdk.dir` in `platforms/android/local.properties` (where Gradle
reads it), then `ANDROID_HOME`/`ANDROID_SDK_ROOT`, then the path this checkout's own CMake
cache recorded, then `PATH`. That unskipped the two JNI contract tests on a machine where
a complete NDK and a built APK were both present but `ANDROID_NDK_HOME` was unset, so the
shipped `.so` symbols are now actually read instead of silently skipped.

### The release pipeline had never once run to completion

Three faults sat in a queue on the publish path, each hidden behind the one in front of
it. Every tag since `v1.30.4` failed, and the releases that did reach PyPI -- 1.30.4,
1.30.5, 1.30.23 -- were uploaded by hand, so nothing ever surfaced the reason.

- **The PyPI trusted publisher named the wrong workflow.** It was registered against
  `ci.yml`; publishing happens in `release.yml`. PyPI said so plainly --
  `invalid-publisher: valid token, but no corresponding publisher` -- which is the server
  reporting that authentication *succeeded* and only the matching record is missing. And
  `ci.yml` has no publish step at all, so that registration could never have fired.
- **The job asked for provenance it had not been granted.** `attestations: true` needs
  `attestations: write`, and the block carried only `id-token: write`. Authenticating by
  OIDC and writing the Sigstore attestation are two different permissions, and the step is
  refused at the second.
- **The publish step tripped over its own evidence.** `sigstore/gh-action-sigstore-python`
  writes each bundle beside the file it signed, so `dist/` held
  `shugocore-*.whl.sigstore.json` -- and `pypa/gh-action-pypi-publish` `twine check`s every
  file in that directory, rejecting anything that is not a distribution
  (`InvalidDistribution: Unknown distribution format`). The SBOM was written there for the
  same reason and would have failed one file later. `dist/` now holds distributions and
  nothing else; the SBOM and the signatures go to `attestations/`, which is what the GitHub
  release is fed from.

A fourth fault was cosmetic until it mattered: the release step's glob was `*.sigstore`,
which cannot match `.sigstore.json`, so the signatures were dropped even by a run that
succeeded. The release for `v1.30.24` carries them.

The published wheel answers
`https://pypi.org/integrity/shugocore/1.30.24/shugocore-1.30.24-py3-none-any.whl/provenance`
with a publisher reading `kind=GitHub, repository=SamurAI-Official/ShugoCore,
workflow=release.yml`: the artifact naming the workflow that built it, which is exactly the
claim that had been failing to match.
### The suite was green only where it was run

Nine tests failed on CI for four reasons, none of which was the code they were about: the
suite had grown dependencies on this bench. Each is now checked rather than assumed.

- **`cryptography` was never declared anywhere.** The actuation sandbox generates a real
  certificate for its TLS target, and its own docstring claimed the library "ships with
  this fleet (it is a dependency of the memory stack)" -- which was untrue: no declared
  dependency carried it, and no other module imports it. Two tests errored on CI for a
  missing extra. It is `shugocore[sandbox]` now, and in `dev` (which is what CI installs),
  and the failure names the extra instead of surfacing as a bare `ImportError`.
- **Five persona tests needed the Mac to be awake.** They passed only because this LAN
  answers at `192.168.1.162:11434`. Two failed because the endpoint was asked which model
  to use -- a network call -- so the error the test had injected was never reached; and two
  more *passed* without ever reaching the poster they were built around, so away from this
  LAN they asserted nothing at all. The reachability probe is injectable now
  (`_resolve_persona(connect=...)`, the same seam `CapabilityMap` already accepted) and the
  model is named. The suite is proven network-free rather than assumed to be:
  `runtime/tools/persona_offline_proof.py` re-runs it with every outbound connection
  refused -- 26 tests, 0 attempts.
- **"Not fetched" was reported as "drifted".** `submodule_state` folded an unfetched
  llama.cpp submodule into `agree: False`, so the check on a runner read as a node out of
  step with its own pin. Absent and mismatched are separate findings now, in the state and
  in the checker's own output (`not fetched`, not `checkout != pin`).
- **A pytest-only file could not be skipped under `unittest`.** `pytest.importorskip` at
  module scope raises while the module is being *imported*, which `unittest discover`
  reports as an error rather than a skip. Naming unittest's own exception skips cleanly
  under both runners.

That last one led to a finding worth more than the fix: `unittest discover` collects
`TestCase` subclasses only, so the two pytest-style files in `tests/` had never run on CI at
all -- 27 contract tests in `test_sound_provider_contract.py` among them. CI runs them
explicitly now, with `-p no:unittest` so the step stays complementary rather than becoming a
second run of the whole suite.

Found while fixing the above, and fixed with it: `test_memory_lifecycle` asserted that
*every* `.db` under the working directory was renamable, which swept in the actuation
sandbox's live databases and failed on Windows for another suite's files. It went unseen
because `os.replace` always succeeds on Linux, and because pytest's conftest chdirs each
test out of the repository -- the same "green only where it was run" shape.

Fixing those four unblocked a fifth that had been invisible behind them. On Python 3.13 the
sandbox's TLS target failed with `SSL: CERTIFICATE_VERIFY_FAILED ... Missing Authority Key
Identifier`. The sandbox builds its own CA and leaf certificate so its loopback target can
be *trusted* rather than have verification switched off, and they carried the minimum -- a
SAN and `basicConstraints`. OpenSSL 3.x requires the leaf to name the key id of the issuer,
and refuses the chain without it; OpenSSL 1.1.1 (Python 3.9-3.12 on CI, and this bench)
does not. The CA publishes a `subjectKeyIdentifier` now, the leaf names it back in an
`authorityKeyIdentifier`, and both carry `keyUsage` with `extendedKeyUsage: serverAuth`.
Being invisible to the local OpenSSL is exactly why it is asserted directly:
`test_the_certificate_chain_names_its_issuer` reads the profile back off the generated PEM,
which holds on any OpenSSL.
### The headset, actually worn: Quest Link gives the desktop a real session

`mesh.quest3` had been unproven because the headset went to sleep before it could be probed.
With it awake, attached and worn -- streamed over Quest Link -- the probe ran end to end from
the headset's own address, and the same desk produced a second, larger correction.

- **`mesh.quest3` is proven.** The headset (`192.168.1.151`) reached this desktop
  (`192.168.1.152`), `POST /api/v1/task` answered 200, the same request without the token
  answered 401, and the agent's decision is quoted from the *server's* log rather than from
  either side's word for it: `speak`, with real words. What it decided and what became of it
  are both recorded, and they are not the same thing -- `Execution result: no speech provider
  registered for 'speak'`. The delivery gap is now reproducible from the headset on demand.
- **The desktop reaches a real OpenXR session, which the docs said it could not.** The README
  asserted "the desktop's own Oculus runtime is not usable from Godot". Over Quest Link the
  runtime is Oculus 1.208.0 and the session reaches `XR_SESSION_STATE_FOCUSED`, with
  `XRBootstrap` adopting it -- `presence = xr`, 3 runs of 3, exit 0, rendered to the headset.
  Checked while it could still be checked, and corrected.
- **The presence verdict was a race, and it under-reported.** Autoloads run before the session
  reaches READY, so `xr_bootstrap` concluded `desktop_preview` once and never revisited it:
  no headset reported to an operator wearing one. It now watches over a bounded window
  (`SETTLE_FRAMES`), prints what it observed, and `world_session.gd` reports the mode when it
  settles instead of reading it once at `_ready()`.
- **The renderer had to be measured, not assumed.** Forward+/Vulkan removes the runtime's
  "OpenGL 3.3 below the required 4.0" warning and then makes the runtime refuse the session
  outright (`XR_ERROR_GRAPHICS_REQUIREMENTS_CALL_MISSING`); `gl_compatibility` is the one that
  streams. `project.godot` keeps it, with the reason written beside the setting so the obvious
  "fix" is not retried.
- **Two limits found by doing it.** Passthrough is not granted over Link -- only
  `XR_ENVIRONMENT_BLEND_MODE_OPAQUE` is offered and the Meta runtime logs
  `IsDeviceUsingLegacyPassthrough` failing -- and the engine's *shutdown* crashes when a live
  session is active (`signal 11`, leaked GLES3 textures and OpenXR objects) where the same
  scene driven by `--quit-after` exits 0. The transcript now reports
  `mode=not observed before the session ended` rather than presenting the bootstrap's initial
  placeholder as an observation.

### Two things wearing the headset showed that the logs did not

Both were found with the headset on, and both were the scaffold reporting the wrong thing
about itself rather than a missing feature.

- **`start_passthrough()` raised a `SCRIPT ERROR` on every session.** The
  `OpenXRFbPassthroughExtension` singleton exists on the Meta runtime over Quest Link and
  does not expose the call, so the scaffold produced a crash where the truth was "this
  runtime cannot start passthrough" -- logged loudly enough to be a bug report and quietly
  enough that the scene carried on looking fine. The guard now precedes the call, and the
  absence is reported as the fact it is. (What a wearer sees behind the scene over Link is
  the Link environment, not an app passthrough layer.)
- **The background was made transparent without asking the runtime.** Measured:
  `XRInterface.environment_blend_mode` reports `alpha_blend` — the value Godot set from
  `xr/openxr/environment_blend_mode` — while the runtime's own enumeration offers only
  `XR_ENVIRONMENT_BLEND_MODE_OPAQUE`. The two disagree, so neither is trusted: the granted
  mode is read back and printed alongside the decision. Transparency is now a named setting
  (`transparent_background`, env `SHUGOCORE_XR_TRANSPARENT`, default true) instead of a
  fixed assumption, which is what makes the black desktop mirror window explicable — a
  mirror has nothing behind it to blend alpha against, while the headset composites the
  scene over the Link environment. Set it false for an opaque background in both places.

Both are locked from Python (`tests/test_quest_build.py`), since a source-level regression
is what let the passthrough error live through four release runs.

### The surface can be worn, and a headless engine cannot hold a session

- **`xr_session.py --interactive`** starts the scaffold against a local agent with no
  `--headless`, no frame budget and no auto-quit, writing its own log
  (`runtime/evidence/world.xr.interactive.log`) so an operator session never overwrites the
  scripted transcript the claims matrix reads. "It only ran for a few moments" was a
  `--quit-after` of a few seconds, plus a world session that exits as soon as its first
  exchange completes. Measured with a headset worn: `[PRESENCE] mode=xr`, still up after 45s.
- **A headless engine crashes with a live Link session** — 3 runs of 3, access violation,
  before the engine printed anything — where the same scene with a display reaches
  `XR_SESSION_STATE_FOCUSED`. The scripted path retries once on a display surface and says so
  in the transcript, rather than reporting a crash as a session that never answered. That is
  why `world.xr` now records `presence=xr`, a real session, instead of `desktop_preview`.
- **A transcript must come from this run.** A crashed surface left the previous run's log on
  disk and the harness reported it as this run's evidence — a transcript twenty minutes old,
  carrying a presence mode that run never observed. A log older than the run is ignored now.
- **Presence is reported when it settles**, and when it does not settle the transcript says
  so instead of printing the bootstrap's initial placeholder as though it were an
  observation.

- **A skipped poll no longer logs an error.** `poll_status` — and every other request —
  tested the return value for `ERR_BUSY`, which is too late: Godot has already printed
  `HTTPRequest is processing a request. Wait for completion or cancel it…`. Measured in a
  50-second operator session: 17 of them, roughly every third second, for polls that were
  skipped on purpose — which reads exactly like a crash in the console and is a normal skip.
  Requests go through an in-flight guard now, so the skip is silent (17 error lines → 0, 87
  stderr lines → 2). The interactive run also reports what its exit code means
  (`crashed with an access violation (0xC0000005)…`, `stopped by you`, `was terminated`)
  instead of printing a raw number that tells an operator nothing.

## [1.30.24] - 2026-09-28 — audio perception, in layers

`sound/` is the contract layer for hearing, which is the NRR pattern applied to audio: a
window descriptor in, a summary plus labels out, and no path that can carry samples. That
is the same rule as "descriptors, never pixels" — and for audio it is also the privacy
rule.

- **Tier 0 — describe, without a model.** `sound.descriptors` turns per-frame RMS (which
  the device's energy VAD already computes) into a level (quiet/conversational/loud), a
  trend, an activity ratio, an onset count, the longest silence, and a one-sentence
  `describe()`. Free, licence-free, and it replaces a boolean with a description.
- **Tier 1 — speech or not, from a model.** Silero VAD (MIT, 2.2 MB, ONNX) verified here:
  0.005 mean on 1 s of silence, 0.014 on noise, **0.813 mean / 1.000 max on real speech**.
  Its contract is pinned in `sound.schema` because it is easy to get wrong: a 512-sample
  chunk at 16 kHz *prepended with a 64-sample context* (576 in), and an RNN state of
  `[2, 1, 128]` carried across chunks. Feeding 512 without the context reads as junk
  (0.001) — precisely the bug that would have been invisible on a device.
- **Tier 2 — what the sound was.** YAMNet (MediaPipe float32 bundle, 3.9 MB) takes a raw
  15600-sample window (0.975 s at 16 kHz) and returns 521 AudioSet scores, computing its
  own log-mel features internally, so there is no frontend to write. Verified: on
  synthesized speech it returns `Speech 0.984, Speech synthesizer 0.148`, which also
  confirms the class-map order (index 0 = Speech).
- **A label is never a fact.** AudioSet labels are weak (balanced mAP 0.306), so labels
  are capped in number, floored in confidence, and each carries `source: yamnet`. And a
  classifier can never author a transcript: speech keeps its own observation type and its
  own words.
- **Fail-closed and honest.** A worker with no backend advertises *nothing* in
  `compute_caps` and answers `not_supported` with a reason, because "the device did not
  hear anything" and "the device cannot hear" are different facts.
- **The microphone has one owner, and the contract says which.** Android gives audio to one
  capture at a time, and the device's `AudioProvider` keeps an on-device speech recognizer
  holding the mic continuously — a sound classifier opening a second stream would read
  *silence*, not an error. So ownership is explicit: `sound.schema` names the states
  (`available`, `busy_speech`, `denied`, `absent`) and the modes (`sound`, `speech`, `off`),
  a busy microphone answers `not_supported` **naming the owner** instead of a fabricated
  "quiet", and the workload is withheld from the capability manifest while another consumer
  holds the mic. `sound` and `speech` are modes an operator chooses between, not concurrent
  consumers.
- **YAMNet ships as ONNX, so the phone runs one inference runtime.** The TFLite bundle
  cannot be converted directly — its graph uses a `GATHER` that `tflite2onnx` does not
  implement — so the canonical Keras weights are converted with `tf2onnx` and int8-quantized
  with ONNX Runtime, the runtime the APK already links. `scripts/convert_yamnet_to_onnx.py`
  is the recipe: 16 MB fp32 → **4.9 MB int8**, a flat `[15600]` waveform in, and **521
  scores plus the 1024-d embedding** out (the embedding the MediaPipe bundle does not
  expose, so Tier 3's novelty fingerprint comes free). Verified against the reference bundle
  rather than assumed: both return `Speech` as the top class (TFLite 0.984, ONNX 0.993), and
  the recipe prints the shipped artifact's sha256. Measured caveat, stated in the script's
  own output: the two builds' scores differ by up to 0.13 because they compute mel features
  differently, so thresholds get calibrated against the build that ships.
- **The bridge exists, and it ships.** `sound_jni.cpp` (+ `SoundBridge.kt`) runs both models
  on the ONNX Runtime the APK already links: `nativeCreate` builds one session per model,
  `nativeSpeechProbability` takes exactly **512** new samples and keeps Silero's RNN state
  and 64-sample context inside the native layer, and `nativeAnalyze` returns top-5 class
  *indices* plus an optional 1024-d embedding — the label table stays in Python, so this
  layer cannot reorder 521 classes by accident. A session with only one model still loads and
  says which, because a node that cannot classify must not look like a node that heard
  nothing. Verified by building the APK: `libsound_jni.so` (44 KB) for both ABIs, the three
  model assets packaged (6.9 MB), and `tests/test_sound_jni_contract.py` reading the shipped
  `.so` to confirm all six JNI entry points are exported — plus a check that Kotlin, C++ and
  Python agree on 512 / 15600 / 16000, since that model contract is now stated in three
  places and only one of them was previously tested.
- **Bus contract.** `HumanObservation` gains `type="sound"`, and `observation_payload()`
  flattens a result to seven keys, inside the bus's 12-key / 160-char budget.
- **Licences recorded, not assumed.** `MODELS.md` gives every model its licence and a
  ship/no-ship decision, and a guard now fails the suite if a shipped row has no licence, if
  any row still carries a "confirm" note, or if the fetch script downloads a model the table
  does not list. YAMNet is Apache-2.0 (read on the Kaggle model page by hand — the page
  serves a JS shell, so the record names its verifier), Silero VAD is MIT, and the AudioSet
  class map is CC BY 4.0 with attribution. `THIRD_PARTY.md` is the attribution surface —
  which the repository did not have before; nothing in this stack is NonCommercial or
  ShareAlike, and Apache-2.0 imposes no conditions on the labels produced.

Models are fetched and never committed (`scripts/fetch_audio_models.py` with pinned
hashes; `MODELS.md` records every licence). The device provider and the native JNI bridge
that runs these two models on a phone are the next step — the contracts above are what
they get built against, and both were verified against the real weights rather than
assumed.

## [1.30.23] - 2026-09-27 — the first published release since 1.30.5

This release carries every change since the 1.30.5 wheel: the hive's capability map and
placement plane, the persona phrasing service, fleet onboarding, capability baselines,
memory-durability and proposal-backoff fixes. Publishing it means the package on PyPI
finally matches the tree the fleet has been running — the module list had stopped at
1.30.5, so `capabilities.py`, `persona.py`, `mesh_model_host.py` and the rest were in the
repository and absent from anything installed. The operator tools now ship as console
scripts: `shugocore-capability-matrix`, `shugocore-claim-matrix`, `shugocore-onboard` and
`shugocore-actuation-sandbox`.

### The manifest is guarded now, because the release caught it (v1.30.23)

`pyproject.toml` enumerates its modules by hand. That is what makes a wheel predictable,
and what made it silently wrong: the list had stopped at 1.30.5, so thirteen modules were
in the repository and absent from anything installed. The packaging twin of the Android
tree-sync guard now fails the suite when a root module is neither declared nor excluded
with a stated reason, when a declaration names a module that no longer exists, when a
package with an `__init__.py` is undeclared, when a console script points at a module or
function that is not shipped, or when the CHANGELOG has no section for the version being
released (`tests/test_packaging_manifest.py`). Drift was proven to fail it before the
guard was kept: removing `persona` from the list fails the suite with `['persona']`.

### Deployed to the fleet, and the baseline says nothing was lost (v1.30.23)

The same version went to the phones as an in-place upgrade through the gated
`fleet_deploy` path — allowlist, digest match, audit trail — rather than a bare
`adb install`, because an uninstall would take a device's memory and models with it.
Three phones (Tab S9 FE / SM-X518U, A51 5G / SM-S515DL, A16 / SM-A166U) moved from
versionCode 36 to 37 and from 1.30.22 to 1.30.23, each reporting
`Streamed Install Success`.

The capability matrix then compared the fleet against the baseline taken *before* the
upgrade: **no regressions**, the A16's three pre-existing gaps reported as still failing
rather than charged to this deploy, and the A51 — which was not attached when that
baseline was taken — reported as a node with no baseline entry rather than as damage.
That comparison exiting 0 is the claim "the upgrade preserved everything" being checked
against the devices themselves.

Building it needed three environment fixes, now in the README: `JAVA_HOME` pointing at
the JDK's real directory, a `python3.exe` beside its own `python313.dll` for Chaquopy
(the `chaquopy.buildPython` key in `local.properties` is ignored, and a launcher without
the DLLs fails as `STATUS_DLL_NOT_FOUND`), and the fetched ONNX Runtime headers the
native build requires.

### NRR is wired end to end, and now guarded across its three languages (v1.30.23)

The port notes listed the JNI library, the Kotlin bridge, the shipped model asset and the
capability advertisement as future work. All four are in place, so the document now says
so and keeps only NNAPI and the upstream bug reports open.

Verified on a device rather than by reading the code: `nrr_probe`, cross-compiled from the
same CMake sources with NDK 27 and pushed to a Galaxy A51 5G, reports
`onnxruntime: true, active_backend: CPU, device_created: true, model_loaded: true,
execute_frame: true, output_distinct_bytes: 17, failures: 0, ok: true` — a real transform
rather than a passthrough, on the CPU execution provider, with `supports_nnapi` reported
honestly as `false`. The 1.30.23 APK carries `libnrr_jni.so`, `libonnxruntime.so` (19 MB),
`libomp.so`, and `assets/nrr/nrr_upscaler_v0.1.onnx`.

The integration's weak point was that it spans three languages which agree only by *name*,
and every one of those names fails at runtime: a renamed JNI export is an
`UnsatisfiedLinkError` the first time a device renders, and a renamed bridge method is a
Python `AttributeError` that the worker's fail-closed path swallows — the fleet would
simply never advertise `nrr_render`, with no error anywhere. `tests/test_nrr_jni_contract.py`
now holds those names together (Kotlin's `external fun`s against the JNI exports including
their arity, the bridge methods Python calls against the Kotlin adapter,
`loadLibrary("nrr_jni")` against the CMake target) and, when an APK exists, reads the
symbols back out of the packaged `.so` — the only check that can see a stale or stripped
build. Renaming one export fails three assertions, naming it exactly; renaming nothing
passes.

### A proposer that keeps failing stops being asked (v1.30.23)

The rule fallback already existed for a model that never proposes an executable action,
but it did not stop *calling* that model: every cycle paid the round-trip again, and the
failure counter climbed whether or not the model was the reason. Three consecutive
unusable proposals now open a backoff window that doubles with the streak and is capped
(30s → 600s), during which the proposer is not called at all. The skip is reported as
`reason: proposer_backoff` with the remaining wait, never as `model_fallback`, so the
status cannot read as "the model produced garbage" when it was never asked — and the
streak stops growing while we are the ones not asking.

### `fleet_onboard.py`: onboarding as a deliberate act (v1.30.23)

A node needs a shared secret and a peer list, both device-local, and both easy to get
wrong in ways the node cannot see. The new tool writes them idempotently: an existing
secret is reused rather than replaced (supplying a different one is an error unless
`--force-token` means it, since that silently forks the fleet), peers are merged rather
than clobbered, malformed entries are reported instead of dropped, and `--check` reports
a node's state while writing nothing. `--check` on `runtime/desktop` still lists
`shugo-a51`, a phone that was away from the mesh when the check ran and attached again
later — which is why onboarding merges peer entries instead of pruning them: a device
that is merely absent is not a device to forget.

### Baselines: what a deploy took away (v1.30.23)

`capability_matrix.py` can now record a run as a baseline (`--save-baseline`) and compare a
later run against it (`--baseline`), so "the upgrade preserves your memory" is checked
against the device on both sides of the upgrade instead of being asserted. The comparison
charges a run only for what it changed: a claim the baseline never knew about is new
knowledge rather than damage (adding a node cannot look like that node losing its memory),
and a claim that was already failing is reported as *still failing* rather than blamed on
the deploy. Exit status is 1 on a regression and 0 when nothing regressed, so it doubles as
a post-deploy gate. The first real baseline — hub, Tab S9 FE, A16 — is saved at
`runtime/capability_baseline.json`.

### Memory durability: a drained batch is not yet durable (v1.30.23)

Tier 1 truncated its write-ahead journal at `drain()` time — before consolidation had
stored anything in Tier 2 — so a crash in that window lost exactly the episodes the journal
exists to guard, and the loss was invisible: the truncate is what makes a 0-byte journal
look like normal operation. A drained batch is now marked in flight and its copy is
released only when consolidation acknowledges it. Compaction rewrites through a temporary
file with `os.replace` rather than truncating, because a plain truncate at acknowledgment
time would also drop events recorded since the drain. Replaying an acknowledged batch is
harmless: Tier 2 ignores content it already holds.

### A capability says where a service is, not which model to ask (v1.30.23)

`--persona-url auto` resolved the Mac's phrasing service and the phrasing request then came
back HTTP 404: the advertised capability carries a locator, and nothing carried the model
name, so a placeholder was being sent to a healthy endpoint. An unnamed model now asks the
endpoint once (`/v1/models`, then Ollama's `/api/tags`) and remembers the answer, which is
what makes auto-resolution configuration-free; naming a model still skips the question.

### An operator's line is phrased like any other, and late is still in time (v1.30.23)

`--say` bypassed the phrasing step entirely, so the one line an operator explicitly asked
the hive to speak was the one line that went out unphrased. An operator line is now a
speech decision like any other path, and getting there surfaced the same late-arrival race
the service URL had, twice:

- The character text was attached only when the shaper was already usable at *startup*, so a
  shaper enabled by the fleet resolution would have phrased with no personality at all.
  Attaching it is now idempotent and happens the moment the shaper becomes usable.
- `--say` ran before resolution and spoke the unphrased draft. It now waits, bounded (30 s),
  and says so when it gives up — gated on `auto`, so a shaper that is deliberately off is
  not waited for, and the status records that choice rather than a timeout.

### A locator is worthless until a peer can dial it (v1.30.23)

The capability map was advertising addresses that meant something only to the node they
came from. A node configured for `127.0.0.1` published its own loopback, so a peer adopting
that locator dialled itself; and `persona` was defined as "I have a client endpoint
configured", which points *elsewhere*, so the Mac (which serves the model) claimed nothing
while a phone could appear to offer phrasing. Locators are now rewritten to the address
peers reach the node on and dropped when that cannot be learned, `persona` means "I serve a
model whose backend is my own loopback", and the consumer rewrites a peer's "here" to the
address it is dialling. Fixing that surfaced a classic: `text.partition(":")` splits at the
*scheme's* colon, so `http://127.0.0.1:11434` became `…:9000//127.0.0.1:11434`. The
substitution now works on the scheme-stripped address, and the test asserts both the exact
form and the address the connect used.

### The hive resolves its own services (v1.30.22)

`capabilities.py` answers the question the other two planes do not: which node *offers a
service* the hive can use, and where to reach it. Authority is the election's, capacity is
the model host's; this is placement.

- A short, bounded vocabulary (`reasoning`, `persona`, `perception`, `capacity`), each node
  advertising what it **actually runs** with a locator when there is an address to give,
  carried in the heartbeat every node already reads and sanitised at the consumer.
- **Advertised is not usable**: a locator that does not answer is reported unreachable and
  never offered. **"Nobody" needs a reason**: a peer that stayed silent and a fleet that
  never claimed the capability would otherwise look identical.
- First consumer: `--persona-url auto` resolves the phrasing service from the fleet rather
  than naming a node, re-checked every `--persona-recheck` (60 s) because a service that
  appears a moment after this node boots is the normal case -- a service found once is kept
  even if it later goes quiet, and the draft is spoken either way.
- The status line reports `placement=reasoning@…,persona@…`, and the phones' own
  advertisement says they offer `capacity` and `perception` and **not** `reasoning`, which
  is the follower posture stated as a fact rather than a configuration.

### The persona model phrases; the PC decides and gates (v1.30.21)

The two-stage description this system already carried -- *the primary decides what to say,
the nearest device says it* -- is now three-stage: the primary decides **what**, a model on
another node decides **how**, and the gate still runs on the words that will actually be
spoken.

- **`persona.py`** shapes a line of speech through an OpenAI-compatible endpoint, handed
  only what it needs: the primary's personality text and the governor's verdict, never the
  reasoning trace. `--persona-url` / `--persona-model` / `--persona-timeout` configure it;
  unset, nothing changes.
- **It runs before the gate, and only on speech.** `_apply_persona` sits at the end of the
  personality pass in `decision_engine`, where the codebase already states that *"the
  safety gate runs on the returned decision afterward -- personality never pre-approves"*.
  Tool actions and the advisory tool path are never shaped, so a second model cannot reach
  anything but a spoken line.
- **Fail-open on style, never on safety.** No endpoint, a timeout, an empty completion, a
  shaper that raises -- every one returns the primary's own draft with the reason recorded.
  A hive that cannot reach the node that phrases still speaks.
- **Both drafts are kept**: the decision carries `persona: {source, label, draft, reason}`
  and the event is journalled, so "what the persona changed" is answerable afterwards.
- The status line reports `persona=<model>@<host>` or `persona=off`, and the trait vector
  stays on the node that owns the agent: the phrasing node is replaceable.

### Parallelism and the KV cache survive the split (v1.30.20)

An operator's own llama.cpp flags had no way in, so anything beyond the derived arguments
was lost -- and `--parallel N` in particular changes what a split *costs*: llama.cpp keeps
a layer's cache with the layer, and a second slot doubles that cache. Four things now hold:

- **`--model-host-arg`** (repeatable) passes extra llama.cpp arguments, appended after the
  derived ones so they win, recorded in the audit, and replayed verbatim on every
  rebalance. The flags that decide the budget (`--parallel`/`-np`, `-nkvo`, `-ctk`/`-ctv`,
  `-c`) are *read*, not merely forwarded, so a plan describes what will actually run.
- **A cache-aware budget.** `plan_layer_split` charges each device weights *plus* the cache
  for the layers it holds, folded into a single per-layer cost -- a device holding fewer
  layers also holds less cache, so the two are proportional and solving them together is
  what stops a phone being told it has room for layers whose cache it cannot hold.
  `--parallel 4` now costs four times the cache rather than four times nothing.
- **The model states its own geometry.** `gguf_meta` reads `block_count`,
  `attention.head_count_kv` and `embedding_length` from the GGUF header -- a few hundred
  bytes of I/O, because the point of the split is that this node may not have room to load
  the file. `--model-host-layers 0` (the new default) means "ask the model", so the
  hard-coded 24 is gone. Validated against the real model on this bench: 24 layers, 2 KV
  heads against 14 query heads, 512 B of cache per token per layer.
- **`-nkvo` is named, not silently absorbed.** With layers remote it means the cache stays
  on the host, so the split saves weights only: that is logged, audited, and shown in the
  status line as `kv=host`.

### P1.2: the split is re-planned, the hot device detaches, the model is watched (v1.30.19)

P1.1 decided the split once, at boot, and lived with that decision for the life of the
process. The first live cross-device run showed what that costs: a phone heartbeated
**one second** after the settle budget expired, and its capacity was lost until the
hub was restarted. `reconcile()` now runs on a cadence (`--model-host-reconcile`, 60 s
by default):

- **A device that arrives gets offered layers**, and one that leaves or goes thermally
  critical gives its layers back -- the released layers go to whoever can take them, or
  home to the host. A change must persist across two checks and rebalances are at most
  5 minutes apart, so a phone that blips over Wi-Fi does not cost a model reload.
- **`THERMAL_REFUSE_STATUS` is now a runtime detach, not only a planning rule.** A
  device that becomes critical *while holding layers* is detached, and it is then
  **pinned** out of the plan for 5 minutes: a device that has just shed layers must not
  be handed them straight back because its next advertisement still looks cool.
- **The host model is watched.** Liveness is asked fresh each check rather than trusted
  from the launch verdict -- a peripheral that dies mid-session takes the model's
  ability to serve with it, and a stopped process says nothing about that. If it is not
  serving, it is restarted; each further failure gives up one device's layers, weakest
  headroom first, until the model runs locally. After three attempts it reports
  `failed` and stops trying rather than looping, keeping the reason the last attempt
  actually failed for.
- **A stopped host model is no longer orphaned.** The desktop agent now stops the model
  host on shutdown, which is where the seven stale `llama-server` processes on this
  bench came from.

`reconcile()` plans, so it also cannot regress what P1.1 verified: a healthy split is
left alone, and a relaunch keeps the same fail-closed path (reachability by real
connect, argv, health, probe).

### Layer-split planning: fresh headroom, a fleet-sized settle, and the RPC port (v1.30.19)

The first live cross-device attempt exposed three defects that would each have read
as "the hive has no room" rather than as bugs:

- **The plan waited for one peer, not the fleet.** A booting hub planned against
  whoever had heartbeated first -- on a fresh start that was the Mac alone, which was
  then excluded for being in use, so the model ran locally while three phones were
  still quiet. It now waits until the live set covers the peer map (bounded by
  `--model-host-settle`, and a caller passing a static peer list still never waits).
- **A phone advertised stale memory.** Telemetry is minutes old on a busy node: the
  Tab advertised ~161 MB while `MemAvailable` was 769 MB, so the planner skipped the
  device with the most room in the fleet. The advertisement now measures fresh and
  falls back to telemetry, instead of the reverse.
- **The peer map's port is not the model port.** Endpoints came straight from the
  transport map (port 9000), but llama.cpp must dial the peripheral's RPC port
  (50052). The transport port is open too, so the reachability check passed and the
  offload then failed. Endpoints now keep the transport *host* and substitute the
  RPC port.

`--model-host-exclude` is new too: an operator can keep a device out of the pool (the
machine you are working on should not hold layers merely because it has memory), and
the reason is reported -- `local(skipped shugo-mac:excluded by operator)` -- instead
of being silently skipped.

### Consolidation: the Mac line and this line are one history (v1.30.18)

The Mac's three commits (`ac05b78` desktop host launcher, `5d57648` the 18-commit
merge, `e0863fd` the network bridge's 1.30 spatial/NRR seam) are preserved, and this
tree sits on top of them. Verified rather than assumed: `git diff --name-status
e0863fd HEAD` is exactly this work's ten files, and the diff of
`scripts/desktop_agent.py` contains **only additions** -- no Mac hunk was dropped by
the hand-merge that recovered from the interrupted rebase.

One real defect came out of it. `shugonet_bridge` advertises five types policy does
not define (the spatial/NRR seam) and merges them into `policy`'s sets **in place**
at registration -- deliberately, because `execution_layer` holds a reference to that
very set. The test asserting an exact snapshot of those globals was therefore
order-dependent: it passed when run alone and failed in a full-suite run. It now
asserts the contract (the core types are always present, the bridge's seam sits on
top, and registration teaches policy every bridge action), and `policy.py` documents
that its network sets grow at runtime so nobody freezes them again.

`docs/mac_node.md` marks the Mac-specific portions -- the desktop control plane, the
`vm_stat` free-memory branch, and host-launcher behaviour -- with the runbook and the
rule that shared code must not assume Mac paths, Homebrew prefixes or a
case-insensitive filesystem. A node that cannot measure its memory advertises none,
which makes it ineligible in the election and skipped by the layer planner; that is
what `mem=0` was doing to the Mac.

Known pre-existing failures, unrelated and unchanged: the Windows
`PermissionError` file-lock class, and tests that call the model backend while it is
down (`Model shugocore-local call failed: HTTPError`).

### P1.1: the layer split runs from the runtime (v1.30.18)

`plan_layer_split()` had existed since the layer-split work and was called by
nobody. It has a runtime now: `mesh_model_host.py` plans layers from the election's
*own* live peers, wakes their peripherals over the mesh, launches llama-server with
the resulting `--rpc` / `-dev` / `-ngl` (plus `--tensor-split` when several devices
contribute), then verifies `/health` **and** a one-token completion.
`scripts/desktop_agent.py --model-host <gguf>` turns it on; it is off by default and
fail-closed at every step.

New delegated action **`mesh_rpc`**: the primary asks a device to start its own
peripheral. Only the app can execute a phone's native-library binary, so this
replaces the adb debug broadcast for headless orchestration -- through the device's
own authority gate, returning what it actually did. The Kotlin service now registers
its native library dir with the agent so the device can find its own binary.

Decisions rather than assumptions:

- Reachability is a TCP connect to the peripheral's port, not the peer's word.
- A device that does not answer loses its layers (they stay local) and is reported
  with the reason rather than silently skipped.
- `mmap` defaults *off* when offloading: with it the host keeps the GGUF mapped and
  RSS stays at the local-only figure, hiding the win the split exists for.
- `local` always says why: `model_host=local(skipped shugo-a51:thermal_status=4)`.

Measured end to end with a loopback peripheral standing in for a device: 23/24
layers planned remote, delegated start, `--rpc 127.0.0.1:50052 -dev RPC0 -ngl 23
-c 512 -t 4 --no-mmap`, health and probe both true, host RSS **183 MB** against
**537 MB** local-only.

### Node consistency in one command, and the Mac can measure its memory (v1.30.17)

Consistency on a fleet node has four parts, and every incident so far came from one
of them drifting silently. `scripts/node_consistency.py` reports all four *on the
node itself*: the repo commit and its distance from origin; the llama.cpp pin
versus the checkout; whether the Android bundle still matches the repo's copies;
and whether the layer-split host binaries exist **and** can offload (`--rpc`
present -- a build that compiles and cannot offload is how that went wrong once).
It also reports the node's persisted id and whether it can measure free memory,
because a node that advertises none is ineligible in the election and skipped by
the layer planner: alive, and contributing nothing.

That last check found a live fault: **the Mac advertises `mem=0`**.
`available_memory_bytes()` answered via `os.sysconf` and a Windows `ctypes` path
and had no macOS branch -- macOS does not define `SC_AVPHYS_PAGES`, so every Mac
node advertised no headroom and could never be a candidate.
`mesh_election.macos_available_memory()` now reads `vm_stat` (free + inactive +
speculative pages, page size from `sysctl -n hw.pagesize`), and the Darwin
platform is routed there explicitly.

The same observation showed the Mac advertising `shugo-MacBook` while the peer map
dials `shugo-mac`: still needs its restart to adopt the persisted identity, and the
checker names that difference rather than leaving it to be inferred from a log.

### The fleet builds llama.cpp from the pinned commit (v1.30.16)

The Phase 0 commit silently moved the recorded llama.cpp pin: `git add -A` staged
the gitlink, taking it from `6703d78` -- what the phone's peripheral is built from,
and the commit `docs/layer_split_rpc.md` measured -- to a newer local checkout
`95887577`, 410 commits ahead. A host and a peripheral built from different trees
can disagree about the wire protocol while every local test passes, so the pin is
restored in `88cea0a` and the decision is recorded rather than implied.

Both revisions were checked before choosing, because the question had to be about
parity and not capability: `llama_state_seq_save_file` / `_load_file`,
`llama_memory_clear` / `llama_memory_seq_rm` (what `llama_jni.cpp` actually calls),
`--rpc` and `--slot-save-path` are all present at the pin, and the peripheral's
CMake target is `ggml-rpc-server` at both revisions. Both also measure the same:
host RSS 537 -> 170 MB with all 24 layers on a peripheral holding 377 MB and
**tokens identical** to local greedy decode.

So nothing was gained by the jump and protocol risk was. The Windows host was
rebuilt from the pin, and the loopback tables were re-measured there
(`runtime/evidence/mesh_model_host/loopback_measurement.md`): the bench at the pin
reads local 476MB / 22.15 tok/s -> all 24 layers 445MB / 40.03 tok/s.
`docs/layer_split_rpc.md` now carries the rule: a pin change is explicit -- bump
it, rebuild **both** ends, re-measure, and name the commit in the numbers.

### Phase 0: the layer split builds and is measurable on Windows (v1.30.15)

First step of the approved distributed-inference plan: make the layer-split path
reproducible instead of a shell history, and answer the questions the KV phase
depends on *before* designing it.

- **`scripts/build_llama_rpc.py`** builds the host (`llama-server` +
  `ggml-rpc-server`, static backends) and the arm64 peripheral from the pinned
  submodule. It finds CMake/Ninja (the Android SDK's copies work as host tools)
  and the MSVC environment via `vswhere`, records the checkout, the repo pin and
  the source facts in `runtime/evidence/mesh_model_host/llama_build.txt`, and does
  not call a build good until the server offers `--rpc`.
- **The pinned llama.cpp has the KV APIs Phase 2 needs**
  (`llama_state_seq_save_file` / `_load_file`, `--slot-save-path`), so parking a
  sequence's state on a peripheral is supported rather than a gamble.
- **Measured on Windows over loopback** (`runtime/evidence/mesh_model_host/loopback_measurement.md`):
  with all 24 layers of a 0.5B on a peripheral, host RSS falls **537 -> 170 MB**
  while the peripheral holds **377 MB**, and the tokens are **identical** to local
  greedy decode. With mmap the host keeps the GGUF mapped, so the win is visible
  only with `--no-mmap`.
- **`mesh_rpc.process_alive` / `process_rss_mb`**: the benchmark asked "is the
  peripheral still there?" with `ps -p`, which does not exist on Windows -- it
  raised instead of reporting. Portable now, so the bench runs here.

Four traps, each found by running the thing rather than reading it: shared/dynamic
ggml backends produce a server that cannot offload (static now);
`--list-devices` is not a working-build check for a static build; `cmd /c "call …"`
cannot carry a quoted path and `set` wraps `PATH` away; and `LNK1104` on
`bin\ggml-rpc-server.exe` means an old peripheral process still holds the file.

### A failed delegated action says why (v1.30.14)

The A51 accepted the primary's delegated `speak` and then reported `error` with
`delivered=None` and no reason: `_execute_speak` put the cause in `message`, the
delegation handler forwarded only `reason`, and the receiver logged only the
status. A device-side failure was therefore invisible from both ends -- the hub
could see that the hive chose the right mouth and that it did not speak, but not
why.

Every failure on the speech path now carries a machine-readable reason
(`speak_listener_failed: <Exc>: <message>`, `speak_listener_returned_false`), the
delegation handler forwards it (falling back to `message`), and the receiver logs
it through the module logger -- which on Android lands in logcat and on a host in
stderr, both of which outlive the run. A listener that returns false is no longer
reported as a success: the platform held the text and refused it, and saying so is
the whole point of this path.

### A node reports what it can actually perceive, from both doors (v1.30.13)

Measured against the live hive, a phone's own perception never reached its
advertisement, so the router saw three healthy speakers and scored every one of
them `0.00`:

* a phone's **camera** facts land on the interaction bus (`face_count`, the
  attribution verdict), not in telemetry;
* a phone's **microphone** facts land in telemetry under the shell's own
  spellings (`voice_active`, `scene_speech_source`), not the router's;
* the shell pushes no local face/gaze at all -- only its peers' facts.

`_mesh_perception_facts` now reads both doors and maps the platform's spellings,
so an advertisement says what the device actually perceived. A camera refused by
policy no longer makes a phone invisible either: telemetry's voice energy and the
bus's presence are advertised as `remote_presence_present`, a new weak signal
(0.15) that is deliberately below the selection floor on its own -- a person in
the next room is not an operator -- but which, with live voice, makes the phone
in front of them the answer.

### The nearest device can now be found: presence is advertised (v1.30.12)

The routed path -- "the primary decides the words, the device closest to the
operator says them" -- could not choose a phone at all. Two independent gaps, both
found by measuring the live hive rather than reading the code:

1. **No node advertised where it was.** A node's advertisement carried identity,
   priority, thermal state, headroom and `can_speak` -- and nothing about the
   operator. The primary scored every device at `0.00`, so even with the operator
   standing in front of the Tab the honest answer was "nobody reports the operator
   present" and the hive stayed silent. Nodes now advertise what they actually
   perceived (`remote_face_present`, `remote_face_count`,
   `remote_gaze_toward_camera`, `remote_voice_active`, `remote_speech_source`,
   `remote_speech_recent`, `remote_utterance_age_s`) under the same spelling the
   Android shell already streams, so one spelling reaches the router whichever
   transport delivered it. Nothing observed means nothing advertised.
2. **`can_speak` was dead data.** Devices advertised it; nothing read it, so a
   peer with no speaker was treated as a mouth. The receiving node now records it
   on the peer entry and in `_peer_tts`, and the router consults it, which is what
   the routing contract always claimed.

Two smaller corrections in the same path: the age of a peer's facts is now derived
from when the advertisement arrived (so evidence decays even when the peer stamps
no age, and a face seen two minutes ago stops placing a device), and a **routed**
`--say` waits for the observation to list the peers, not just for the election to
settle -- a hub that had just booted had a verdict before its first tick and
answered the first utterance with "no device reports speech output" while the
phones could plainly speak.

### A mesh advertisement is authoritative for the receiver's own verdict (v1.30.11)

Delegated work was dispatched correctly and then refused: the receiver answered
`sender is not the primary`. The cause was structural, not a race. A peer's
advertisement is merged into `telemetry['mesh_peers']` and the election is fed
from that key by `_mesh_heartbeat_tick` -- but on Android the **shell owns that
key** and pushes its own DDS view every tick, replacing what the mesh just merged.
A phone's election could therefore know only its DDS peers (other phones) and
never the host that leads the hive, so it judged that host's delegation against a
primary that was one of its neighbours.

A mesh advertisement now feeds the election **immediately**, in
`_mesh_heartbeat_received`, while the tick's pass still covers the DDS path. The
same message also stopped being silent about the mismatch: a refusal names the
receiver's own view (`mine is <primary>, sender <peer>`), and a refusal that is
merely a lagging lease gets a **bounded retry** (two attempts, six seconds apart)
rather than being reported as a policy failure -- any other refusal stays final.

### One identity per node (v1.30.10)

**The defect.** A node had two names: the mesh id it *self-declared* -- derived
from device capabilities, e.g. `android-Unknown (s5e8835)`, a string with spaces,
parentheses and a SoC token two devices can share -- and the name its peers
*dialled* it by (`shugo-tab`, from their peer maps). Nothing could address a node
by the name it answered to: delegated work and response routing aimed at a name
that was not in `_outbound` ("unknown peer"), and two devices reporting the same
SoC would have collided and shadowed each other in every election.

**The fix.** `node_identity.py` is the single source of truth: a short,
filesystem/CLI-safe id persisted as `<data-dir>/node_id.txt`, adopted on every
start, used as the election node id, the transport agent id **and** the dial name.
Precedence is stored id, then an explicit `mesh_node_id`, then
`shugo-<caps>-<random>` -- the suffix is what makes an automatically named node
unique, because capability strings are not. Both layers now also *detect* a
duplicate: a heartbeat claiming our own identity logs a loud error in the agent and
a warning in the transport instead of being silently ignored.

**Migration.** Each phone's name was pre-written as the name it is already dialled
by (`shugo-tab`, `shugo-a51`, `shugo-a16`), so no peer map changed: identity and
dial name are now the same string by construction, which is what makes a node
addressable by a peer that only knows one of them.

Tests: 10 new -- sanitize (including the old capability form), persistence,
adoption of a stored id, uniqueness across data dirs, an unwritable data dir, and
that the agent actually uses the stored id for the election *and* the transport.

### Unknown headroom is not zero headroom (a Mac could not join the hive)

The Mac joined the mesh and advertised every 10 s, but never appeared as a
candidate. Two reasons, both about treating a *missing* measurement as a
*measured* failure:

- A host that cannot read its own free memory advertised `mem_available_bytes: 0`,
  and the election refuses a candidate reporting no headroom -- so the node
  advertised itself out of its own hive. Unmeasurable memory is now **omitted**
  from the advertisement, and the receiver treats an absent field as *unknown*
  rather than empty. A field that is present and zero still means "measured
  empty" and is still ineligible; a field that is present but unusable ("lots")
  still fails closed.
- The old unit test asserted the opposite (`absence is not fabrication`,
  fail-closed). That rule excluded exactly the node that needed the entry most, so
  the test now states the new one and keeps the two cases that matter (reported
  zero, unusable value) as ineligible.

Incumbency landed with it, because making a second host eligible exposed it: the
ranking is `(priority, node_id)`, so a machine joining on the same priority could
take the lease from a healthy holder on the alphabet alone, re-homing the fleet
mid-flight. A **strictly better** candidate still takes over; an equal-ranked one
no longer does.

With both changes the Mac is a full hive member, and the hub keeps the lease it
already holds.

### Answers come from the device nearest the operator (response routing)

A hive has several mouths but one operator. `speak` and `ask_user` now choose
which device says it: `response_routing.py` scores each device from the
perception facts the fleet already publishes -- camera face, gaze toward the
camera, VAD, speech attribution, how recently the human spoke, and the attention
verdict -- with local and `remote_*` spellings normalised so a peer's facts and
our own score identically. Evidence decays (halved after 15 s, ignored after 45 s),
so a face seen two minutes ago does not mean the operator is standing there now.

- The nearest **speaker** wins: a device that cannot speak is never chosen,
  however close it is, and devices advertise `can_speak` in their heartbeats.
- Ties break deterministically (score, can_speak, election priority, device id),
  so two runs on the same evidence pick the same mouth.
- **Silence is allowed**: with nobody reporting the operator present the agent
  does not guess a device -- it records `nobody reports the operator present` (or
  `no device reports speech output` on a host with no speaker) and stays quiet.
  An operator can still force a device with policy `response_target = <device>`.

The chosen device receives the utterance over the mesh as a **delegated action**
(`orchestrate/delegate`), runs it through its own gate, speaks through its own
speaker and reports the outcome back on `orchestrate/result` (visible in
`get_status()["delegated"]` and `response_routing`). Two authority rules keep this
from becoming a remote-control channel: only the current lease holder may direct
another node, and only `speak`/`ask_user` are delegatable -- everything else is
refused. A follower still cannot speak on its own initiative; it can only be the
primary's voice.

The transport needed one fix to make this real: inbound `send` frames were acked
and then dropped, so a peer could address a node and be heard by nobody.
`set_send_handler()` now delivers them (peer, topic, payload) to the agent.

### Phase E: claim, check, artifact

`claim_matrix.py` turns each claim the docs make into a row -- the check that
decides it, the captured evidence, and a verdict -- and refuses to call anything
proven because it is written down:

| verdict | meaning |
| --- | --- |
| `proven` | every check passed |
| `failed` | a check did not pass |
| `unproven` | a live check could not be evaluated (not the same as failing) |

Checks are either commands (test modules, `actuation_sandbox.py`,
`capability_matrix.py`) or *live* facts parsed from real output by pure functions
(`role=primary`, `imported=N`, a hash-linked chain, "no local model calls on a
subordinate"). Artifacts land in `runtime/evidence/<id>.txt`, so a claim can be
inspected later instead of trusted:

```bash
python3 claim_matrix.py --status-file runtime/device_backups/<host>.err
```

### Top-down orchestration by measured capacity (v1.30.8)

Android nodes kept running the **full** agent loop -- local model proposals,
`ask_user` decisions, decision-journal writes -- while a desktop held the primary
lease. That is both wasteful and wrong: the hive has one orchestrator, and a
phone's job is the work the primary delegates to it.

`_orchestration_mode()` now decides agency every tick from *measured capacity* --
the same thermal/free-memory signals the election ranks on, plus battery level
and whether the device is charging -- together with the election's own verdict:

- `primary` -- holds the lease (or stands alone): full local agency.
- `subordinate` -- a live peer outranks this node (better priority/headroom): it
  keeps observing, heartbeating and serving memory, but does **not** run its own
  model-backed decision loop; its work is what the primary delegates.
- `degraded` -- no headroom of its own (thermal at the refuse threshold, free
  memory under 64 MiB, or battery <= 15% and not charging): steps down even with
  nobody to hand to.
- Operator override through policy: `orchestration`: `auto` | `full` |
  `sensor_only`.

Status carries it (`get_status()["orchestration"]` = mode, reason, capacity
profile), and the mode is logged on change rather than every tick.

Verified live on the real fleet (both phones upgraded in place to 1.30.8): the Tab
and A51 now log **zero** `ANDROID_INFERENCE generate called` and **zero**
`Decision made for task` lines, and their decision journals stop being written --
the Tab's last entry is the old local `ask_user` proposal that came back
`mesh_follower`, which is exactly the behaviour that prompted this. Both keep
heartbeating: the hub still reports `connected=3 mesh_peers=2` with the desktop
holding the lease, so a subordinate stays a first-class hive member.

Also in this change: a refused frame now says *why* -- `mesh token missing (this
node gates every frame; give the sender SHUGOCORE_MESH_TOKEN)` instead of
"malformed" -- and a node that has peers but no token warns at startup that it is
advertising into a wall. That is precisely what the Mac was doing: it is dialled
and advertising every 10 s, and both the hub and the phones were refusing every
frame it sent.

### Phase D: the loopback actuation sandbox (containment, not adjectives)

`actuation_sandbox.py` drives the **real** pipeline -- the engine's own
`_gate_decision` (Tier 3 invariants, SAFE_STATE, external consent, human
approval) and then its own `_execute_gated` (hash-bound policy token) and the
real `ExecutionLayer` -- against a **real** service on 127.0.0.1 that records what
arrived. Every scenario is checked twice: what the engine reported, and whether
any byte reached the wire. A refusal that still hit the target, or a "success"
that never landed, is a FAIL here.

The target serves TLS (self-signed CA trusted through `REQUESTS_CA_BUNDLE`, never
by disabling verification) because the shipped egress policy is https-only. A
plain-HTTP twin exists so a scenario can *show* that rule refusing a local
service even with consent and approval in hand.

All 17 scenarios behave as required on the live host:

| scenario | expect | wire |
| --- | --- | --- |
| loopback refused by the default allowlist | refused | 0 |
| **loopback allowlisted + consent + approval** | **allowed** | **1** (HTTP 200, audited) |
| loopback plain http refused by the scheme rule | refused | 0 |
| no consent / expired consent / revoked consent | refused | 0 |
| approval denied / approval pending | refused | 0 |
| LAN target / internet target | refused | 0 |
| method not allowed | refused | 0 |
| forged verdict / missing verdict | refused | 0 |
| SAFE_STATE (read-only) | refused | 0 |
| mobile actuation topic / unpaired device | refused | 0 |
| mobile sensor topic | allowed | — |

Four operator-relevant properties fell out of writing it, each now pinned by a
scenario: egress is **https-only** (a local HTTP service is unreachable by
policy); `api_call`'s shipped method allowlist is **GET only**, so a POST needs
explicit operator say-so; consent refusals surface as the Tier 3 invariant
`consent_required` *before* the consent registry is consulted; and device topics
require **pairing first**, after which a paired device may still only publish on
the contracted sensor namespace -- actuation topics are unreachable by
construction.

### Capability retention, checked instead of asserted (Phase C)

Every capability this system claims lives somewhere concrete on each node, so
`capability_matrix.py` reads that state and reports per node whether the
capability is still there: Tier 2 memory, the hash-linked audit chain, the
decision and episodic journals, pending timers, learned user facts, the
personality model, the profile marker, local weights, the mesh secret, the
configured peers, and the two build directories v1.30.7 added.

It is read-only and dependency-free (a directory listing is all it needs), so one
classifier serves both a host data dir and an Android app data dir read through
`run-as`:

```bash
python3 capability_matrix.py --host-dir runtime/desktop --host-name hub \
    --phone "Tab S9 FE=<serial>" --phone "A51=<serial>" \
    --token-file runtime/desktop/mesh_token.txt
```

Verdicts are honest about *why* something is absent: `ok`, `empty` (present but
zero bytes -- a failure, not a pass), `missing`, `mismatch` (a secret that is not
the fleet's), `n/a` for claims a node does not carry (a host serves weights from
its backend, and takes peers from CLI/env), and `not-created` for things a host
only writes when it uses them. Only `missing`/`empty`/`mismatch` fail, and a node
whose data dir cannot be read is reported as such rather than as 13 lost
capabilities.

First live run, real devices, after restarts plus **three in-place upgrades**:

| node | result |
| --- | --- |
| Tab S9 FE | 13/13 ok |
| A51 | 13/13 ok |
| desktop hub | 8 ok, 2 not-created, 3 n/a, 0 failing |

That is the "in-place upgrade keeps your memory and models" claim verified against
the devices rather than asserted, and the fixture tests include what a reinstall
looks like (every claim missing) so the check would catch the real failure.

### A restarted peer is visible again (the hive stops flapping)

`MeshElection.observe_heartbeat()` dropped any advertisement whose sequence did
not increase -- and returned `True` as if it had been observed. A peer that
reboots begins its counter again, so a node that remembered a high sequence
stayed **blind to that peer forever**: the phone advertised every 10 s, the hub
counted the frames (`rx` grew) and still reported `role=standalone`, and only
restarting the hub recovered the hive. That is what made the fleet flap after
every redeploy.

The lease is now renewed on an equal sequence (a replayed frame renews the lease
but still cannot rewrite the record's fields) and a *rollback* is treated as a
restart, logged and accepted ("peer … restarted (seq 459 -> 1); renewing its
lease"). Two regression tests were added that **fail against the old logic**, and
the two tests that encoded the old rule were rewritten to state the new one
explicitly rather than deleted.

### The hive ships its own builds (mesh artifact transfer)

Memory already travelled the mesh; a *build* could not leave the machine that
made it without an ADB cable. The ShugoNet transport now carries artifacts:
`artifact_manifest` / `artifact` (chunked, offset-addressed) and `artifact_offer`
(the receiver pulls), behind new surfaces -- `fetch_artifact()`,
`offer_artifact()`, `list_artifacts()`, `artifact_receipts()` -- and the agent
tools `mesh_artifact_fetch` / `mesh_artifact_offer`.

Safety is inherited, not re-invented: every request sits behind the same shared
secret gate; artifacts are addressed by *bare file name* out of one allowlisted
directory per node, with containment re-checked on the real path (so `../..` or a
symlink reaches nothing outside it); one transfer is capped (256 MiB) and chunked
(192 KiB, which still fits one frame after base64); the manifest digest is
verified before a byte is written *and* the assembled file is digested again
before it is moved into place, so a partial transfer never becomes an artifact.
Receipts and `mesh_artifact_received` / `mesh_artifact_offered` /
`mesh_artifact_failed` events land in the node's own audit chain.

Live proof on the real hive, hub out and back: the desktop shared the current
`app-debug.apk` (78,117,673 bytes, sha256 `d4ce2339…`), a plain mesh node pulled
it in 398 verified chunks and the reassembled file was bit-for-bit identical to
the source; the same node then offered a file that the hub pulled into its
staging dir, digest-checked -- `artifacts=1 art_in=1` in the hub's status line,
with the receipt in `audit_chain.jsonl`.

A host joins the channel with `--share-dir` / `--artifact-dir` (defaults
`<data-dir>/shared` and `<data-dir>/artifacts`) or by setting
`SHUGOCORE_ARTIFACT_ROOT` / `SHUGOCORE_ARTIFACT_DIR`, and can ship or receive at
startup with `--artifact-offer NAME=PEER` / `--artifact-fetch NAME=PEER` -- the
same command a laptop or the Mac runs to pick up the current build.

### The duplicate agent: one service process built two of them

The Tab logged two `SHUGONET: runtime started` lines and two tick counters,
every tick failed with "database is locked", and the mesh runtime's second bind
died with EADDRINUSE.

`onStartCommand` is `START_STICKY` and schedules `initializeInference()` on
*every* `startService`, and that function had no "already bootstrapped" guard --
so each call built a second `AndroidAgent` inside the same Chaquopy interpreter:
two tick loops, two SQLite connections, two mesh binds. The activity, the boot
receiver and the UI Start button all reach that path, so it was a matter of
when, not if.

Bootstrap now goes through `maybeInitializeInference()`, which holds
`bootstrapLock` for the whole (slow) bootstrap and returns early when `pyAgent
!= null` -- concurrent starts cannot both pass the check, and a repeat call is a
no-op that repairs `agentRunning` instead of leaving it stale.
`tests/test_android_agent_singleton.py` asserts the invariant against the source
(there is no Kotlin test harness), including that `initializeInference()` keeps
exactly one call site.

### The fleet token, and a heartbeat log that hid a healthy node

Bringing the real four-node hive up surfaced two things the loopback tests could
not:

1. **A Python-only node must present the mesh token.** `_accepts()` refuses any
   frame without one when the runtime was built with an `auth_token`, and the
   phones are token-gated, so the desktop's unstamped advertisements were
   answered with `shugonet refused malformed message` every 10 s while the
   phones stayed followers of *each other*. A host joins the hive by supplying
   the fleet secret through `SHUGOCORE_MESH_TOKEN` or
   `<data-dir>/mesh_token.txt` -- the same precedence `_load_mesh_token()`
   already used on Android.
2. **The advertisement loop's first-cycle-only log lied.** The loop logged
   either its first cycle or nothing, so a node whose peers connected a few
   seconds *after* it booted (exactly how the phones come up) advertised
   perfectly and never said so -- read here as an inert producer. The first
   *successful* advertisement now logs, and an empty first cycle logs once too,
   so "silent" and "not advertising" can be told apart in the fleet logs.

With both fixed the live hive converged: the desktop reports
`role=primary mesh_peers=2 beats=2` and the phones refuse nothing, electing
`primary='shugo-desktop'`. The Mac remains the only node without heartbeats (it
still runs the pre-heartbeat bundle), which is why it is not yet a live peer.

### Track 1 election: heartbeats over the mesh, and why every node said 'standalone'

Every node in the hive honestly reported `role=standalone`. Getting to the bottom
of it took four faults, each invisible on its own.

1. **No producer.** `MeshElection` was fed only by the Android DDS path
   (`ShugoCoreService` -> `update_mesh_peers` -> `telemetry['mesh_peers']`), and
   the ShugoNet TCP mesh carried no heartbeat at all, so a Python-only fleet had
   nothing to compare against. The transport now carries the advertisement:
   `{"type": "heartbeat", "from": ..., "payload": {...}}`, fire-and-forget
   (never acked -- an ack every 10 s per peer is pure noise) and stamped with the
   mesh shared secret when one is configured, because a token-gated peer rejects
   any unstamped frame (i.e. the whole real fleet). New surface:
   `set_heartbeat_provider()`, `set_heartbeat_handler()`, `broadcast_heartbeat()`,
   `heartbeat_snapshot()`, `status()["heartbeat"]`, and a 10 s advertisement loop
   whose first cycle logs whether the beat actually went out.
2. **A zero-headroom trap.** `MeshElection._eligible()` refuses a candidate whose
   `mem_available_bytes` is 0, and a host has no memory telemetry, so *every*
   desktop was "no-headroom" and no primary could ever be elected: the election
   looked inert while it was really disqualifying everyone. New
   `mesh_election.available_memory_bytes()` measures free physical memory
   (POSIX/Android/macOS via `sysconf`, Windows via `GlobalMemoryStatusEx`) and
   `_mesh_mem_headroom()` prefers telemetry but falls back to the measurement.
3. **Silence.** Every diagnostic on this path went through `self.log()`, which
   only fills the in-memory ring buffer the Android LOG tab polls -- on a host
   nothing reached the process log, so a node whose peers never became candidates
   reported `standalone` with no explanation. The transport and the election now
   log through the module logger (first advertisement, first peer merged, verdict
   changes, ineligibility reasons), and the host launcher's status line reports
   `mesh_peers=` (peers declared to the election) and `beats=` (received by the
   transport), so the two ends can be told apart at a glance.
4. **Verdict latency.** Verdicts were evaluated only at the top of a tick, and a
   model-bound tick measured 80-120 s in the lab, so roles lagged the fleet by
   minutes -- long enough for a follower to keep acting. Ingest now re-evaluates
   immediately, and `shugocore_agent` merges every received advertisement into
   `telemetry['mesh_peers']` (bounded, de-duplicated by node id, `source:
   "mesh"`) so `_mesh_heartbeat_tick` stays the single evaluator and the DDS and
   mesh views cannot drift apart.

Verified live: two host nodes in separate processes converged on the same
primary -- the lower-priority node -- with candidates
`['shugo-testlab-b', 'shugo-testlab-a']`, one reporting `primary` and the other
`follower`, and `_mesh_may_act` refusing on the follower.
`tests/test_mesh_heartbeat.py` (20 tests) covers the wire contract, the
provider/handler lifecycle, token stamping, the election outcome on both sides,
thermal and headroom ineligibility, the standalone fallback, immediate
evaluation and the agent wiring without booting an agent.

### Operator consent surface: the missing half of the governance model

The decision engine gates side-effecting, robotics, mobile, network and fleet
actions behind `ConsentRegistry`, and the README is explicit that a grant may
only come from an operator - "a `consent` flag written by the acting agent
itself is never trusted". The *approval* half of that model existed on the wire
(`GET /api/v1/approvals`, `POST /api/v1/approvals/<id>/approve|deny`). The
*consent* half existed nowhere: `ConsentRegistry.grant()` had no caller in the
tree, no HTTP route, and no UI. A device therefore could never be granted
network egress - the A51's audit chain filled with
`policy_block: no external consent grant for 'network_send'` while its mesh
transport was healthy and connected to all three peers - and the same gap would
have blocked agent-driven `fleet_deploy`, which the engine consent-gates.

- **`shugocore_server.py`**: `GET /api/v1/consent` (grants in force, bounded by
  `CONSENT_MAX_GRANTS` and sanitized), `POST /api/v1/consent/<action_type>`
  (grant, with `ttl_seconds` capped at `CONSENT_MAX_TTL_SECONDS` = 24 h and
  `granted_by`/`scope`/`note` recorded) and
  `POST /api/v1/consent/<action_type>/revoke`. Only `policy`'s consent-gated
  families are grantable; an unknown type is refused with the grantable list and
  an unimportable `policy` grants nothing (fail-closed). Bearer auth and rate
  limiting apply exactly as for `/api/v1/approvals`.
- **`security_inventory.py`**: the consent block now reports the registry the
  gate consults. It read `agent.consent_registry`, which the engine never sees
  (`shugocore_agent` builds a `ConsentRegistry` but never passes
  `DecisionEngine(consents=...)`), so operator grants would have been invisible
  in the very pane meant to show them.
- **`clients/desktop/shugocore_desktop.py`**: the SECURITY pane gains an
  "Operator consent (Track 1)" control - action quick-picks, optional TTL,
  Grant / Revoke, and a live "in force" line read from the same registry.
- **`tests/test_consent_surface.py`** (new) - 10 tests over real HTTP: route
  naming (including slash-smuggling), empty-but-enabled listing, unknown-type
  refusal, grant -> engine allows (`has_grant_for_attended_action`) -> revoke ->
  engine refuses with the exact reason seen on the A51, TTL expiry, TTL
  validation and the 24 h cap, `fleet_deploy` grantability, the security
  inventory integration, and bearer-token enforcement.

### Fleet rollout: `fleet_deploy`, consent- and approval-gated ADB installs

Updating the hive was a manual `adb install` with no governance and one
land-mine: a build signed by a key the device does not trust fails with
`INSTALL_FAILED_UPDATE_INCOMPATIBLE`, and the only way forward is an uninstall,
which wipes the node's memory DB and every cached model (3.25 GB of GGUFs on the
Tab S9 FE during this session's re-key). Rolling a build is a side-effecting
action, so it is now modelled as one.

- **`fleet_deploy.py`** (new, host-only) - `FleetDeployHandler` serves
  `fleet_deploy` (install) and `fleet_status` (list attached devices plus the
  build each one runs) over the ADB transport the fleet already uses, including
  the wireless-debugging link. The ADB surface is injected (`AdbRunner` protocol
  + `SubprocessAdbRunner`), so the capability is unit-testable with no device and
  no subprocess. Fail-closed on every axis: no operator allowlist, a non-`.apk`
  artifact, an artifact outside the operator artifact root, a digest that does
  not match the supplied `sha256`, a target outside the allowlist, or more
  targets than the rollout bound all refuse before a device is touched. `dry_run`
  plans a rollout; `expect_version` verifies what the device reports afterwards.
  Every attempt, and every refusal, is appended to the audit chain.
- **`policy.FLEET_ACTION_TYPES` / `FLEET_READ_ACTION_TYPES`** (new) plus the
  `KNOWN_ACTION_TYPES` union; **`decision_engine`** probes the module's presence
  (`_HAS_FLEET`) and consent-gates `fleet_deploy` like the side-effecting class,
  so an operator grant and approval are required before a rollout can execute.
  On Android the import fails by design, so a phone can never propose a deploy it
  has no means to perform. Both mirrored modules stay byte-identical to the
  bundle (`tests/test_android_tree_sync.py`).
- **`scripts/desktop_agent.py`** - `--deploy-target SERIAL` (repeatable, off by
  default), `--deploy-artifact-root`, `--adb`; the launcher logs whether the
  capability is enabled and where APKs may come from.
- **`tests/test_fleet_deploy.py`** (new) - 19 tests: the allowlist, artifact
  root, digest and bound refusals (each asserting adb was never called),
  per-target success/failure/partial, dry-run, `expect_version` mismatch, status
  parsing (including `offline`/`unauthorized` transports), audit wiring, and the
  engine's know-and-gate assertion.

### Fleet re-key hardening: peer-dial fd leak and a race-free liveness gate

Bringing the four-node hive onto a single signing key surfaced two defects that
only appear on a quiet-but-healthy fleet (the Tab S9 FE stayed green while the
A51 was reported down by the smoke gate).

- **`agent_runtime._PeerConnection.connect()`** - a refused dial now closes the
  socket it created. The old path abandoned a live socket to the garbage
  collector, so an unreachable peer cost one leaked fd per reconnect pass and
  filled logcat with `ResourceWarning`s every few seconds (measured on the Tab
  S9 FE with the A51's node down). Regression test:
  `tests/test_agent_runtime.py::TestPeerConnection.test_failed_dial_closes_socket`.
- **`tests/android_device_smoke.py`** - the `service_alive` gate polls for the
  Chaquopy marker and accepts `python.stdout` OR `python.stderr`. Sampling
  `python.stderr` exactly once failed a genuinely healthy A51: the warm-up
  clears logcat (`wait_for_agent_loop`), the device ring buffer is hard-capped
  at 5 MiB (`logcat -G` refuses more), and a node that emits no warnings writes
  `python.stderr` once per decision (~50 s while thermally throttled) - so the
  single sample landed in the cleared window while `Decision made for task` was
  found in the same phase. Both tags are written by the embedded interpreter
  alone, so the claim (Chaquopy is up) is unchanged; only the race is gone.

### Desktop control plane, host-node launcher, and a strict-server json_mode

The desktop side had no control surface: the only ways to run a node were the
Android app and the CLI, so a mixed fleet could not be operated or inspected
from a desktop.

- **`clients/desktop/shugocore_desktop.py`** (new) - a stdlib-only (tkinter)
  control plane that mirrors the Android one: a pinned node-status header
  (node/backend/model/engine/memory/mesh/uptime/ticks) over
  SERVER | AGENT | ACTIVITY | SENSORS | SECURITY | LOG, polled at 1 Hz. The
  SERVER pane is the backend/model chooser: Ollama, LM Studio (or any
  OpenAI-compatible endpoint), llama.cpp `llama-server`, a ShugoCore server
  bridge, or the offline stub, with a live "Detect models" probe
  (`/api/tags` vs `/v1/models`), an endpoint preview, node/mesh fields,
  Start / Apply (restart) / Stop, health rows read from real agent signals, and
  one-click audit-chain verification. `--selftest` probes every backend
  headlessly. Not part of the Android bundle; nothing to mirror.
- **`scripts/desktop_agent.py`** (new) - the headless host node behind it: a
  full agent (engine, Tier 2, policy gates, audit) plus the memory mesh, with
  `--peer/--token/--seed-fact/--sync`, mesh identity/priority, and a status line
  reporting node/prio/role/primary/connected/imported. `mesh_peers.json` (or
  `SHUGOCORE_MESH_PEERS`) still decides the fleet: the launcher logs what was
  configured versus what is connected, so a missing peer is visible rather than
  implied.
- **`model_backends.py`** - `json_mode` for the OpenAI-compatible backend
  (default unchanged). A grammar request used to map to the legacy
  `response_format: {"type": "json_object"}`, which strict servers reject
  (LM Studio: HTTP 400 "'response_format.type' must be 'json_schema' or
  'text'"). The rejection was invisible in the agent loop - it looked like a
  model that never proposed an action - so every decision fell back to rules.
  `json_schema` sends a permissive object schema; `text`/`none` omits the hint
  entirely. Mirrored into the bundled Android tree and pinned by
  `tests/test_model_execution_stress.TestStructuredOutputMode`.
- **`tests/two_agent_memory_smoke.py`** - the harness now presents
  `SHUGOCORE_MESH_TOKEN` when it is set. A token-gated peer rejects every
  message without the secret, so the probe reported "request failed: timed out"
  and a hardened mesh could not be verified at all.
- **`tests/test_memory_sharing.py`** - the async-dial test restored `_dial_peer`
  as a bare function instead of a `staticmethod`, which re-binds `self`; every
  later dial thread in the same process then died with
  `TypeError: _dial_peer() takes 1 positional argument but 2 were given`. The
  suite still reported success because the traceback goes to stderr from the
  thread, not to the test result. Restoring the descriptor keeps the merged
  async dial honest for the rest of the run.
- **`.gitignore`** - ignore `runtime/` (per-node state: memory database, audit
  chain, personality files, local `mesh_peers.json`).

Verified on a four-node LAN mesh (Windows desktop, macOS laptop, two Android
nodes): the desktop held 224 facts imported from its peers with `shared_from`
provenance, and the desktop client drove a node whose decisions came from LM
Studio (`proposal_source` = the model id, zero model-call failures) instead of
the rule fallback.

### Android lifecycle: a race that escaped as an exception from on_resume

`AndroidRuntime.on_pause()` set ``state = "paused"`` and **then** called
``fallbacks.report_violation(...)`` — the call that makes the governor latch
PAUSED. Between those two steps the runtime already looked paused while the
governor was still IDLE, so an ``on_resume()`` running in that gap called the
governor's resume against an IDLE governor and raised

    GovernorError: cannot resume from state=idle

A lifecycle callback crosses from the app shell (Java/Kotlin) into Python, so
an exception there is not a test annoyance — it is an app crash. This is the
same failure the Python 3.9 CI job hit
(`test_android_lifecycle_stress.TestLifecycleChurn.test_concurrent_pause_resume_race`),
and the interleaving is reachable whenever pause/resume are driven from more
than one thread (screen off/on, backgrounding) while the agent loop is running.

- **`android_runtime.py`** — the state transition and the governor latch are now
  one critical section (`self._state_lock`, an RLock). ``on_resume()`` can no
  longer observe "paused" before the latch landed, and a second concurrent
  resume finds ``state == "started"`` and returns instead of double-resuming the
  governor. ``on_create()``/``on_destroy()`` take the same lock so the
  ``state`` field has a single writer discipline; ``on_destroy()`` joins the
  monitor thread *outside* the lock so no thread is ever waited on while
  holding it.
- **`android_runtime.py` — lifecycle callbacks can no longer raise.** If the
  governor refuses a resume (e.g. a terminal HALT), ``on_resume()`` now logs
  and stays **paused** rather than claiming a resume that did not happen; a
  failing pause latch is logged and the pause still completes. An exception
  escaping into the app shell is the crash class we are trying to remove.
- **`tests/test_android_lifecycle_stress.py`** —
  `TestLifecycleChurn::test_resume_during_pause_latch_does_not_escape` makes the
  interleaving deterministic (a deliberately slow ``report_violation`` holds the
  pause un-latched while ``on_resume()`` runs). Verified to fail on the old code
  with the exact `cannot resume from state=idle` error and to pass on the fixed
  code.

### Test determinism: a wall-clock-dependent assertion, and the watermark limit it hid

`test_sync_combines_memory_and_is_usable_by_each_agent` failed on the Python
3.9 CI job asserting `again["received"] == 0` (got `1`). Reproduced to be
**wall-clock dependent, not a regression**: the same code passes or fails
depending on which second the run lands in.

- **The assertion was over-specified.** ``sync`` advances its watermark to the
  newest received ``created_at``; an imported fact is stamped at *import* time
  (`store_fact` sets ``created_at = now``), so agent-a's newly imported fact is
  stamped after agent-b's watermark was taken. Whether the incremental filter
  then re-offers it depends on whether those two moments fell in the same
  wall-clock second. Proof, same code, clock the only difference: facts stamped
  ``…:43`` and ``…:43`` → ``received=0`` (test passes); ``…:43`` and ``…:44`` →
  ``received=1`` (test fails). The test now asserts the real invariants —
  ``status == "success"``, nothing newly imported, the durable store stays at
  two facts — and bounds ``received`` instead of fixing it, so it is
  deterministic in either second.
- **The underlying limit is now pinned, not lurking.**
  `TestSemanticMemorySharingPrimitives::test_same_second_watermark_relationship`
  states it explicitly: `_utc_now_iso()` stamps `created_at` with
  `isoformat(timespec="seconds")` and `facts_since` filters
  `created_at > since`, so facts created in the same second as the watermark
  are filtered out of each other. **Known issue:** an incremental sync can
  therefore never deliver a sibling fact created in the same second as the
  watermark — it arrives only once a later-second fact moves the watermark, or
  on a full re-pull (`since=0`). Fixing that properly means discriminating
  stamps (sub-second precision) or an `(created_at, id)` cursor; both are
  protocol/storage changes and are deliberately **not** made here.

### Mesh: peer refusals surface as failures, and mesh init cannot stall or leak

Three defects made a node look healthy while its mesh was broken, stalled the
app that hosts mesh startup, or leaked a thread per stalled peer.

- **`agent_runtime.py` — `sync()`/`query()` reported a peer refusal as
  success.** A peer that refuses a request (protocol/version mismatch, rejected
  token, unknown verb) answers with an `error` frame. `_one_shot_request`
  returns that frame verbatim, and both callers checked only `if not resp` — an
  `error` frame is a *truthy dict*, so it read as a successful transfer of zero
  facts. A node whose peer was actively rejecting it therefore reported
  `{"status": "success", "received": 0, "imported": 0}` and stayed "connected"
  forever. The new `ShugonetAgentRuntime._peer_error()` recognises both refusal
  shapes (`type: error` and `status: error`, with `message`/`reason`) and
  `sync()` now returns `{"status": "error", "message": <peer reason>}`, while
  `query()` skips the refusal instead of counting it as a reply. `errors` joins
  the runtime's stats counters.
- **`agent_runtime.py` — peer dialing blocked the caller.** `add_peer()` and
  `start()` connected to each peer inline, each costing a full
  `_CONNECT_TIMEOUT` (2s) when the peer was down. Both run on the
  agent/service init path, so a three-peer mesh with two unreachable peers
  stalled init for seconds — an ANR on Android, which the user sees as the app
  dying. Dialing now happens on a short-lived daemon thread (`_dial_async`).
  `send()`/`sync()` still reconnect inline on demand and the reconnect loop
  still retries, so the link is not lost by being lazy.
- **`agent_runtime.py` — the peer server spawned one thread per connection
  without bound.** Each stalled peer pinned a handler thread *and* its fd; the
  handler blocks in the memory backend, so a peer that gives up mid-request
  leaves its thread wedged and its socket in `CLOSE_WAIT`. Observed live: the
  A51 held three sockets in state `08` (`CLOSE_WAIT`) and stopped answering
  mesh requests entirely while its process stayed alive. Unbounded growth
  ends in an fd/memory-exhaustion kill on a small device — which reads to the
  user as the app dying. `_PeerServer` now holds a
  `BoundedSemaphore` (`max_client_threads`, default 16, tunable via
  `ShugonetAgentRuntime(max_client_threads=...)`) claimed by the accept loop
  before it spawns, and released by the new `_serve_client` wrapper on every
  exit path. A saturated server sheds the excess connection (closing it, which
  the peer retries) instead of growing threads, so the worst case is a refused
  connection, never an unbounded leak.
- **`tests/test_memory_sharing.py`**, **`tests/test_agent_runtime.py`** — new
  classes pin every contract: `TestPeerErrorsAreNotFalseSuccess` (an `error`
  frame surfaces as `status: error` from `sync()`, is skipped by `query()`, a
  real `sync_result` is *not* misread as an error, and both helper shapes),
  `TestPeerDialDoesNotBlockInit` (`add_peer()` after `start()` and `start()`
  itself return in well under the connect timeout while still delegating the
  dial), and `TestServerConcurrencyBound` (a full server drops excess
  connections, and released slots are reused across sequential requests). The
  `_PeerServer` unit tests cover the slot bound, the clamp, and slot release on
  both normal and raising handler exits.

### CI integrity: the 3.9 regression, missing optional deps, packaging

The `CI` workflow had been red on every push since 2026-09-15. Two independent
failure classes were hiding behind the matrix's default `fail-fast`: a genuine
Python 3.9 incompatibility, and a suite that exercised optional dependencies CI
never installed. Both are fixed — `test (3.9)` through `test (3.13)` pass
together now.

- **`py_compat.py`** (new) — one documented home for stdlib features newer than
  the `requires-python = ">=3.9"` floor. `dataclass_slots` is
  `dataclasses.dataclass` with `slots=True` on 3.10+, and a plain dataclass on
  3.9, where the keyword does not exist. `@dataclass(slots=True)` raises
  `TypeError` at *class definition* time, so the four uses in
  `kv_mesh/shard.py` (3) and `personality/governor.py` (1) — added in v1.28.1 —
  made both packages unimportable on 3.9: 189 cascading `TypeError`s and 120
  errors from that one construct. Slots are still applied everywhere the
  interpreter supports them; only the 3.9 fallback omits them, so the on-device
  memory win is kept.
- **`tests/test_py_compat.py`** (new) — pins both halves of the contract (a
  working dataclass on every interpreter, `__slots__` retained where supported,
  `default_factory` still per-instance) and guards the two production classes
  that broke.
- **`tests/test_simulation.py`** — skips the module *with a reason* when numpy
  is absent instead of failing collection: `simulation.base` builds its state
  vectors with numpy. One bare `import numpy` previously reddened every
  interpreter in the matrix (the 2026-09-06 `test (3.11)` failure).
- **`tests/test_pg_memory.py`**, **`tests/test_entity_graph.py`** — the PG tests
  patch `_HAS_PSYCOPG` to `True` to exercise real `psycopg2.sql.Identifier`
  composition, which needs the actual driver; without it they now skip with an
  install hint instead of raising `AttributeError: 'NoneType' object has no
  attribute 'Identifier'` (21 errors on the runner).
- **`telemetry.py`** — binds `_otel_trace = None` when the OTel import fails, so
  patching that name works on a host without the `telemetry` extra (previously
  the one remaining `test_v1` failure). Real usage stays gated on `_HAS_OTEL`.
- **`.github/workflows/ci.yml`** — `fail-fast: false` (the matrix is how we see
  *which* interpreter breaks; fail-fast hid 3.10–3.13 behind 3.9), a
  `workflow_dispatch` trigger so a work branch can be reviewed on demand with
  `gh workflow run ci.yml --ref <branch>`, and the test job installs `.[dev]`
  alongside `requirements.txt`. It also provisions the vendored NRR submodule
  and re-applies `patches/nrr/`: three tests read that tree (`nrr_android_port`'s
  submodule + portability checks, `xr_scaffold`'s descriptor sweep) and had been
  erroring with `FileNotFoundError: .../cpp/nrr/runtime/onnx_runtime.cpp` on
  every run. Only NRR is fetched — a recursive checkout would clone the whole
  llama.cpp history onto all five matrix legs.
- **`patches/nrr/0002-godot-plugin-descriptor-ini.patch`** (new) — the NRR Godot
  descriptor's XML→INI conversion had only ever existed inside a working copy,
  so a fresh clone restored the Godot-3 XML file and `test_all_descriptors_are_ini`
  failed on CI while passing locally. It is a re-appliable patch now, and the
  durability guard's needle list includes `engine_plugins/godot/plugin.cfg` so
  the gap cannot reopen silently. Verified against a fresh clone of the pinned
  SHA: the whole series applies and reproduces all four in-submodule fixes.
- **`pyproject.toml`** — `dev` carries the optional dependencies the suite
  exercises for real (`numpy`, `psycopg2-binary`, `opentelemetry-api`), and
  `py-modules` gains `delegation`, `mesh_election`, `mesh_rpc`, `py_compat`,
  `security_inventory` and `talker`. None of those were declared, so a built
  wheel was missing modules that `shugocore_agent` and `shugocore_server`
  import at runtime — a latent packaging bug independent of CI.
- **`.github/workflows/release.yml`** — removes `build/` and `dist/` before
  `python -m build`. setuptools never prunes `build/lib`, so a warm working
  tree re-packs the previous build's `__pycache__` into the wheel: 73 `.pyc`
  files (~1.28 MB) inflated a locally built v1.30.5 wheel to 950 KB where a
  clean build is 340 KB. Runner workspaces are fresh, so published artifacts
  were never affected — this is belt-and-braces plus a local-build trap
  removed.
- **`.github/workflows/android.yml`** — a branch-dispatched APK build could
  never succeed: `NAME="shugocore-${GITHUB_REF_NAME#v}.apk"` turns
  `hotfix/pypi-publishes-cia` into a `cp` target inside a directory that does
  not exist (and an invalid artifact name). The ref is now slash-sanitised, and
  the release-attach step reuses `$APK_PATH` instead of re-deriving the name.
- **Dropping the 3.9 floor** is tracked in issue #12 for a later release; until
  that is decided, `py_compat` keeps 3.9 working and the matrix keeps testing it.

### Mobile fleet wiring on the desktop server + operator pairing route

- **`shugocore_server.py`** — `main()` now constructs the canonical mobile
  stack (`MobileNodeManager` + `MobileComputeBroker` + `MobileExecutionHandler`)
  and injects it as `engine_kwargs["mobile_handler"]`, so `GET /api/v1/fleet`
  reports `enabled: true` instead of the previous hard-wired `enabled: false`
  (the CLI never passed a handler). Opt out with `--no-mobile`; a construction
  failure degrades to `enabled: false` rather than refusing to start. New
  bearer-authenticated `POST /api/v1/fleet` implements the documented operator
  pairing flow (`{"device_id", "action": "pair"|"unpair", "manifest", ...}`):
  pairing allowlists the node, keeps the ingest topic-ACL allowlist in sync
  (set semantics — pair adds, unpair removes), and subscribes the device's
  contract topics. `GET /api/v1/sensors` still reports `enabled: false` on a
  bare desktop engine by design — a sensor stream is only reported when the
  hosted agent's telemetry actually exists, never fabricated.
- **`tests/test_shugocore_server.py`** — two new tests cover the pairing route
  (400 on bad input, allowlist sync on pair *and* unpair, fleet visibility,
  503 when the fleet is disabled).

### Distributed mesh primary election, security baselines, XR scaffold

The Android custodian (Exynos-1380 page-robot family) and a paired desktop can
now form a mesh with exactly one primary: the primary runs the agent loop's
side-effects (speech, `ask_user`), every other live node falls back to
peripheral mode (sensors + journal + RPC offload). Design rule: **paired + fresh
heartbeat + thermal < 3 + headroom > 0 are candidates; lowest priority wins,
tie-break on smallest node_id**. Lease 10s, heartbeat timeout 30s. A partitioned
node fails closed to standalone; re-merge is a fresh election.

- **`mesh_election.py`** (new) — deterministic, thermal-aware election plus the
  split-layer command builder. `observe_heartbeat(payload, now=None)` accepts an
  injectable clock for tests and replay.
- **`shugocore_agent.py`** — each tick feeds local thermal/memory and the
  peer snapshots Kotlin pushes, then re-evaluates the lease
  (`_mesh_heartbeat_tick`). All four side-effect chokepoints
  (`_execute_speak`, `_execute_ask_user`, `_speak_direct`, `speak_test`) are
  gated on the lease and a follower refusal is journaled, not silently dropped.
  `get_status()` reports `mesh_role` (`primary` / `follower` / `standalone` /
  `none`) and `mesh_primary` — **a lone node holding its own single-node lease
  reports `standalone`, never `primary`**.
- **`shugocore_server.py`** — `/api/v1/status` surfaces `mesh_role` /
  `mesh_primary` additively; absent keys are never fabricated.
- **`security_inventory.py`** (new) — one bounded, observational snapshot of a
  node's posture (audit-chain integrity via the real `verify()`, policy
  invariants, network policy, granted caps, consent actions, mesh role) plus a
  documented baseline evaluator. Violations are `drift` (control present but
  wrong) or `unverifiable` (control absent — always critical: silence is not
  safety). A not-yet-written audit chain reports `empty`, not tampered.
  `GET /api/v1/security` exposes the server's own controls and any drift,
  including a tokenless dev server reported *as* drift.
- **`platforms/godot/`** (new) — Godot 4 OpenXR scaffold wired to the real wire
  contracts (`/health`, `/api/v1/status`, `/api/v1/sensors`, `POST /api/generate`,
  policy-gated `POST /api/v1/task`), bearer token via `SHUGOCORE_SERVER_TOKEN`.
  XR bootstrap reports only observed modes (`xr` / `desktop_preview` /
  `unavailable`); agent presence never claims a headset it does not have, and
  LISTENING is opt-in only. Boots clean headless (Godot 4.7.2) with zero script
  or scene errors.
- **Kotlin** — `NodeStatusHeader` shows the mesh role (`a lone node renders
  STANDALONE, never PRIMARY`); a peripheral can now advertise its election
  fitness over RFCOMM as a `mesh/health` message (`thermal_status`,
  `mem_available_bytes`, `priority`, monotone `seq`). Fields are clamped on
  receipt (thermal 0..4, priority 1..9999), and **both** peer serializers emit
  them only when the peer actually advertised health — a silent peer stays
  ineligible rather than having values invented for it.

## [1.30.5] - 2026-09-20

### Closed conversational loop: toolable-question routing, honest measurements, ask_user answers

Measured on the A51 (SM-S515DL) + Tab S9 FE (SM-X518U): asking the agent basic
things (*what time is it*, *what is the temperature*) produced no proper tool use
and often no response at all. Three separable defects:

- **A question's phrasing decided whether a tool was reached.** Only the literal
  string `what time is it` was promoted to a command; `what's the time`,
  `do you have the time`, `current time`, `what's the date`, `what day is it`,
  `what is the temperature`, `how hot is it`, `what's my battery` all stayed
  QUESTIONs and were answered by the language model — which invented readings
  (`"The temperature outside is currently 20 degrees Celsius."`, an older window
  even a fabricated city). `tell me the time` mis-routed to `search`
  ("I can search for that. Let me think about it.") and `get_date` /
  `check_sensors` were unreachable from any transcript. `handle_time` also
  executed the `get_time` tool twice per query.
- **A turn could end with no spoken outcome at all.** An injected `what's the
  time` was recorded as heard and then produced no tool answer, no model speak
  and no fallback line — silence.
- **`ask_user` was fire-and-forget.** `_execute_ask_user` returned
  `{status, asked, delivered}`; the answer was paired on the bus as metadata
  only and never answered; expiry at `_ANSWER_TTL_S` was silent;
  `ConversationManager.on_speak_end` had zero callers so `ConversationState.
  WAITING` was unreachable; and the one existing loop (timer clarification) only
  accepted digits — on device `five minutes` produced *"I didn't catch that"*
  while `5 minutes` set the timer.

Fixes: `IntentParser.tool_topic()` + `_categorize(verb, transcript, tool_topic)`
make routing phrasing-independent (new `handle_date` / `handle_temperature` /
`handle_sensors`, single-call `handle_time`); `get_temperature` reports a real
reading (ambient vs device/CPU framed, `<= 0` = no reading) or refuses honestly;
the agent re-routes toolable questions before the model, guarantees exactly one
spoken outcome per turn (blocked/empty/unusable/crashing decisions degrade to an
honest line), suppresses model-supplied measurement claims, and closes the
ask_user loop (verified answer, `question_answered` / `question_expired`
journalled, tick-time expiry, verified question passed to the prompt); the
clarification resolver accepts number words; unknown commands now fall through
to the conversational path instead of "I can't do that yet, but I'm learning!";
`handle_search` no longer promises a backend that does not exist.

Tests: `tests/test_tool_routing.py` (21 — phrasing matrix, parser↔router
consistency, temperature honesty, no-fabrication guard, word-number clarify) and
`TestAskUserLoop` in `tests/test_speech_output.py` (9), plus the root↔bundled
drift guard now recurses into subpackages. `DialogueState`'s clarification window
is now 120 s, matching the interaction bus's answer TTL — at 90 s a spoken answer
could be parsed after it expired, because the conversational fast path waits
behind the loop's own model decision (20-90 s measured).

- **Release gate** — three new device phases: `time_tool_query` (a RE-WORDED
  clock question must reach the tool), `measurement_honesty` (a temperature
  question is answered by a tool or refused, never an invented number) and
  `ask_user_round_trip` (the agent's own question, detected via the bus's
  `pending_question`, gets an answered reply). All three pass on hardware. Also
  fixed two harness bugs the phases exposed: `measure_decision_cadence` matched
  only dotted timestamps (`logcat -v brief` has no prefix and the engine writes
  `15:04:49,311`), so it always returned its 50 s floor — the Tab now measures a
  real 21.8 s cadence; and a bounded warm-up plus `await_conversation_idle` stop
  a slow node failing phases for backlog rather than behaviour. The run now also
  prints the device thermal status (the A51 hit status 4/critical with decisions
  ~100 s apart — a phase failure there is thermal, not behavioural).
- **`shugocore_server.py`** — `build_server` binds `_BoundHTTPServer`
  (`request_queue_size = 128`) instead of socketserver's default 5: macOS resets
  a SYN that arrives while the accept queue is full, before the handler or the
  rate limiter runs, and the fleet/approvals surfaces invite concurrent clients.

## [1.30.4] - 2026-09-18

### CSFA hardening: bandit B608, remote audit, fleet auth, embedders, backend pools, pg memory, approvals console, Termux/NPU/memory-policy infra, server hardening

- **Bucket A (safety-critical)**: bandit B608 fix — all `pg_memory.py`
  identifier interpolation uses `psycopg2.sql.Identifier`; remote audit
  log shipping (`LogSink` + `HTTPSAuditSink` + `FileAuditSink`, bounded
  queue/retries/flush, sink failure can never block a decision,
  `SHUGOCORE_AUDIT_HTTPS_URL` / `SHUGOCORE_AUDIT_FILE_PATH` env wiring);
  Android bearer-token pairing for the desktop API.
- **Bucket B (CSFA loop gaps)**: pluggable embedding backends
  (`HashingEmbedder` default, byte-identical to legacy vectors;
  optional `SentenceTransformerEmbedder`); per-model `BackendPool`
  with health routing + circuit breaker; PostgreSQL/pgvector env
  switch (`SHUGOCORE_MEMORY_DSN`); Tier-2 entity/relation graph
  helpers (`link_entities`, bounded `query_subgraph`).
- **Bucket C (operator surfaces)**: approvals console, fleet endpoint,
  bounded sensor live-stream; `ApprovalBroker` late-verdict race fix
  (first resolution wins).
- **Bucket D (long-horizon infra)**: Termux llama-server launcher, NPU
  bring-up rungs (Hexagon QNN / MediaTek APU probing), per-agent
  memory policies (`shared_rw` / `shared_read` / `isolated`), CI
  trusted (OIDC) PyPI publishing + SPDX SBOM + Sigstore signing.
- **Server hardening**: strict loopback check (numeric IPv4 in
  127.0.0.0/8 via `ipaddress` — lookalike DNS like `127.evil.com`
  no longer counts); prefix-route auth-first (suffix tricks 404, never
  reach real handlers); query-tolerant routing; approvals/fleet
  malformed-payload tolerance.
- **Verified**: full suite **1,105 passing**; ruff clean;
  bandit-high clean; CSFA host soak **STABLE** (118 ticks, 6/6 checks).

## [1.30.3] - 2026-09-15

### Desktop activity API, CSFA soak tools, bounded model scoring

The loop that was made *visible* in 1.30.2 is now made **verifiable over
time** — and the soak immediately paid for itself with two real findings.

- **Desktop activity API** (`shugocore_server.py`): `GET /api/v1/activity`
  (per-endpoint request counters, outcome buckets, bounded latency ring,
  requests/minute, and a verbatim `agent` block — loop / loop_stages /
  mesh_activity — when the server hosts a full agent), `GET /api/v1/uptime`,
  and additive `activity` + `uptime_seconds` keys on `GET /api/v1/status`.
  Accounting is observational and bounded; a bare DecisionEngine deployment
  simply omits the agent block — never fabricated.
- **CSFA soak** (`tests/csfa_soak.py`): wall-clock host endurance with
  periodic invariant checks (stage liveness, counter consistency,
  audit-chain integrity, bounded structures, honest uptime), deterministic
  scripted backend plus a `--null-dialect` mode. JSON report; nonzero exit
  on any violation.
- **Device soak** (`tests/android_device_soak.py`): the same endurance
  posture over adb — process liveness, native-crash and Python-traceback
  watch, a time-based wedge detector tuned to real on-device cadence, and
  optional conversational injections. Per-device JSON verdict.
- **Fixed: `VERIFY_ATTENTION` stage was never stamped** — the v1.20
  attention layer runs every tick, but the stage liveness surface showed
  `unknown` forever. Stamped when (and only when) the layer evaluates; an
  absent layer still reports honest `unknown`. The endurance suite now
  requires 7 of 8 stages stamped on SUCCESS cycles.
- **Fixed: unbounded model-performance growth** (`model_manager.py`) — the
  multiplicative ×1.1-per-success update had no ceiling; a healthy loop
  drove `aggregated_output` to 1.3e12 within 293 soak cycles and would
  overflow the float at ~7300. Scores are now capped at
  `MODEL_PERFORMANCE_CAP` (100.0) on both the update and the RL set path;
  relative ranking in the working regime is unchanged.
- **Verified by the tools this release ships**: host soak STABLE (5 min,
  294 ticks); Tab S9 FE (1.5B, dotprod) 15-min soak STABLE — 82 decisions,
  0 crashes, 0 tracebacks; A51 (0.5B, portable) 15-min soak STABLE — 22
  decisions, 0 crashes, 0 tracebacks.


## [1.30.2] - 2026-09-15

### CSFA activity + uptime instrumentation, endurance verification, and the ACTIVITY tab

The Continuous Synthetic Functional Agency loop is now **verifiable, not
assumed** — instrumented end to end and confirmed on real hardware.

- **Loop instrumentation** (`AndroidAgent`): bounded counters (cycles,
  `by_outcome`, `by_source`, `by_action`, conversational ticks), a 60 s
  cycles-per-minute window, a 100-entry activity ring, per-stage timestamps
  for all 8 CSFA stages (`ok` / `stale` > 60 s / `unknown`), `uptime_seconds`,
  and `mesh_activity` (shared facts by provenance peer). Purely observational —
  no change to any decision, gate verdict, or outcome.
- **Fixed**: `decision_source` status key was the constant `"none"`;
  `execute_task` now surfaces `proposal_source`/`action_type` so cycle truth
  is real.
- **Endurance suite** (`tests/test_csfa_endurance.py`): 30-cycle scripted
  model-driven run asserting full stage-trail stamping, zero rule fallbacks,
  audit-chain integrity, and honest accounting; plus the null-dialect case
  proving the documented 3-strike fallback is reported and unchanged.
- **On-device verification**: A51 (portable build, 0.5B — 33 cycles, 28
  model-sourced) and Tab S9 FE (dotprod, 1.5B — 33 cycles, 23 model-sourced,
  conversational `ask_user`/`speak`), 0 crashes.
- **Android UI**: new **ACTIVITY tab** (recent-cycle ring, outcome ledger,
  loop rate/uptime, memory-mesh activity) and **AGENT tab** Loop section +
  Loop-stages liveness. Missing evidence renders `—`, never fabricated
  success. Verified live on both devices.

### Android UI: the loop is visible (ACTIVITY tab, AGENT tab additions)

- **New ACTIVITY tab** — the recent-cycle ring (timestamped
  `action [outcome] via source · duration` rows), loop accounting
  (cycles / conversational ticks / cycles-per-minute / success rate / uptime),
  the bounded outcome ledger, and memory-mesh activity (shared facts in, by
  provenance peer).
- **AGENT tab additions** — a **Loop** section (uptime, cycles, rate, last
  outcome) and **Loop stages**: all 8 CSFA stages with
  `"age · ok / stale"` liveness, `—` when the agent has no evidence yet.
- Everything binds the exact keys AndroidAgent.get_status() emits
  (`loop`, `loop_stages`, `mesh_activity`, `uptime_seconds`); missing
  evidence renders `—`, never fabricated success.

The 1 Hz agent loop now accounts every pass in **bounded** structures so the
Continuous Synthetic Functional Agency loop is verifiable, not assumed:

- **Counters** — cycles, `by_outcome` (the outcome contract), `by_source`
  (which model / rule fallback drove it), `by_action`, and
  `conversational_ticks` (separate from decision cycles).
- **Loop-stage liveness** — per-stage timestamps for all 8 stages
  (OBSERVE … CONSOLIDATE) with honest `ok` / `stale` (> 60 s) / `unknown`
  (no evidence yet, never fabricated).
- **Activity ring** (last 100) and a 60 s rolling window for
  cycles-per-minute.
- **Mesh activity** — shared-fact counts by provenance peer and the total.
- **`uptime_seconds`** on the agent status.

Supporting fixes: `DecisionEngine.execute_task` now surfaces
`proposal_source` / `action_type` on its result (the status key
`decision_source` was previously a constant `"none"`), and the scripted
endurance backend is config-tolerant. New
`tests/test_csfa_endurance.py`: a **30-cycle sustained model-driven loop**
verifying every declared stage is stamped, outcome accounting matches,
Tier 2 grows, the audit chain verifies across the whole run, the activity ring
is ordered and bounded, and the null dialect is reported honestly.

**Verified on hardware.** With the instrumented loop running on both test
phones (A51 Exynos 9611 + Tab S9 FE), the decision log shows the loop cycling
with predominantly model-sourced decisions — A51: 33 cycles, 28 model-sourced
(`record_observation` / `network_list_agents`); Tab: 33 cycles, 23
model-sourced (`ask_user` / `speak` / `record_observation`), one side-effecting
proposal correctly refused by the policy gate.

## [1.30.1] - 2026-09-14 — one APK, self-determining CPU kernels

The fleet needed a single installable that is safe on every arm64 SoC and still
fast where the silicon allows it. This release makes that the default.

### Runtime-selectable CPU kernel variants (`platforms/android`)
- **`GGML_CPU_ALL_VARIANTS` + `GGML_BACKEND_DL` are now the Android default.**
  ggml builds each arm64 kernel set into its own dlopen'ed backend library
  (`libggml-cpu-android_armv8.0_1.so` … `android_armv9.2_2.so`). Each exports
  `ggml_backend_score()`, which returns **0** when the running CPU lacks that
  variant's features (detected via `getauxval(AT_HWCAP/AT_HWCAP2)`), and
  `ggml_backend_load_best("cpu")` loads the highest scorer — so the same APK
  picks the portable `armv8.0` kernels on an Exynos 9611 and the
  dotprod+fp16 set on an Exynos 1380, with no per-device builds.
- **`llama_jni`** now holds the shared llama/ggml libraries and gained
  `nativeSetBackendPath()`: ggml's own search paths (executable dir, cwd,
  `GGML_BACKEND_DIR`) never include Android's native library directory, so the
  app passes `applicationInfo.nativeLibraryDir` in before any model work —
  otherwise no CPU backend registers and every model call fails.
- **`extractNativeLibs`/`useLegacyPackaging` is now `true`.** With the libs
  packed uncompressed inside the APK they are not enumerated by
  `fs::directory_iterator`, so the variants are invisible (verified: 0
  backends found on the A51). Extraction makes the directory real.
- **16 KB page alignment** applies to every shipped shared object, not just
  `llama_jni` (global `add_link_options`).
- **Escape hatch:** `-Pshugocore.singlearch=true` restores the historical
  static single-library layout (with the `-Pshugocore.dotprod` override).
- **Verified on two devices with one APK:** A51 (no `asimddp`) →
  `libggml-cpu-android_armv8.0_1.so`; Tab S9 FE (`asimddp`+fp16) →
  `libggml-cpu-android_armv8.2_2.so`. Both models load, zero crashes.

### Decision-prompt steering (`subconscious.py`)
- The decision prompt now says to prefer `record_observation` for routine
  self-maintenance and that side-effecting actions (send/query/sync, device or
  hardware control) need operator approval — reducing the rate at which the
  small model proposes gated actions for nothing.

### Signing / release
- The release APK is signed from environment key material
  (`SHUGOCORE_KEYSTORE_FILE` + password/alias vars), never committed; CI
  restores it from the `ANDROID_KEYSTORE_BASE64` secret and attaches the
  signed APK to the release (`.github/workflows/android.yml`).

## [1.30.0] - 2026-09-13 — fleet memory mesh + on-device structured inference

Two capabilities that were previously a façade are now real, and the on-device
agent loop is model-driven instead of rule-driven.

- **ShugoNet memory sharing was a stub.** A cross-agent `query` returned an
  invented `"stub from <agent>"` fact and `sync` merely acknowledged, so a
  two-device "combined memory" could not be real. Both are now backed by each
  agent's living Tier 2 memory — verified between an A51 and a Tab S9 FE:
  A51 pulled **64** facts from its peer (provenance-tagged) and the reverse
  direction imported 2, with the imported knowledge recallable through the
  agent's own memory API.
- **On-device structured inference.** With no `.gguf` staged, the loopback
  API server never started and the model backend logged `URLError` forever;
  and the build forced ARMv8.2 dotprod kernels that took `SIGILL` on CPUs
  without `FEAT_DotProd`. Both are fixed, and the decision path now uses
  **grammar-constrained decoding** so a 0.5B model cannot emit unparseable
  output. Verified: decisions went from **100% `rule_fallback`** to
  predominantly `proposal_source: <model>` with schema-valid JSON.

### ShugoNet memory sharing is real (was a stub)

### ShugoNet peer runtime (`agent_runtime.py`, mirrored to the Android tree)
- **Real memory-backed query.** An inbound `query` is answered from the
  peer's `MemoryManager` (`retrieve_context`) instead of a stub; with no
  memory backend configured the reply is empty, never fabricated.
- **Real, incremental `sync`.** An inbound `sync` exports Tier 2 facts
  created after the caller's watermark; the caller merges the reply into its
  own memory and advances a per-peer watermark, so repeats transfer nothing.
  Returns `{received, imported, duplicates}`.
- **Request/response transport fix.** `query`/`sync` now send and read their
  reply on a dedicated short-lived connection (`_one_shot_request`); the
  persistent outbound socket is send-only, so previously no reply could ever
  be read.
- **Fixed a latent self-deadlock** in `_PeerConnection.connect()` (a plain
  `Lock` re-acquired via `close()`); the lock is now reentrant. This fired on
  any real `add_peer()`/`start()` connect and was never exercised before.
- **Conflict-storm guard.** Duplicate-content syncs are counted over a
  sliding window; exceeding `conflict_threshold` reports the existing
  deterministic `memory_sync_conflict_storm` fallback. Duplicates are always
  idempotent (never re-stored).
- **Bounded peer reconnection.** Two nodes starting together each dial the
  other before it is listening, so the first dial can fail; the outbound
  socket is now re-dialed by `reconnect_peers()` (also run periodically by a
  bounded `reconnect_interval` thread, disable with `0`). `mesh_status`
  re-dials before reporting, so "connected" reflects reality instead of a
  stale startup failure.
- **Bind-failure reporting fixed.** `_PeerServer` published its socket before
  binding, so a failed bind left a phantom port (`getsockname()` -> 0) that
  peers then dialed, producing a confusing `EADDRNOTAVAIL` instead of a clear
  "not listening". The socket is now published only after a successful bind,
  and `bind` failures are logged with host:port.
- **Peer configuration.** `SHUGOCORE_MESH_PEERS` ("id=host:port,..."),
  `SHUGOCORE_MESH_PORT`, and `SHUGOCORE_MESH_TOKEN` let an agent join a mesh
  at bootstrap (`shugocore_agent._start_shugonet`). Android nodes join with
  `mesh_peers.json` in the app data dir instead
  (`{"peer-id": "host:port"}` or a list of `{"id","host","port"}`) — no
  environment variables required on device.

### On-device mesh control (`subsystems/intent.py`, `subsystems/command_router.py`)
- The intent parser now classifies `sync` / `share` / `mesh` transcripts as
  commands, and the command router routes them to a `mesh` category
  ("sync your memory with your peer", "mesh status", "share what you know").
- `AndroidAgent` exposes `mesh_status` / `mesh_sync` tools wired to its live
  ShugoNet runtime, so a spoken command merges a peer's Tier 2 memory into
  the agent's own store and reports `imported` / `already known` counts.

### Tier 2 memory (`memory_system.py`, mirrored to the Android tree)
- **`SemanticMemory.facts_since()` / `content_exists()`** — bounded,
  deterministic export and a content dedupe primitive for shared facts.
- **`MemoryManager.export_shared_facts()` / `import_shared_facts()`** — the
  sharing boundary. Only Tier 2 crosses the mesh (Tier 0/1 stay private, Tier
  3 stays read-only identity); imports are idempotent and record
  `shared_from` / `shared_at` provenance. `shugonet` is now a sanctioned
  Tier 2 writer in the write gate.
- **`count_shared_facts()` / `SemanticMemory.count_shared()`** — durable,
  provenance-aware count of retained mesh facts (survives restart), used by
  `mesh_status`. Note: imported facts are normal Tier 2 memory, so the
  consolidation pipeline can later compress them (e.g. `summary` -> `pattern`)
  and provenance is not propagated through that rewrite; the retained count
  therefore decays over time by design.

### Tests / tooling
- `tests/test_memory_sharing.py` (9 tests): primitives, provenance,
  idempotent dedupe, the two-agent combined-memory contract over real TCP,
  no-backend behavior, and the conflict-storm guard.
- `tests/two_agent_memory_smoke.py`: live harness — local two-agent mode, and
  `--remote host:port` to pair with an Android node via `adb forward`.

### Android on-device inference (`platforms/android`)
- **Portable arm64 baseline by default; dotprod is now opt-in.** The CMake
  build forced `GGML_CPU_ARM_ARCH=armv8.2-a+dotprod` for every arm64-v8a
  target, which compiles the *baseline* ggml CPU backend with dotprod
  (SDOT/UDOT). Those kernels are not runtime-gated, so on any arm64 SoC
  without FEAT_DotProd the first Q4_K_M matmul died with
  `Fatal signal 4 (SIGILL), code 1 (ILL_ILLOPC)` inside
  `ggml_vec_dot_q5_0_q8_0` — a native crash + restart loop, observed on the
  Exynos 9611 test unit whose `/proc/cpuinfo` has no `asimddp`. The default is
  now the portable baseline; dotprod-capable fleets opt in with
  `./gradlew assembleDebug -Pshugocore.dotprod=true`. Upstream's safe-and-fast
  multi-variant path (`GGML_CPU_ALL_VARIANTS`) requires
  `GGML_BACKEND_DL`/shared libs and does not fit the static single-library
  layout.
- **On-device model staging is a hard prerequisite.** With no `.gguf` in the
  search path, `findModelFile()` returns null, `LocalApiServer` never starts,
  and the Python backend logs `URLError` on every model call (endless rule
  fallback). Verified fixed by staging `Qwen2.5-0.5B-Instruct-Q4_K_M.gguf`
  (397 MB) where the service looks for it: the loopback API then binds
  `127.0.0.1:11434` and real on-device generation resumes (~24-34 s per
  decision on the A51 baseline build).

### Grammar-constrained decoding (on-device structured decisions)
The on-device 0.5B model answered but emitted loose dialects
(`action_type: speak` with no braces, or `record_observation: {json}`) that
`DecisionEngine._parse_proposal` could not read, so every decision fell back to
rules even once the model was healthy.
- **Schema-derived GBNF** (`subconscious.build_decision_grammar`): the grammar
  is built from the engine's own `available_action_types()`, so
  `action_type` is restricted to the real executor set (plus `null`) and the
  proposal keys/order are pinned. Root ends at the closing brace, so a
  satisfied grammar completes immediately instead of padding whitespace.
- **Decision path only** — `get_model_output` passes the grammar;
  `get_conversational_output` stays free text (speech is not JSON).
- **Backend plumbing**: `BaseBackend.generate(..., grammar=None)`.
  `OllamaBackend` maps it to `format: "json"`, `OpenAICompatibleBackend` to
  `response_format: {"type": "json_object"}`, and `AndroidBackend` sends the
  GBNF verbatim. A cached capability probe keeps older backends and test
  doubles (whose `generate` predates the kwarg) working.
- **`LocalApiServer`**: accepts `grammar` (bounded to 8 KiB) or Ollama's
  `format: "json"` (→ a generic JSON-object GBNF) on `/api/generate` and
  `/api/chat`; an unusable grammar is dropped, never fatal.
- **`llama_jni`**: a persistent `llama_sampler` grammar on the session,
  installed via `nativeSetGrammar` and freed on reset/free. Three llama.cpp
  facts are load-bearing and documented in the code:
  - the grammar must be added **first** in the per-token chain
    (`common_sampler_sample`'s `grammar_first`), because applying it after
    `temp`/`top_p` lets an already-softmaxed token bypass the `-INF` mask;
  - `llama_sampler_sample()` **already accepts** the sampled token on the
    chain, so the grammar must not be accepted again;
  - a satisfied grammar offers only EOG, and accepting EOG exhausts the
    grammar stacks → `std::runtime_error`. That is normal completion, so it is
    caught and treated as end-of-sequence (previously an uncaught throw →
    SIGABRT restart loop).
- **Verified on the SM-S515DL**: `grammar: caller GBNF (977 chars)` →
  `grammar installed` → model returns schema-valid JSON; decisions are
  predominantly `proposal_source: shugocore-local`, up from 0% model-sourced
  (was 100% `rule_fallback`).

### Execution dispatch consults the handler registry (`execution_layer.py`)
`register_handler()` has always accepted network/mobile/robotics/custom action
types, and `DecisionEngine.available_action_types()` advertises them (it unions
the registry, so the model prompt and the decision grammar offer them) — but
`ExecutionLayer._dispatch()` was a hardcoded `if/elif` chain that only looked
the registry up for `record_observation` / `speak` / `ask_user`. Every
pluggable handler was therefore unreachable end-to-end: advertised to the
model, gated by policy, then rejected with `Unknown action type`. Observed
live once grammar-constrained decoding made the model actually propose
`network_list_agents`. Dispatch now falls back to the registry for any
registered type (unregistered types still fail closed with the same message),
so "advertised == executable" holds again.

## [1.29.1] - 2026-09-13 — network-surface hardening

Bounded the two network-facing surfaces that previously accepted unbounded or
unauthenticated input. No frozen Python API changed; all additions are
opt-in/backward-compatible defaults.

### Desktop server (`shugocore_server.py`)
- **Fail-closed exposure.** Binding to a non-loopback host is now refused
  unless `SHUGOCORE_SERVER_TOKEN` is set or `--allow-unauthenticated` is passed
  explicitly. With a token configured, every route except `/health` requires
  `Authorization: Bearer <token>` (or `X-ShugoCore-Token`), compared in
  constant time via `hmac.compare_digest`.
- **Per-client rate limiting.** Token-bucket limiter (`security.RateLimiter`)
  keyed by client address; `--rate-limit-per-minute` (default 240) and
  `--rate-limit-burst` (default 120); `/health` exempt; overflow returns 429.
- **Body-size guard fixed.** A negative `Content-Length` previously reached
  `rfile.read(-1)` (drain-to-EOF); negative and oversized lengths are now
  rejected before reading.
- **CORS narrowed.** Preflight echoes `Access-Control-Allow-Origin` only for
  loopback origins instead of `*`, and allows the auth headers.

### ShugoNet peer runtime (`agent_runtime.py`, mirrored to the Android tree)
- **Frame-size bound.** A peer that never sends a newline can no longer grow
  the receive buffer without bound: a frame over `_MAX_FRAME_BYTES` (1 MiB,
  `--max-frame-bytes`) closes the connection; oversized framed messages are
  dropped.
- **Message validation.** Inbound payloads must be dicts with a non-empty
  string `type` before dispatch; malformed messages are logged and ignored.
- **Optional shared-secret gate.** `auth_token` (`--auth-token-env`, default
  `SHUGOCORE_MESH_TOKEN`) requires a matching `token` on inbound messages and
  stamps it on outbound send/query/sync. Default off (unchanged behavior).

### Model backends (`model_backends.py`, mirrored to the Android tree)
- **Base-URL validation.** `OllamaBackend` / `OpenAICompatibleBackend` now
  reject non-http(s) schemes (`file://`, `ftp://`), missing hosts and embedded
  URL credentials at construction, instead of passing the raw config value to
  the transport.
- **Redirects refused.** Every request sets `allow_redirects=False` and a 3xx
  response raises `BackendError`, so a redirect can no longer bounce a request
  past an allowlist.
- **Response size cap.** Bodies are streamed and refused past
  `MAX_RESPONSE_BYTES` (256 KiB), bounding how much a hostile endpoint can make
  the process buffer.

### Security primitives (`security.py`, mirrored to the Android tree)
- **Token redaction fixed.** The previous secret-in-text regex captured only up
  to the first whitespace, so ``Authorization: Bearer <token>`` redacted the
  word "Bearer" and leaked the token. The pattern now consumes an optional
  ``Bearer``/``Basic`` scheme and masks the whole credential.
- **Invisible-Unicode stripping.** `sanitize_text` now removes bidi
  overrides, zero-width and other Unicode format characters (U+00AD,
  U+200B–U+200F, U+202A–U+202E, U+2060–U+2064, U+2066–U+2069, U+FEFF) that
  could spoof logs or smuggle prompt text.
- **SecretResolver fallback narrowed.** The bare ``<NAME>`` environment
  fallback applies only to env-var-shaped names, so an arbitrary caller string
  cannot probe unrelated environment variables.

### Latent bugs found by the new lint gate (ruff `F821`)
- `audit.py` referenced an undefined `logger` in `_load_existing`, so a
  malformed or unreadable chain raised `NameError` instead of being reported.
- `mobile_nodes.py::KVTransportAdapter` called `kv_mesh.protocol` helpers
  (`make_advertise` / `make_assign` / `make_put` / `make_get` / `make_evict` /
  `make_heartbeat` / `msg_type` / `proto.TOPIC_*`) that were never imported, so
  every method raised `NameError` — the KV-mesh DDS adapter was
  non-functional. Imports added and now covered by tests.
- `shugocore_agent.py::nrr_status_json` used `_json` without the local
  `import json as _json` its sibling accessors use; and an unreachable
  `return {"asked": text, ...}` after the `return` in `_extract_perception`
  (dead code) was removed.

### CI / supply chain
- CI matrix extended to **Python 3.13**.
- New blocking **`lint`** job (`ruff check`, configured in `pyproject.toml`
  with `[tool.ruff]` selecting `E9` + `F821`).
- New **`security-scan`** job: `bandit -lll` (high) blocking, `bandit -ll`
  (medium) advisory.
- `.github/dependabot.yml` covering pip, GitHub Actions, both Gradle roots and
  the vendored submodules.
- `SECURITY.md` — coordinated disclosure process, the invariants to attack,
  and the verification commands.
- `pyproject.toml` gains a `dev` extra (`pytest`, `ruff`, `bandit`).

### Tests & docs
- `tests/test_shugocore_server.py`: new `ServerHardeningTestCase` (auth,
  rate-limit 429, negative `Content-Length`, loopback-only CORS, fail-closed
  bind).
- `tests/test_agent_runtime.py`: new `TestPeerServerHardening` (validation,
  token gate, frame-clamp, live socketpair DoS bound, dispatch filtering).
- `tests/test_model_execution_stress.py`: new `TestBackendEgressHardening`
  (scheme/credentials/host rejection, redirect refusal, oversized-body refusal).
- `tests/test_security.py`: bearer/basic redaction, query-credential redaction,
  invisible-Unicode stripping, SecretResolver name guard, malformed-audit-line
  logging.
- `tests/test_kv_mesh.py`: `KVTransportAdapterTest` pins the previously-broken
  DDS adapter path (advertise/register, heartbeat, evict, inbound guards).
- `tests/test_mobile.py`: audit-write failure must be logged, not swallowed.
- `docs/desktop_server.md` + README: document `SHUGOCORE_SERVER_TOKEN`,
  `--allow-unauthenticated` and the rate-limit flags.

### Known advisory follow-ups (bandit medium, non-blocking)
- `B608` string-based SQL construction in `pg_memory.py` (to be reviewed for
  parameterization) and `B104` bind-all in `agent_runtime.py` (documented
  opt-in). High-severity bandit is clean.

## [1.29.0] - 2026-09-12 — native NRR neural rendering on Android (upstream Phase 13 port)

Upstream NRR's C++ runtime now runs on-device: a paired node can execute
`nrr_render` natively instead of answering `not_supported`. The Python `nrr/`
contract layer stays binary-free.

### Vendored NRR C++ runtime (`platforms/android/app/src/main/cpp/nrr`) — submodule
- `SamurAI-Official/NRR` added as a git submodule (mirrors the llama.cpp
  pattern). The Android build compiles its C++ runtime + Phase 13 mobile
  sources into `nrr_runtime` (static), so a paired node can execute
  `nrr_render` natively instead of answering `not_supported`.
- Compiled from our own `CMakeLists.txt` rather than upstream's
  `add_subdirectory()`: upstream gates `NRR_HAVE_ONNXRUNTIME` on the Windows
  layout (`lib/onnxruntime.lib` + `.dll`) with non-`CACHE` `set()`s, so the
  ORT-enabled path is unreachable on Android from outside.
- ONNX Runtime is **required**: upstream's ORT-less "placeholder" path does
  not compile (`onnx_runtime.cpp` uses `provider_note_`, which
  `onnx_runtime.h` declares only under `NRR_HAVE_ONNXRUNTIME`). Configure
  fails fast with instructions instead of degrading misleadingly.

### ONNX Runtime from the official Maven AAR (`scripts/fetch_ort_android.sh`)
- Extracts the version-matched C headers *and* per-ABI `libonnxruntime.so`
  from `onnxruntime-android` (default 1.23.2; ~19 MB arm64-v8a, ~22 MB
  x86_64). Gitignored; regenerate with the script.
- Gradle packages that directory via `jniLibs.srcDirs`, so the `.so` CMake
  links is the exact file shipped in the APK.

### Upstream bugs found (Phase 13 had never been compiled)
- ORT-less path does not compile (`provider_note_` outside its `#ifdef`).
- ORT path is Windows-only (`windows.h`/`MultiByteToWideChar` inside the
  `NRR_HAVE_ONNXRUNTIME` block); patched to use `ORTCHAR_T` semantics.
- `nrr_power_manager.cpp` calls four Android hooks
  (`android_get_battery_level`, `android_get_battery_status`,
  `android_get_thermal_headroom`, `android_is_low_power`) that upstream
  neither declares nor defines. Supplied by ShugoCore
  (`nrr_android_platform.cpp`, sysfs battery/thermal reads).
- macOS is unbuildable (`backend_apple.cpp` pulls ObjC framework headers into
  a `.cpp`; `backend_apple.h` lacks the declarations `backend_registry.cpp`
  references under `__APPLE__`). Host CI compiles those TUs with `__APPLE__`
  undefined, matching the Windows/Linux behavior upstream supports.
- **Vendor auto-selection trap:** `is_supported()` never probes for the
  vendor GPU and Adreno (60) outranks CPU (10), so auto-selection resolves to
  Adreno on every SoC and then fails detection, killing `nrr_device_create()`.
  All callers must pass an explicit `preferred_backend`.

### Durable upstream patches (`patches/nrr/`, `scripts/apply_nrr_patches.sh`)
- The parent repo records only the submodule SHA, so the required in-submodule
  fixes would be lost on a fresh clone. They now live as an idempotent patch
  series re-applied after `git submodule update --init`; the port-wiring test
  fails if the series or the apply script goes missing.

### App-side native layers
- `nrr_jni.cpp` (`libnrr_jni.so`): JNI bindings for create/destroy session,
  `renderFrame`, capabilities and power status — same handle-passing and
  `__ANDROID__`-guard conventions as `llama_jni.cpp`.
- `NRRBridge.kt` in `.../inference/`, mirroring `LlamaCppBridge`.
- `nrr/adapter.py`: `NRRRenderWorker` (injectable `frame_source` + `renderer`;
  the step up from `worker_stub`) and `android_native_worker()` — a factory
  that binds the worker to `NRRBridge` through Chaquopy and returns `None`
  when unavailable, so callers keep the fail-closed stub.
- `NRRRenderWorker.compute_caps()` advertises `nrr_render` only when the
  worker can serve it, so `nodes_for_workload()` routing never sends pixel
  work to a node that would answer `not_supported`.

### Reachable from inside the app
- `ShugoCoreService` builds the NRR bridge at startup, attaches it to
  `LocalApiServer`, registers it with the Python agent
  (`register_nrr_renderer`) and runs a startup self-test.
- The model ships in `assets/nrr/` and `NRRBridge.extractAssetModel()` copies it
  into `filesDir` (ONNX Runtime needs a real path; `*.onnx` is `noCompress` so
  the length check can skip a redundant copy).
- `LocalApiServer` serves `GET /nrr/info` and `GET|POST /nrr/selftest`.
- `shugocore_agent.py` gains `register_nrr_renderer()`, `nrr_status_json()`
  and `nrr_worker()`; `nrr/adapter.py::android_native_worker` now binds to the
  registered renderer object (the device owns construction, since it needs a
  Context and the packaged asset).
- **Verified in-app** on the A51: `NRR self-test ok: 768 bytes, 17 distinct,
  4.3ms` logged by `ShugoCoreService` on a clean start (`768 == 16*16*3`, and
  the distinct-byte count matches `nrr_probe`). New `nrr_native_ready` phase in
  `tests/android_device_smoke.py` asserts it across a restart.
- **Verified with a LIVE camera frame**: the camera came up on the A51 and the
  service logged
  `NRR camera frame render ok: 320x426 -> 408960 bytes, 88 distinct, 11.0ms`
  — `408960 == 320*426*3` (RGB8) and 88 distinct byte values, i.e. a real
  image through ONNX Runtime, not a synthetic fill. `nrr_camera_render` is now
  a harness phase.
- Full harness re-run after the port: **11/11 phases pass**, verdict STABLE
  (1 round, 1 attempt, no flaky phases) on 1.29.0 / versionCode 17 — the native
  runtime does not regress the existing on-device agent.

### Live camera frames — wired, with a device limitation found
- `PerceptionState` now carries the latest analysed camera frame as RGBA8
  (filled by `VisionProvider` from the bitmap it already decodes for face
  detection: one `getPixels` per *analysed* frame, not per camera frame).
  `ShugoCoreService` renders the freshest frame in a bounded startup probe;
  pixels stay in-process.
- **The A51 test unit cannot open its front camera.** `dumpsys media.camera`
  records `Camera "1" disabled by policy` for every attempt; the system camera
  app only works by falling back to the back camera. `DEFAULT_FRONT_CAMERA`
  resolves to camera 1, so `VisionProvider` has never received a frame on this
  hardware — and the failure was **silent** (binding "succeeds", then the
  camera closes asynchronously).
- `VisionProvider` now logs that once, with the CameraX state, so the operator
  can see vision-backed perception is unavailable instead of guessing:
  `no camera frames after 20s (state=CLOSED, error=null)`.
- Deliberately unchanged: the front-camera selector. Person-presence semantics
  depend on the front camera, and silently switching to the back camera would
  report the room as the user — a product decision, not a porting one.
- **Surfaced in the UI too, not just logcat**: `VisionProvider.cameraFault` is
  published to `PerceptionState` and consumed by
  `SensorCapabilityManager.detail("camera")`, so the SENSORS row reads
  `⚠ Granted · not delivering · camera not delivering frames (state=CLOSED...)`
  instead of a healthy `✓ Granted · ○ Idle`. Reported only once the fault is
  established, and cleared when frames arrive or the provider stops.

### Tests
- `tests/test_nrr_android_port.py`: guards that the camera fault is published,
  exposed via `PerceptionState.unavailableVisionNote()`, surfaced by the
  SENSORS tab, and that the camera path stays wired end-to-end to the NRR
  render probe.
- `tests/test_android_lifecycle_stress.py`: fixed the last flaky test in the
  suite. `test_200_full_lifecycle_cycles` asserted
  `threading.active_count() == baseline`, which also fails when an *unrelated*
  background thread finishes during the 200 cycles (observed `1 != 2`, roughly
  one full-suite run in three). The intent is "no leak", so it now asserts
  `<= baseline` — a leak still fails, an unrelated thread exiting no longer
  does. Three consecutive full-suite runs are now green (934 tests).

### Version
- `versionCode` 16 -> 17, `versionName` 1.28.2 -> 1.29.0 in version.py,
  pyproject.toml, build.gradle and the README badge.

### Build size note
- The NRR runtime ships `libonnxruntime.so` (19.3 MB arm64-v8a / 23.2 MB
  x86_64) plus the model asset, so both debug and release APKs grow by roughly
  42 MB across the two filtered ABIs. `libnrr_jni.so` itself is only ~192 KB
  (stripped). Mitigations if this matters: ABI splits, or `onnxruntime-mobile`
  (smaller, but frozen at 1.18.0 with reduced operator coverage).
- Verified: both `assembleDebug` and `assembleRelease` succeed; the release APK
  carries `libnrr_jni.so`, `libonnxruntime.so` (both ABIs) and
  `assets/nrr/nrr_upscaler_v0.1.onnx`, with `libllama_jni.so` unchanged at
  5.0 MB.

### `nrr_probe` diagnostic — verified on device
- End-to-end NRR check (device → model → RGBA8 texture → `execute_frame` →
  download) printing JSON; runs on-device via adb without Gradle.
- Galaxy A51 (Exynos 1380, arm64-v8a): `onnxruntime: true`,
  `active_backend: "CPU"`, `model_loaded: true`, `execute_frame: true`,
  `output_distinct_bytes: 17`, `ok: true`. Capabilities report honestly
  (`active_ep: "CPU"`, `neural_acceleration: "absent"`) — NNAPI is *not*
  advertised, because upstream's `apply_provider()` only understands
  `cpu`/`cuda`/`directml` and would otherwise claim an EP it never appended.
- See `docs/nrr_android_port.md`.
- Full app APK (debug) built and installed on the A51; the 9-phase device
  harness passes 9/9 in a cold-start round (`recursive_loop_training.py`,
  verdict STABLE, 1 attempt), so the added native runtime does not regress the
  existing on-device agent.

### Tests
- `tests/test_nrr_android_port.py`: structural guards for the port wiring
  (submodule, hard ORT requirement, upstream patches, Gradle packaging).

## [1.28.2] - 2026-09-09 — NRR contract shim + capability routing + 16 KB budget + governor + kv-mesh transport

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

### Android backend delegation URL fix (`android_inference.py`, `decision_engine.py`)
- The Android backend was constructed with a `base_url` kwarg while the class signature takes `api_url` — delegation silently fell back to the default endpoint. Fixed at both call sites; mirrors updated.

### Device smoke harness hardening (`tests/android_device_smoke.py`) — 9/9 verified on both devices with this harness
- **`personality_growth_log` saturation fix** — device models reached generation 11 with warmth pinned at 1.0, and the positive-only praise mix clamped every delta to 0.0 (drift NONE on repeat runs). The growth drive is now an **alternating CRITICIZE/PRAISE burst loop** that picks its initial direction from the live snapshot's room-to-move; every turn hits a real lexicon pattern in `personality/growth.py`. Verified: non-zero drift (0.061 / 0.078) with non-zero trait deltas on both devices.
- **`full_teardown_announced` ack race fix** — the fact-seed ack queued behind pending ~50 s-cadence model decisions after the cadence measurement, blowing the fixed 125 s budget (device-dependent flake). The first ack window now drains the decision queue before its deadline starts.

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