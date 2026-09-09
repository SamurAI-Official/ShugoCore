"""
User intent classification.

Classifies what the user wants so the agent can route to the right handler:
    QUESTION    — "what's the weather?" → knowledge retrieval + speak
    COMMAND     — "set a timer for 5 minutes" → tool execution
    CHITCHAT    — "how are you?" → personality-driven response
    GREETING    — "hello" → greeting response
    FAREWELL    — "goodbye" → farewell response
    CONFUSED    — unclear intent → ask for clarification

The classifier is rule-based (fast, deterministic) — no model call needed.
This keeps intent detection at <10ms so it doesn't add latency.
"""
import re
from enum import Enum
from typing import Any, Dict, Optional


class IntentType(Enum):
    QUESTION = "question"
    COMMAND = "command"
    CHITCHAT = "chitchat"
    GREETING = "greeting"
    FAREWELL = "farewell"
    CONFUSED = "confused"


class UserIntent:
    """A classified user intent with metadata."""

    def __init__(self, intent_type: IntentType, transcript: str,
                 confidence: float = 0.5, entities: Optional[Dict[str, Any]] = None):
        self.intent_type = intent_type
        self.transcript = transcript
        self.confidence = confidence
        self.entities = entities or {}

    def __repr__(self) -> str:
        return f"UserIntent({self.intent_type.value}, conf={self.confidence:.2f})"


class IntentParser:
    """Rule-based intent classifier. Fast (<10ms) and deterministic."""

    # Greetings
    _GREETING_PATTERNS = [
        r"^(hi|hello|hey|good\s+(morning|afternoon|evening)|howdy|what'?s\s+up)",
        r"^(yo|sup|greetings)",
    ]

    # Farewells
    _FAREWELL_PATTERNS = [
        r"(goodbye|bye|see\s+you|later|good\s+night|take\s+care)$",
        r"^(bye|later|gotta\s+go|i'?m\s+leaving)",
    ]

    # Commands — action verbs that imply the user wants something done
    _COMMAND_PATTERNS = [
        r"^(set|create|start|stop|turn|open|close|play|pause|remind|show|tell|find|search|send|call|text|navigate|go\s+to)",
        r"(set\s+a\s+timer|remind\s+me|turn\s+(on|off)|play\s+(some\s+)?music|what\s+time\s+is\s+it|what'?s\s+the\s+weather|check\s+(my\s+)?(battery|email|calendar))",
        r"(call|text|message)\s+\w+",
        # Phase 4: durable-memory commands ("remember that X", "forget about X").
        # The mid-string variant excludes leading question words so
        # "do you remember my name" stays a question.
        r"^(remember|recall|forget)\b",
        r"^(?!(what|who|where|when|why|how)\b).*\b(remember|forget)\s+(that|about|to)\b",
    ]

    # Questions — wh-words and question marks
    _QUESTION_PATTERNS = [
        r"^(what|who|where|when|why|how|which|is|are|can|could|would|do|does|did|have|has|will)",
        r"\?$",
    ]

    # Chitchat — social/relational, not seeking information
    _CHITCHAT_PATTERNS = [
        r"(how\s+are\s+you|how'?s\s+it\s+going|what'?s\s+new|tell\s+me\s+a\s+joke|do\s+you\s+(like|love|hate)|what\s+do\s+you\s+think\s+about)",
        r"(i\s+(love|hate|like|miss|want)\s+)",
        r"(thank|thanks|thx)",
    ]

    def __init__(self):
        self._greeting_re = [re.compile(p, re.IGNORECASE) for p in self._GREETING_PATTERNS]
        self._farewell_re = [re.compile(p, re.IGNORECASE) for p in self._FAREWELL_PATTERNS]
        self._command_re = [re.compile(p, re.IGNORECASE) for p in self._COMMAND_PATTERNS]
        self._question_re = [re.compile(p, re.IGNORECASE) for p in self._QUESTION_PATTERNS]
        self._chitchat_re = [re.compile(p, re.IGNORECASE) for p in self._CHITCHAT_PATTERNS]

    def classify(self, transcript: str) -> UserIntent:
        """Classify the user's intent from their transcript.

        Returns a UserIntent with the classified type and confidence.
        Order matters: more specific patterns are checked first."""
        text = transcript.strip()
        if not text:
            return UserIntent(IntentType.CONFUSED, text, confidence=0.1)

        # Check in order of specificity
        if self._matches_any(self._farewell_re, text):
            return UserIntent(IntentType.FAREWELL, text, confidence=0.9)

        if self._matches_any(self._greeting_re, text):
            return UserIntent(IntentType.GREETING, text, confidence=0.9)

        if self._matches_any(self._command_re, text):
            entities = self._extract_command_entities(text)
            return UserIntent(IntentType.COMMAND, text, confidence=0.85, entities=entities)

        if self._matches_any(self._question_re, text):
            return UserIntent(IntentType.QUESTION, text, confidence=0.8)

        if self._matches_any(self._chitchat_re, text):
            return UserIntent(IntentType.CHITCHAT, text, confidence=0.7)

        # Default: if it's short, probably chitchat; if long, probably a question
        if len(text.split()) <= 3:
            return UserIntent(IntentType.CHITCHAT, text, confidence=0.4)
        return UserIntent(IntentType.QUESTION, text, confidence=0.4)

    def _matches_any(self, patterns: list, text: str) -> bool:
        return any(p.search(text) for p in patterns)

    def _extract_command_entities(self, text: str) -> Dict[str, Any]:
        """Extract entities from a command (time, target, etc.)."""
        entities: Dict[str, Any] = {}

        # Time entities (timer, reminder)
        time_match = re.search(
            r"(\d+)\s*(minute|min|second|sec|hour|hr)s?", text, re.IGNORECASE)
        if time_match:
            entities["duration_value"] = int(time_match.group(1))
            entities["duration_unit"] = time_match.group(2).lower()

        # Target (who to call/text)
        target_match = re.search(
            r"(?:call|text|message)\s+(\w+)", text, re.IGNORECASE)
        if target_match:
            entities["target"] = target_match.group(1)

        # Action verb
        action_match = re.match(
            r"^(set|create|start|stop|turn|open|close|play|pause|remind|remember|recall|forget|show|tell|find|search|send|call|text)",
            text, re.IGNORECASE)
        if action_match:
            entities["action_verb"] = action_match.group(1).lower()

        # Phase 4: memory-command payload ("remember that my sister is Ana"
        # -> "my sister is Ana"). Everything after the op verb's connector.
        mem_match = re.search(
            r"\b(?:remember|recall|forget)\s+(?:that\s+|about\s+|to\s+)?(.+)",
            text, re.IGNORECASE)
        if mem_match:
            entities["memory_text"] = mem_match.group(1).strip()

        return entities
