"""
Conversation state machine.

Tracks whether Shugo is in dialogue, what turn it is, and handles turn-taking
so the agent knows when to listen, when to speak, and when the user has
interrupted (barge-in).

States:
    IDLE         not in conversation, waiting for input
    LISTENING    user speech detected, accumulating
    PROCESSING   generating a response
    SPEAKING     TTS is playing
    WAITING      asked a question, listening for answer
"""
import logging
import threading
import time
from enum import Enum
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class ConversationState(Enum):
    IDLE = "idle"
    LISTENING = "listening"
    PROCESSING = "processing"
    SPEAKING = "speaking"
    WAITING = "waiting"


class ConversationManager:
    """Thread-safe dialogue state machine."""

    def __init__(self, max_history: int = 12):
        self._lock = threading.Lock()
        self._state = ConversationState.IDLE
        self._turn_index = 0
        self._history: List[Dict[str, str]] = []
        self._max_history = max_history
        self._last_user_transcript: str = ""
        self._last_agent_response: str = ""
        self._last_activity_ts: float = 0.0
        self._pending_question: bool = False

    @property
    def state(self) -> ConversationState:
        with self._lock:
            return self._state

    @property
    def is_in_conversation(self) -> bool:
        with self._lock:
            return self._state in (
                ConversationState.LISTENING,
                ConversationState.PROCESSING,
                ConversationState.SPEAKING,
                ConversationState.WAITING,
            )

    def on_user_speech(self, transcript: str) -> None:
        """Call when new user speech is detected."""
        with self._lock:
            now = time.time()
            self._last_user_transcript = transcript
            self._last_activity_ts = now
            if self._state in (ConversationState.IDLE, ConversationState.LISTENING):
                self._state = ConversationState.PROCESSING
            elif self._state == ConversationState.SPEAKING:
                logger.debug("conversation: barge-in during SPEAKING")
                self._state = ConversationState.PROCESSING
            elif self._state == ConversationState.WAITING:
                self._state = ConversationState.PROCESSING

    def on_response_start(self) -> None:
        """Call when the agent begins generating a response."""
        with self._lock:
            self._state = ConversationState.PROCESSING

    def on_speak_begin(self, text: str) -> None:
        """Call when TTS starts playing."""
        with self._lock:
            self._last_agent_response = text
            self._state = ConversationState.SPEAKING
            if self._last_user_transcript:
                self._append_turn("user", self._last_user_transcript)
                self._last_user_transcript = ""
            self._append_turn("assistant", text)
            self._turn_index += 1
            self._last_activity_ts = time.time()

    def on_speak_end(self, expects_answer: bool = False) -> None:
        """Call when TTS finishes."""
        with self._lock:
            self._pending_question = expects_answer
            self._state = ConversationState.WAITING if expects_answer else ConversationState.IDLE

    def on_barge_in(self) -> None:
        """Call when the user interrupts TTS."""
        with self._lock:
            self._state = ConversationState.LISTENING
            self._last_activity_ts = time.time()

    def _append_turn(self, role: str, text: str) -> None:
        self._history.append({"role": role, "text": text})
        if len(self._history) > self._max_history:
            self._history = self._history[-self._max_history:]

    def get_history(self, last_n: int = 8) -> List[Dict[str, str]]:
        """Return the last N conversation turns (oldest first)."""
        with self._lock:
            return list(self._history[-last_n:])

    def get_history_text(self, last_n: int = 6) -> str:
        """Format history as 'ROLE: text' lines for prompt injection."""
        with self._lock:
            lines = []
            for turn in self._history[-last_n:]:
                role = turn.get("role", "?").upper()[:5]
                text = turn.get("text", "")[:200]
                lines.append(f"{role}: {text}")
            return "\n".join(lines)

    def clear_history(self) -> None:
        """Reset conversation history."""
        with self._lock:
            self._history.clear()
            self._turn_index = 0
            self._state = ConversationState.IDLE

    def stats(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "state": self._state.value,
                "turn_index": self._turn_index,
                "history_len": len(self._history),
                "last_user_transcript": self._last_user_transcript,
                "last_agent_response": self._last_agent_response,
                "pending_question": self._pending_question,
                "last_activity_age_s": (
                    round(time.time() - self._last_activity_ts, 1)
                    if self._last_activity_ts else None
                ),
            }

    @property
    def turn_index(self) -> int:
        with self._lock:
            return self._turn_index
