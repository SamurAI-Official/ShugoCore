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


def _clean_float(value: Any) -> Optional[float]:
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    if value != value or value in (float("inf"), float("-inf")):
        return None
    return value


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
                 privacy_scope: str = "local"):
        self.type = str(type).strip().lower()
        self.source = sanitize_text(str(source or ""), _MAX_SOURCE_LEN)
        self.payload = _sanitize_payload(payload)
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
        return {"type": self.type, "source": self.source,
                "payload": dict(self.payload), "confidence": self.confidence,
                "timestamp": self.timestamp,
                "privacy_scope": self.privacy_scope}

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
                  privacy_scope=str(data.get("privacy_scope") or "local"))
        ok, reason = obs.validate()
        return (obs, reason) if ok else (None, reason)


class AgentResponse:
    """One agent response directed at a human (schema reserved for the
    v1.15/1.16 speech/action capabilities; carried by the same bus)."""

    def __init__(self, type: str, content: str = "", target: str = "",
                 priority: float = 0.5):
        self.type = str(type).strip().lower()
        self.content = sanitize_text(str(content or ""),
                                     _MAX_RESPONSE_CONTENT_LEN)
        self.target = sanitize_text(str(target or ""), _MAX_SOURCE_LEN)
        prio = _clean_float(priority)
        self.priority = prio if prio is not None else 0.5

    def validate(self) -> Tuple[bool, str]:
        if self.type not in RESPONSE_TYPES:
            return False, f"unknown response type: {self.type!r}"
        if not self.content:
            return False, "empty content"
        if not 0.0 <= self.priority <= 1.0:
            return False, f"priority out of range: {self.priority}"
        return True, "ok"

    def to_dict(self) -> Dict[str, Any]:
        return {"type": self.type, "content": self.content,
                "target": self.target, "priority": self.priority}

    @classmethod
    def from_dict(cls, data: Any) -> Tuple[Optional["AgentResponse"], str]:
        if not isinstance(data, dict):
            return None, "not an object"
        resp = cls(type=str(data.get("type") or ""),
                   content=str(data.get("content") or ""),
                   target=str(data.get("target") or ""),
                   priority=data.get("priority", 0.5))
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
        # AgentResponse side of the contract (v1.15 speech output): what the
        # agent last said to the human, and how many responses it emitted.
        self._last_response: Optional[Dict[str, Any]] = None
        self._response_count = 0

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
        presence_event = ""
        with self._lock:
            self._seq += 1
            entry = observation.to_dict()
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
            self._last_response = response.to_dict()
            self._response_count += 1
        return True, "ok"

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
            return {
                "presence": self._presence,
                "seconds_since_last_observation":
                    round(age, 1) if age is not None else None,
                "observations_recorded": sum(self._counts.values()),
                "speech_recent": speech_recent,
                "vision_recent": vision_recent,
                "person_present": person_present,
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
            }
