# ShugoCore v1.30.5 — the conversational loop closes: tool routing, honest measurements, ask_user

## Why this release exists

Measured on the A51 (SM-S515DL) and Tab S9 FE (SM-X518U) on 2026-09-20, the
reported symptom ("it asks a question, then basic things like *what time is it*
and *what is the temperature* get no proper tool use or response") turned out to
be three separable defects.

### 1. A question's phrasing decided whether a tool was reached

`IntentParser` promotes only a literal phrase list to COMMAND, and the command
block runs only for `intent == command`. Everything else is a QUESTION and went
to the language model:

| phrasing | before | after |
|---|---|---|
| `what time is it` | `time_report` (tool called TWICE) | `time_report` (once) |
| `what's the time` | language model | `time_report` |
| `tell me the time` | `search_initiated` ("I can search for that") | `time_report` |
| `do you have the time`, `current time` | language model | `time_report` |
| `what's the date`, `what day is it` | language model | `date_report` (`get_date` now reachable) |
| `what is the temperature` (+4 variants) | language model | `temperature_report` / honest `temperature_unavailable` |
| `what's my battery` | language model | `battery_check` |
| `what's the weather` | honest (unchanged) | honest (unchanged) |

The device log shows what the model did with them:
`"The temperature outside is currently 20 degrees Celsius."` and, in an older
window, a fabricated city — `"New York is experiencing a high-pressure system"`.

### 2. A turn could end with no spoken outcome at all

Injected `what's the time` was recorded as heard and then produced **no agent
turn, no tool answer, no fallback line** — silence. Every user turn now
produces exactly one spoken outcome: a blocked, empty, unusable or crashing
model decision degrades to an honest spoken line instead of nothing.

### 3. `ask_user` was fire-and-forget

- `_execute_ask_user` returned `{status, asked, delivered}` — success meant "TTS
  accepted the text". The answer was paired on the bus as *metadata only*
  (`answer_to`, one `conversation_event` with latency), and the agent never
  replied to it. Expiry at `_ANSWER_TTL_S` was silent.
- `ConversationManager.on_speak_end` had zero callers, so `ConversationState.
  WAITING` ("asked a question, listening for answer") was unreachable.
- The one closed loop that existed — timer clarification — only accepted digit
  answers: on device, `five minutes` produced *"I didn't catch that — could you
  say it again?"* while `5 minutes` produced *"Got it — timer set for 5
  minutes."* (confirmed against `timers.json`).

## What changed

- **`subsystems/intent.py`** — `IntentParser.tool_topic()`: phrasing-independent
  recognition of toolable questions (clock/date/battery/weather/temperature/
  sensors), bounded so chit-chat mentioning a keyword ("I feel cold today") is
  not hijacked.
- **`subsystems/command_router.py`** — `_categorize(verb, transcript,
  tool_topic)`: a topic from the intent layer wins, and the keyword rules became
  phrasing-independent. New `handle_date`, `handle_temperature`, `handle_sensors`
  handlers (registered), a single-call `handle_time` (the old shape executed
  `get_time` twice), an honest `handle_search` (no more "I can search for that"
  with no search backend — a non-lookup "tell/show" falls through to the model),
  and an honest no-tool battery fallback.
- **`subsystems/tools.py`** — `get_temperature`: a real reading or an honest
  refusal, never a guess. `<= 0` / missing / implausible means *no reading* (the
  A51 ThermalMonitor reports 0), ambient and device (CPU) readings are framed
  differently.
- **`subsystems/dialogue.py`** — clarification answers accept number words
  (`five minutes`, `a minute`) as well as digits, matching the timer handler.
- **`shugocore_agent.py`** —
  - thin honest ask_user loop: `_execute_ask_user` opens a question and returns
    `awaiting_answer` + `answer_ttl_s`; `_take_open_question` verifies the answer
    against the TTL and journals `question_answered` / `question_expired` (counts
    + round trip only — the words stay in the bounded bus); `_expire_open_question`
    runs every tick so an unanswered question is recorded, not forgotten;
  - toolable questions are re-routed through the command path before the model,
    so the model can never supply a measurement;
  - guaranteed single spoken outcome (`_speak_fallback`) on blocked/empty/
    unusable decisions and on handler exceptions;
  - `_guard_fabricated_measurement`: a model text containing a reading is
    replaced with an honest refusal;
  - `_speak_turn` brackets deterministic answers on the conversation state
    machine (`on_speak_begin` -> `on_speak_end`), so WAITING/IDLE are real;
  - an unknown command now returns an empty response so the conversational path
    handles it, instead of "I can't do that yet, but I'm learning!".
- **`prompts/builder.py`** — `build_conversational_prompt(..., answering=)` tells
  the model which question the utterance answers (only ever a *verified* pair).
- **`tests/test_android_tree_sync.py`** — the root↔bundled drift guard now
  recurses into subpackages, so a change to `subsystems/*` can no longer ship to
  the device stale while the guard passes.

## Tests

- New `tests/test_tool_routing.py` (21 tests): the phrasing matrix above, the
  parser↔router consistency invariant (every promoted command lands on a
  registered handler), one `get_time` call per query, temperature honesty
  (0/missing -> unavailable), the no-fabrication guard, word-number
  clarification, topic-detection precision.
- New `TestAskUserLoop` in `tests/test_speech_output.py` (9 tests): awaiting/
  answered/expired, the answer being actually answered, no model call for a
  toolable answer, single spoken outcome on unusable/crashing decisions, and the
  verified question reaching the prompt.


## Verified on device (A51 + Tab S9 FE, 1.30.5 / versionCode 22)

Both phones were upgraded in place (debug build, data preserved) and driven over
wireless adb. The same injections that failed before the fix now answer:

| device | injected | observed | before |
|---|---|---|---|
| Tab S9 FE | `what's the time` | agent turn `It's 01:33 PM.` (+5 s) | **no agent turn at all** |
| A51 | `what time is it` | `It's 01:39 PM.` (+16 s) | tool reachable, tool ran twice |
| A51 | `how hot is it` | `I can't measure a temperature here — this device has no ambient sensor…` (+13 s) | model answered (fabricated reading) |
| Tab S9 FE | `what is the temperature` | same honest refusal | model answered (fabricated reading) |
| A51 | `what's the weather` | `I don't have weather data yet…` (+22 s) | honest (unchanged) |
| A51 | `set a timer` → `five minutes` | `Timer for how long?` (+21 s) → `Got it — timer set for 5 minutes.` (+32 s) | `I didn't catch that — could you say it again?` |

The clarify result also fixed a second-order bug the measured loop latency exposed:
`DialogueState`'s clarification window was 90 s while the interaction bus's answer
TTL is 120 s, and the conversational fast path waits behind the loop's own model
decision (20-90 s) — so a spoken answer could be parsed *after* the window had
expired. Both windows are now 120 s, and `looks_like_answer` still rejects a
fresh intent arriving in that window.

Note on the 50 s windows in `tests/android_device_smoke.py`: a phrasing whose
answer arrives after the window looks like a failure even when the route is
correct — the conversational fast path runs on the agent tick, so an injected
transcript waits behind the loop's own model decision (20-90 s warm). The
weather phrasing, which answered in 1.6 s *before* this release, showed 0
`SPEAK:` lines in a 50 s window during the post-fix matrix, which is what pinned
the effect on the window/queue rather than on the routing.
## Second batch: the release gate itself (same release)

- **Device smoke phases** — `time_tool_query` (a RE-WORDED clock question must
  print the tool's `SPEAK:` line), `measurement_honesty` (a temperature question
  is answered by a tool or refused — never a bare invented number) and
  `ask_user_round_trip` (the agent's own question, detected through the bus's
  `pending_question`, gets an answered reply). All three pass on hardware.
- **Two harness bugs found while adding them**:
  - `measure_decision_cadence` matched only dotted timestamps, but `logcat -v
    brief` has no prefix and the engine's writers use a comma — so it silently
    returned its 50 s floor on every run, pinning every phase window to a value
    unrelated to the node. The Tab now reports a real **21.8 s** cadence.
  - A bounded warm-up (`await_warm_loop`) before phase 1, plus
    `await_conversation_idle` before the conversational phases: the agent handles
    one utterance per tick, so on a slow node injections queue and a phase could
    fail for backlog rather than behaviour.
- **Thermal reporting** — the run now prints the device's Android thermal status
  (0-6) with a warning at severe/critical. Measured: the A51 reached **status 4
  (critical)** after a long inference session, with decisions ~100 s apart and a
  warm-up that timed out at 306 s; the Tab, at status 2, warmed in 2-11 s. A
  phase failure on a throttled node says nothing about the behaviour under test.
- **`shugocore_server.py`** — `build_server` now binds `_BoundHTTPServer`
  (`request_queue_size = 128`, `daemon_threads`) instead of inheriting
  socketserver's default of 5. The fleet/approvals surfaces invite concurrent
  clients, and macOS resets a SYN that arrives while the accept queue is full —
  before the handler or the rate limiter runs. Covered by
  `TestListenBacklog` (attribute plus 16 simultaneous `/health` clients).

