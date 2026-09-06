"""Human Interaction contract (v1.12 — Interaction Foundation).

The provider-side edge of the human-facing sensory interface: the event
schema and the bounded bus that carries observations from human-interaction
providers (camera, microphone, UI) into the agent.

Non-negotiable architectural rule: this module is a PROVIDER contract. It
must never be imported by the decision core (decision_engine, subconscious,
execution_layer, policy) — the engine receives human context only as task
``context`` data, exactly like device telemetry. The import guard in
tests/test_human_interaction.py fails the suite if that ever changes.

Privacy: every observation is ``privacy_scope="local"`` by construction —
payloads are sanitized and bounded on ingest and nothing here has egress.
"""
import threading
import time
import uuid
from collections import deque
from typing import Any, Callable, Dict, List, Optional, Tuple

from security import sanitize_text

# Observation types (the contract the user-approved plan defines).
OBSERVATION_TYPES = ("visual", "speech", "presence", "gesture", "interaction")
# Response types (reserved for v1.15/1.16 agent capabilities; defined now so
# the schema is complete and stable before any emitter exists).
RESPONSE_TYPES = ("speech", "visual", "action", "acknowledgement")
PRIVACY_SCOPES = ("local", "ephemeral")

# Presence lifecycle events emitted by the bus when the presence state
# machine transitions (debounced — see InteractionBus._DEBOUNCE_S).
USER_PRESENT_EVENT = "USER_PRESENT"
USER_LEFT_EVENT = "USER_LEFT"
USER_RETURNED_EVENT = "USER_RETURNED"
PRESENCE_EVENTS = (USER_PRESENT_EVENT, USER_LEFT_EVENT, USER_RETURNED_EVENT)

USER_ABSENT = "user_absent"
USER_PRESENT = "user_present"

_MAX_SOURCE_LEN = 40
_MAX_PAYLOAD_VALUE_LEN = 160
_MAX_PAYLOAD_KEY_LEN = 40
_MAX_PAYLOAD_KEYS = 12
_MAX_RESPONSE_CONTENT_LEN = 300
# v1.16 multimodal: bounded conversation memory + question/answer pairing.
_MAX_CONVERSATION_TURNS = 12
_MAX_CONTEXT_TURNS = 6
_ANSWER_TTL_S = 120.0
# v1.17 closed-loop validation: bounded record of completed question/answer
# round trips (the Record stage of the conversational loop).
_MAX_CONVERSATION_EVENTS = 8


def _clean_float(value: Any) -> Optional[float]:
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    if value != value or value in (float("inf"), float("-inf")):
        return None
    return value


def truncate_sentence(text: str, limit: int = 400) -> str:
    """Trim spoken text to ``limit`` WITHOUT cutting a sentence or a word:
    prefer the last sentence end inside the limit, else the last word
    boundary, else the hard limit. The v1.18 voice rule: the agent never
    speaks a fragment it did not choose. Never raises; never empty-wrongs
    (a clean empty string in, empty string out)."""
    cleaned = sanitize_text(str(text or ""), max(limit * 2, limit))
    if len(cleaned) <= limit:
        return cleaned
    cut = cleaned[:limit]
    for sep in (". ", "! ", "? ", ".", "!", "?"):
        pos = cut.rfind(sep)
        if pos > limit // 2:
            return cut[:pos + 1].rstrip()
    pos = cut.rfind(" ")
    if pos > limit // 2:
        return cut[:pos].rstrip()
    return cut.rstrip()


def _sanitize_payload(payload: Any) -> Dict[str, Any]:
    """Bound + flatten a payload into primitives. Junk never reaches memory:
    non-dict payloads are wrapped under ``text``/``value``, strings pass
    through sanitize_text, every other type is stringified and trimmed."""
    if payload is None:
        return {}
    if isinstance(payload, bool) or isinstance(payload, (int, float)):
        number = _clean_float(payload)
        if number is None:
            return {}
        return {"value": payload}
    if not isinstance(payload, dict):
        return {"text": sanitize_text(payload, _MAX_PAYLOAD_VALUE_LEN)}
    clean: Dict[str, Any] = {}
    for key, value in list(payload.items())[:_MAX_PAYLOAD_KEYS]:
        clean_key = sanitize_text(str(key), _MAX_PAYLOAD_KEY_LEN)
        if not clean_key:
            continue
        if isinstance(value, bool):
            clean[clean_key] = value
        elif isinstance(value, (int, float)):
            number = _clean_float(value)
            if number is not None:
                clean[clean_key] = value
        elif isinstance(value, str):
            clean[clean_key] = sanitize_text(value, _MAX_PAYLOAD_VALUE_LEN)
        else:
            clean[clean_key] = sanitize_text(str(value), _MAX_PAYLOAD_KEY_LEN)
    return clean


class HumanObservation:
    """One observation of a human, from any provider, on any device.

    The agent does not care whether "hello" came from a tablet microphone or
    a headset: it receives HumanObservation(type=speech). It does not care
    whether the camera is a phone front camera or a robot camera: it
    receives HumanObservation(type=visual)."""

    def __init__(self, type: str, source: str, payload: Any = None,
                 confidence: float = 1.0, timestamp: Optional[float] = None,
                 privacy_scope: str = "local",
                 observation_id: Optional[str] = None):
        self.type = str(type).strip().lower()
        self.source = sanitize_text(str(source or ""), _MAX_SOURCE_LEN)
        self.payload = _sanitize_payload(payload)
        self.observation_id = str(observation_id) if observation_id else None
        conf = _clean_float(confidence)
        if conf is None:
            # 0.0 and 1.0 are meaningful values; unparseable confidence is
            # marked invalid instead of silently defaulted.
            self.confidence = 0.0
            self._confidence_invalid = True
        else:
            self.confidence = conf
            self._confidence_invalid = False
        ts = _clean_float(timestamp if timestamp is not None else time.time())
        self.timestamp = ts if ts is not None else time.time()
        self.privacy_scope = str(privacy_scope or "").strip().lower()

    def validate(self) -> Tuple[bool, str]:
        """Post-construction validation; returns (ok, reason)."""
        if self.type not in OBSERVATION_TYPES:
            return False, f"unknown observation type: {self.type!r}"
        if not self.source:
            return False, "empty source"
        if self._confidence_invalid or not 0.0 <= self.confidence <= 1.0:
            return False, f"confidence out of range: {self.confidence}"
        if self.privacy_scope not in PRIVACY_SCOPES:
            return False, f"unknown privacy scope: {self.privacy_scope!r}"
        return True, "ok"

    def to_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {"type": self.type, "source": self.source,
                "payload": dict(self.payload), "confidence": self.confidence,
                "timestamp": self.timestamp,
                "privacy_scope": self.privacy_scope}
        if self.observation_id:
            d["observation_id"] = self.observation_id
        return d

    @classmethod
    def from_dict(cls, data: Any) -> Tuple[Optional["HumanObservation"], str]:
        """Build from untrusted input; NEVER raises. Returns (obs, reason);
        obs is None when the input is unusable (the caller reports it)."""
        if not isinstance(data, dict):
            return None, "not an object"
        obs = cls(type=str(data.get("type") or ""),
                  source=str(data.get("source") or ""),
                  payload=data.get("payload"),
                  confidence=data.get("confidence", 1.0),
                  timestamp=data.get("timestamp"),
                  privacy_scope=str(data.get("privacy_scope") or "local"),
                  observation_id=str(data.get("observation_id")) if data.get("observation_id") else None)
        ok, reason = obs.validate()
        return (obs, reason) if ok else (None, reason)


class AgentResponse:
    """One agent response directed at a human (schema reserved for the
    v1.15/1.16 speech/action capabilities; carried by the same bus)."""

    def __init__(self, type: str, content: str = "", target: str = "",
                 priority: float = 0.5, expects_answer: bool = False,
                 response_id: Optional[str] = None,
                 in_response_to: Optional[str] = None):
        self.type = str(type).strip().lower()
        self.content = sanitize_text(str(content or ""),
                                     _MAX_RESPONSE_CONTENT_LEN)
        self.target = sanitize_text(str(target or ""), _MAX_SOURCE_LEN)
        prio = _clean_float(priority)
        self.priority = prio if prio is not None else 0.5
        self.response_id = str(response_id) if response_id else None
        self.in_response_to = str(in_response_to) if in_response_to else None
        # v1.16: a response that is a QUESTION (ask_user action) — the bus
        # pairs the next speech observation with it as the answer.
        self.expects_answer = bool(expects_answer)

    def validate(self) -> Tuple[bool, str]:
        if self.type not in RESPONSE_TYPES:
            return False, f"unknown response type: {self.type!r}"
        if not self.content:
            return False, "empty content"
        if not 0.0 <= self.priority <= 1.0:
            return False, f"priority out of range: {self.priority}"
        return True, "ok"

    def to_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {"type": self.type, "content": self.content,
                "target": self.target, "priority": self.priority,
                "expects_answer": self.expects_answer}
        if self.response_id:
            d["response_id"] = self.response_id
        if self.in_response_to:
            d["in_response_to"] = self.in_response_to
        return d

    @classmethod
    def from_dict(cls, data: Any) -> Tuple[Optional["AgentResponse"], str]:
        if not isinstance(data, dict):
            return None, "not an object"
        resp = cls(type=str(data.get("type") or ""),
                   content=str(data.get("content") or ""),
                   target=str(data.get("target") or ""),
                   priority=data.get("priority", 0.5),
                   expects_answer=bool(data.get("expects_answer", False)),
                   response_id=str(data.get("response_id")) if data.get("response_id") else None,
                   in_response_to=str(data.get("in_response_to")) if data.get("in_response_to") else None)
        ok, reason = resp.validate()
        return (resp, reason) if ok else (None, reason)


class InteractionBus:
    """Bounded, thread-safe ring of human observations + the presence
    state machine. Mirrors the house LogBus pattern (capacity-bounded,
    synchronized, listener callbacks invoked outside the lock).

    Presence: every observation implies the user is present EXCEPT an
    explicit ``presence`` observation carrying ``present: false``. Presence
    transitions are debounced (``_DEBOUNCE_S``) so a flapping provider
    cannot flood the journal with USER_PRESENT/USER_LEFT pairs."""

    _DEBOUNCE_S = 1.5

    def __init__(self, capacity: int = 256,
                 clock: Optional[Callable[[], float]] = None):
        self._clock = clock or time.time
        self._lock = threading.RLock()
        self._buffer: deque = deque(maxlen=max(16, int(capacity)))
        self._listeners: List[Callable[[Dict[str, Any]], None]] = []
        self._presence = USER_ABSENT
        self._has_been_present = False
        self._presence_changed_at = 0.0
        self._counts: Dict[str, int] = {t: 0 for t in OBSERVATION_TYPES}
        self._presence_event_counts = {e: 0 for e in PRESENCE_EVENTS}
        self._rejected = 0
        self._last_timestamp = 0.0
        self._seq = 0
        # v1.19 causal ID chain: every observation, response, and turn
        # carries a sequential id for deterministic trace reconstruction.
        self._conversation_id: str = "conv-" + uuid.uuid4().hex[:12]
        self._obs_seq = 0
        self._rsp_seq = 0
        self._turn_seq = 0
        self._current_turn_id: Optional[str] = None
        # AgentResponse side of the contract (v1.15 speech output): what the
        # agent last said to the human, and how many responses it emitted.
        self._last_response: Optional[Dict[str, Any]] = None
        self._response_count = 0
        # v1.16 multimodal: bounded two-sided conversation memory (human
        # speech turns + agent speech turns, chronological) and the pending
        # question awaiting a spoken answer.
        self._conversation: deque = deque(maxlen=_MAX_CONVERSATION_TURNS)
        self._last_question: Optional[str] = None
        self._last_answer: Optional[str] = None
        self._pending_question: Optional[Dict[str, Any]] = None
        # v1.17: completed question/answer round trips (bounded) + when the
        # agent last emitted any response (pipeline-health evidence).
        self._conversation_events: deque = deque(
            maxlen=_MAX_CONVERSATION_EVENTS)
        self._last_response_ts: Optional[float] = None

    # -- ingestion ------------------------------------------------------------

    def publish(self, observation: Any) -> Tuple[bool, str]:
        """Validate + record one observation. Returns (accepted, detail):
        detail is the reason when rejected, the presence event name when a
        transition fired, or "ok"."""
        if not isinstance(observation, HumanObservation):
            with self._lock:
                self._rejected += 1
            return False, "not a HumanObservation"
        ok, reason = observation.validate()
        if not ok:
            with self._lock:
                self._rejected += 1
            return False, reason

        now = self._clock()
        present_implied = not (
            (observation.type == "presence"
             and observation.payload.get("present") is False)
            or (observation.type == "visual"
                and observation.payload.get("person_present") is False))
        transcript = (str(observation.payload.get("transcript") or "").strip()
                      if observation.type == "speech" else "")
        presence_event = ""
        with self._lock:
            self._seq += 1
            entry = observation.to_dict()
            # v1.19: assign causal IDs to this observation.
            self._obs_seq += 1
            obs_id = f"obs-{self._obs_seq}"
            entry["observation_id"] = obs_id
            entry["conversation_id"] = self._conversation_id
            observation.observation_id = obs_id
            # Speech starts or continues a turn.
            if transcript:
                if self._current_turn_id is None:
                    self._turn_seq += 1
                    self._current_turn_id = f"turn-{self._turn_seq}"
                entry["turn_id"] = self._current_turn_id
            # v1.16 question/answer pairing: a speech observation carrying
            # words while a question is pending (fresh within _ANSWER_TTL_S)
            # is that question's answer.
            if transcript:
                question = self._pending_question
                if (question is not None
                        and now - question["ts"] <= _ANSWER_TTL_S):
                    entry["payload"]["answer_to"] = question["text"]
                    self._last_answer = transcript
                    # v1.17: the loop closed — record the round trip with
                    # its latency (bounded; drained to the journal by the
                    # agent shell as metadata only, never raw words).
                    self._conversation_events.append({
                        "question": question["text"],
                        "answer": transcript,
                        "round_trip_s": round(now - question["ts"], 2),
                        "ts": entry["timestamp"],
                        "conversation_id": self._conversation_id,
                        "turn_id": self._current_turn_id,
                        "observation_id": obs_id,
                    })
                self._pending_question = None
                self._conversation.append({"role": "human",
                                           "text": transcript,
                                           "ts": entry["timestamp"]})
            entry["seq"] = self._seq
            self._buffer.append(entry)
            self._counts[observation.type] = (
                self._counts.get(observation.type, 0) + 1)
            self._last_timestamp = entry["timestamp"]
            if abs(now - self._presence_changed_at) >= self._DEBOUNCE_S:
                if present_implied and self._presence == USER_ABSENT:
                    self._presence = USER_PRESENT
                    self._presence_changed_at = now
                    presence_event = (USER_RETURNED_EVENT
                                      if self._has_been_present
                                      else USER_PRESENT_EVENT)
                    self._has_been_present = True
                    if presence_event == USER_RETURNED_EVENT:
                        # Rotate the conversation id on return — a new
                        # conversational context after an absence.
                        self._conversation_id = "conv-" + uuid.uuid4().hex[:12]
                        self._turn_seq = 0
                        self._current_turn_id = None
                elif not present_implied and self._presence == USER_PRESENT:
                    self._presence = USER_ABSENT
                    self._presence_changed_at = now
                    presence_event = USER_LEFT_EVENT
            if presence_event:
                self._presence_event_counts[presence_event] = (
                    self._presence_event_counts.get(presence_event, 0) + 1)
                entry["presence_event"] = presence_event
            listeners = list(self._listeners)
        for listener in listeners:
            try:
                listener(entry)
            except Exception:
                pass
        return True, presence_event or "ok"

    def record_rejected(self) -> None:
        """Count a rejection that happened BEFORE the bus (e.g. a payload
        that never parsed into a HumanObservation). Keeps the stats honest:
        'rejected' means 'refused at the contract boundary'."""
        with self._lock:
            self._rejected += 1

    def record_agent_response(self, response: Any) -> Tuple[bool, str]:
        """Record one AgentResponse — the agent's output toward the human
        (v1.15: the speak action). Same honesty rules as observations:
        validated, sanitized, bounded, never raising."""
        if not isinstance(response, AgentResponse):
            with self._lock:
                self._rejected += 1
            return False, "not an AgentResponse"
        ok, reason = response.validate()
        if not ok:
            with self._lock:
                self._rejected += 1
            return False, reason
        with self._lock:
            # v1.19: assign causal response id.
            self._rsp_seq += 1
            rsp_id = f"rsp-{self._rsp_seq}"
            response.response_id = rsp_id
            response.in_response_to = self._current_turn_id
            self._last_response = response.to_dict()
            self._response_count += 1
            self._last_response_ts = self._clock()
            if response.type == "speech" and response.content:
                self._conversation.append({"role": "agent",
                                           "text": response.content,
                                           "ts": self._clock(),
                                           "response_id": rsp_id})
                # The turn is complete — clear for the next one.
                self._current_turn_id = None
                if response.expects_answer:
                    self._last_question = response.content
                    self._pending_question = {"text": response.content,
                                              "ts": self._clock()}
        return True, "ok"

    def expect_answer(self, question: str) -> bool:
        """Mark a question the agent just asked as awaiting a spoken answer
        (v1.16 ask_user path for responses not recorded through
        record_agent_response). The next speech observation within
        _ANSWER_TTL_S is paired with it. Bounded + sanitized like everything
        else here; never raises."""
        text = sanitize_text(str(question or ""), _MAX_RESPONSE_CONTENT_LEN)
        if not text:
            return False
        with self._lock:
            self._last_question = text
            self._pending_question = {"text": text, "ts": self._clock()}
        return True

    def drain_conversation_events(self) -> List[Dict[str, Any]]:
        """Pop all completed question/answer round trips (oldest first) for
        the agent shell to journal (v1.17 Record stage). The bus keeps the
        words; the journal gets only what the shell chooses to record.
        Bounded by construction; never raises."""
        with self._lock:
            events = list(self._conversation_events)
            self._conversation_events.clear()
        return events

    def pipeline_health(self, model_ready: Optional[bool] = None,
                        tts_attached: Optional[bool] = None,
                        memory_ok: Optional[bool] = None) -> Dict[str, Any]:
        """One liveness view over the closed loop (v1.17 validation — the
        'SHUGOCORE LIVE' monitor). Each stage reports ``ok`` (fresh
        evidence), ``stale`` (evidence expired), ``down`` (agent-side truth
        says the provider is absent) or ``unknown`` (no evidence yet — never
        fabricated). Sensor stages are judged from the bus's own buffer;
        model/tts/memory truths are passed in by the shell, keeping this a
        provider-side module (the core never imports it)."""
        with self._lock:
            now = self._clock()

            def _stage(max_age_s: float, last_ts: Optional[float]) -> str:
                if last_ts is None:
                    return "unknown"
                return "ok" if now - last_ts <= max_age_s else "stale"

            last_visual_ts = next(
                (e.get("timestamp") for e in reversed(self._buffer)
                 if e.get("type") == "visual"), None)
            last_speech_ts = next(
                (e.get("timestamp") for e in reversed(self._buffer)
                 if e.get("type") == "speech"), None)
            stages = {
                "sensors": _stage(60.0, self._last_timestamp or None),
                "vision": _stage(60.0, last_visual_ts),
                "hearing": _stage(120.0, last_speech_ts),
                "speech": ("unknown" if tts_attached is None
                           else ("ok" if tts_attached else "down")),
                "model": ("unknown" if model_ready is None
                          else ("ok" if model_ready else "down")),
                "memory": ("unknown" if memory_ok is None
                           else ("ok" if memory_ok else "down")),
            }
            ok_count = sum(1 for v in stages.values() if v == "ok")
            bus_evidence = any(
                stages[s] != "unknown" for s in ("sensors", "vision", "hearing"))
            if any(v == "down" for v in stages.values()):
                overall = "down"
            elif not bus_evidence:
                overall = "unknown"
            elif ok_count >= 3:
                overall = "ok"
            else:
                overall = "stale"
            return {"stages": stages, "overall": overall,
                    "round_trips": len(self._conversation_events)}

    def add_listener(self, listener: Callable[[Dict[str, Any]], None]) -> None:
        with self._lock:
            self._listeners.append(listener)

    def remove_listener(
            self, listener: Callable[[Dict[str, Any]], None]) -> None:
        with self._lock:
            self._listeners = [cb for cb in self._listeners
                               if cb is not listener]

    # -- read side -------------------------------------------------------------

    def snapshot(self) -> List[Dict[str, Any]]:
        with self._lock:
            return [dict(entry) for entry in self._buffer]

    def presence(self) -> str:
        with self._lock:
            return self._presence

    def human_context(self) -> Dict[str, Any]:
        """The view of the human the agent reasons over (task context data —
        never an import of the decision core)."""
        with self._lock:
            now = self._clock()
            age = (now - self._last_timestamp) if self._last_timestamp else None
            speech_recent = any(
                entry.get("type") == "speech"
                and now - entry.get("timestamp", 0.0) <= 30.0
                for entry in self._buffer)
            last_visual = next((e for e in reversed(self._buffer)
                                if e.get("type") == "visual"), None)
            vision_recent = bool(
                last_visual is not None
                and now - last_visual.get("timestamp", 0.0) <= 30.0)
            person_present = self._last_person_present(now)
            # v1.16 multimodal user-state fusion: one view the agent reasons
            # over. Gaze/attention/environment are reserved (None) until the
            # XR providers arrive — the schema is stable before the sources.
            recent_conf = [e.get("confidence") for e in self._buffer
                           if now - e.get("timestamp", 0.0) <= 30.0
                           and isinstance(e.get("confidence"), (int, float))]
            conversation = [dict(t) for t in list(self._conversation)[
                -_MAX_CONTEXT_TURNS:]]
            pending = self._pending_question
            return {
                "presence": self._presence,
                "seconds_since_last_observation":
                    round(age, 1) if age is not None else None,
                "observations_recorded": sum(self._counts.values()),
                "speech_recent": speech_recent,
                "vision_recent": vision_recent,
                "person_present": person_present,
                "conversation": conversation,
                "pending_question": (
                    pending["text"]
                    if pending is not None
                    and now - pending["ts"] <= _ANSWER_TTL_S else None),
                # v1.19 causal ID chain: trace the decision back to
                # the exact observation that triggered it.
                "conversation_id": self._conversation_id,
                "current_turn_id": self._current_turn_id,
                # v1.20 intent extraction: lightweight subject/verb from the
                # most recent human utterance (soft signal for the model).
                "intent_subject": (
                    conversation[-1]["text"].strip().lower().split()[-1]
                    if conversation
                    and conversation[-1].get("role") == "human"
                    and conversation[-1].get("text")
                    and len(conversation[-1]["text"].strip().split()) >= 3
                    else None),
                "intent_verb": (
                    (lambda w: w[1] if w[0] in ("can", "please", "i", "shugo") else w[0])(
                        conversation[-1]["text"].strip().lower().split()
                    )
                    if conversation
                    and conversation[-1].get("role") == "human"
                    and conversation[-1].get("text")
                    and len(conversation[-1]["text"].strip().split()) >= 3
                    else None),
                "user_context": {
                    "person_present": person_present,
                    "speech_recent": speech_recent,
                    "vision_recent": vision_recent,
                    "last_transcript": (
                        conversation[-1]["text"]
                        if conversation
                        and conversation[-1]["role"] == "human" else None),
                    # v1.19 multimodal fusion slots: filled from provider
                    # data when available (gaze from camera landmarks,
                    # attention from speech transcript context,
                    # environment from device telemetry). Honest None
                    # until the real sources are wired.
                    "gaze": (
                        last_visual.get("payload", {}).get("gaze_direction")
                        if last_visual
                        and last_visual.get("payload", {}).get("gaze_direction")
                        else None),
                    "attention_target": (
                        conversation[-1]["text"]
                        if conversation
                        and conversation[-1].get("role") == "human"
                        and conversation[-1].get("text")
                        # Named-entity extraction is reserved for
                        # memory enrichment; pass the raw text here.
                        else None),
                    "environment": None,
                    "confidence": (round(sum(recent_conf) / len(recent_conf), 2)
                                   if recent_conf else None),
                },
            }

    def _last_person_present(self, now: float) -> Optional[bool]:
        """person_present from the most recent visual observation when it is
        fresh (< 60s); None when vision has not reported or went stale."""
        last_visual = next((e for e in reversed(self._buffer)
                            if e.get("type") == "visual"), None)
        if last_visual is None or now - last_visual.get("timestamp", 0.0) > 60.0:
            return None
        value = last_visual.get("payload", {}).get("person_present")
        return bool(value) if value is not None else None

    def stats(self) -> Dict[str, Any]:
        with self._lock:
            now = self._clock()
            age = (now - self._last_timestamp) if self._last_timestamp else None
            last_speech = next((e for e in reversed(self._buffer)
                                if e.get("type") == "speech"), None)
            return {
                "presence": self._presence,
                "observations": sum(self._counts.values()),
                "by_type": dict(self._counts),
                "presence_events": dict(self._presence_event_counts),
                "rejected": self._rejected,
                "buffered": len(self._buffer),
                "last_observation_age_s":
                    round(age, 1) if age is not None else None,
                "person_present": self._last_person_present(now),
                "last_transcript": (
                    str(last_speech.get("payload", {}).get("transcript"))
                    if last_speech is not None
                    and last_speech.get("payload", {}).get("transcript")
                    else None),
                # Speech output truth: what the agent last said (None until
                # a speak action has actually executed).
                "last_spoken": (
                    self._last_response.get("content")
                    if self._last_response is not None
                    and self._last_response.get("type") == "speech"
                    else None),
                "agent_responses": self._response_count,
                # v1.16 conversation truth: the last asked question, the last
                # heard answer, and how many bounded turns are held.
                "last_question": self._last_question,
                "last_answer": self._last_answer,
                "conversation_turns": len(self._conversation),
                # v1.19 causal ID chain: the current conversation, observation,
                # turn, and response ids for trace reconstruction.
                "conversation_id": self._conversation_id,
                "current_turn_id": self._current_turn_id,
                "last_response_id": (
                    self._last_response.get("response_id")
                    if self._last_response else None),
                # v1.17 closed-loop truth: completed round trips and the
                # last one (question/answer/latency), for UI + tests.
                "conversation_events": len(self._conversation_events),
                "last_conversation_event": (
                    dict(self._conversation_events[-1])
                    if self._conversation_events else None),
            }
