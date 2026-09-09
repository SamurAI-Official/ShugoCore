"""
Multi-turn dialogue state (Phase 3.4).

Tracks what the agent is waiting for and lets follow-ups land correctly:

    "set a timer"          -> "Timer for how long?"   (pending clarification)
    "5 minutes"            -> parsed as the duration  (merged back in)
    "turn the lights off"  -> last_entity = "the lights"
    "now turn them on"     -> coreference resolved    (them -> the lights)

The building blocks:

    DialogueState   — clarification + coreference + last-utterance state
    extract_named_entity — best-effort target extraction for coreference

The dialogue module never talks to the model; it only restructures text and
intents so downstream routing (command executor / prompt builder) sees
complete, context-resolved input.
"""
import re
from typing import Any, Dict, List, Optional, Tuple

from subsystems.intent import IntentType, UserIntent

# What a clarification answer looks like per open-category. Each entry is a
# list of (regex, slot_name, value_transform). value_transform is "num" for
# ints, "unit" for duration units, else None (keep string group 1).
_CLARIFICATION_PATTERNS: Dict[str, List[Tuple[str, str, Optional[str]]]] = {
    "timer_duration": [
        (r"(\d+)\s*(minute|min|second|sec|hour|hr)s?\b", "duration_value", "num"),
        (r"(\d+)\s*(minute|min|second|sec|hour|hr)s?\b", "duration_unit", "unit"),
        (r"(?:for|about)\s+(\d+)", "duration_value", "num"),
    ],
}

# Words that indicate a fresh intent (not an answer to a clarification).
_NEW_INTENT_WORDS = (
    "set", "create", "start", "stop", "turn", "open", "close", "play",
    "pause", "remind", "what", "who", "where", "when", "why", "how",
    "hello", "hi", "hey", "goodbye", "bye", "timer", "alarm", "weather",
    "battery", "music", "call", "text",
)

class DialogueState:
    """Holds pending clarifications and recent entity context."""

    def __init__(self, max_entities: int = 5,
                 clarification_timeout_secs: float = 90.0):
        import time
        self.pending_category: Optional[str] = None
        self.pending_transcript: str = ""
        self.pending_entities: Dict[str, Any] = {}
        self._pending_at: float = 0.0
        self._clarification_timeout = clarification_timeout_secs
        self.last_utterance: str = ""
        self._recent_entities: List[str] = []
        self._max_entities = max_entities

    # -- clarification ----------------------------------------------------
    @property
    def has_pending(self) -> bool:
        import time
        if not self.pending_category:
            return False
        if self._pending_at and (time.time() - self._pending_at
                                 > self._clarification_timeout):
            self.clear_pending()
            return False
        return True

    def begin_clarification(self, category: str, original_transcript: str,
                            entities: Optional[Dict[str, Any]] = None) -> None:
        """Record that the agent asked a follow-up question."""
        import time
        self.pending_category = category
        self.pending_transcript = original_transcript
        self.pending_entities = dict(entities or {})
        self._pending_at = time.time()

    def clear_pending(self) -> None:
        self.pending_category = None
        self.pending_transcript = ""
        self.pending_entities = {}
        self._pending_at = 0.0

    def looks_like_answer(self, transcript: str) -> bool:
        """True if a transcript looks like an answer to the open question
        (e.g., a duration when a timer duration was requested) rather than
        a brand-new intent."""
        if not self.has_pending or not transcript:
            return False
        lowered = transcript.strip().lower()
        first = lowered.split()[0] if lowered else ""
        if first in _NEW_INTENT_WORDS:
            return False
        patterns = _CLARIFICATION_PATTERNS.get(self.pending_category, [])
        return any(re.search(p, lowered) for p, _, _ in patterns)

    def resolve_answer(self, transcript: str) -> Optional[UserIntent]:
        """If transcript answers the pending clarification, merge the slot
        into the original utterance+entities and return the reconstructed
        intent. Returns None when it's not an answer."""
        if not self.looks_like_answer(transcript) or not self.pending_category:
            return None
        merged = dict(self.pending_entities)
        patterns = _CLARIFICATION_PATTERNS[self.pending_category]
        for pattern, slot, transform in patterns:
            m = re.search(pattern, transcript.strip().lower())
            if not m:
                continue
            if transform == "num":
                merged[slot] = int(m.group(1))
            elif transform == "unit":
                # "5 minutes" -> group(2) is the unit word.
                merged[slot] = (m.group(2) if m.lastindex and m.lastindex >= 2
                                else m.group(1)).lower()
            else:
                merged[slot] = m.group(1)
        # Rebuild the intent on the merged request.
        original = self.pending_transcript or transcript
        intent = UserIntent(IntentType.COMMAND, original, confidence=0.9,
                            entities=merged)
        self.clear_pending()
        return intent

    # -- coreference / entity memory --------------------------------------
    def remember_entity(self, entity: Optional[str]) -> None:
        if not entity:
            return
        if entity in self._recent_entities:
            self._recent_entities.remove(entity)
        self._recent_entities.insert(0, entity)
        del self._recent_entities[self._max_entities:]

    @property
    def last_entity(self) -> Optional[str]:
        return self._recent_entities[0] if self._recent_entities else None

    def resolve_coreference(self, transcript: str) -> str:
        """Replace pronouns (it/that/them/they/those) with the last entity
        mentioned, e.g. "turn them off" -> "turn the lights off"."""
        entity = self.last_entity
        if not entity or not _COREF_PROJECT.search(transcript):
            return transcript
        return _COREF_PROJECT.sub(entity, transcript)

    def record_utterance(self, transcript: str) -> None:
        self.last_utterance = transcript

    def clear(self) -> None:
        self.clear_pending()
        self.last_utterance = ""
        self._recent_entities.clear()
_COREF_PROJECT = re.compile(r"\b(it|that|this|them|they|those)\b", re.IGNORECASE)
def extract_named_entity(transcript: str) -> Optional[str]:
    """Best-effort noun-phrase entity extraction for coreference memory.

    Looks at verbs that take a target: 'turn ON {entity}',
    'call {entity}', 'play {entity}', 'text {entity}' etc.
    """
    if not transcript:
        return None
    lower = transcript.lower()
    patterns = [
        r"(?:turn|switch)\s+((?:the\s+)?[\w\s]{2,24}?)\s+(?:on|off)\b",
        r"(?:turn|switch)\s+(?:on|off)\s+(?:the\s+)?([\w\s]{2,24}?)(?:\.|\,|$)",
        r"(?:call|text|message|email)\s+(?:the\s+)?([\w\s]{2,24}?)(?:\.|\,|$)",
        r"(?:play|pause|stop)\s+(?:some\s+)?(?:the\s+)?([\w\s]{2,24}?)(?:\.|\,|$)",
        r"(?:open|close)\s+(?:the\s+)?([\w\s]{2,24}?)(?:\.|\,|$)",
    ]
    for pattern in patterns:
        m = re.search(pattern, lower)
        if m:
            entity = m.group(1).strip()
            if len(entity) >= 2 and entity not in ("timer", "alarm"):
                return entity
    return None