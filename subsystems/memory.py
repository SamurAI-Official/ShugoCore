"""
Memory & context management (Phase 3.1).

Turns raw conversational input into durable context that Shugo can reuse
across turns and across restarts:

    FactMemory   — extracts and stores user facts & preferences
                   ("my name is Alex" → name=Alex; "I like jazz" → preference)
                   scored by (repetition x recency), JSON-persisted.
    TopicTracker — keeps a rolling picture of the current topic so a follow-up
                   like "how long?" stays anchored to the earlier timer request.

Both are pure and dependency-free (stdlib only) — the agent shell composes
them; they never import the shell.
"""
import json
import logging
import os
import re
import threading
import time
from collections import deque
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# --- Fact extraction patterns ---------------------------------------------
# Each pattern: (regex, fact_kind). Extraction order matters — name before
# generic attribute so "my name is X" doesn't fall through to `my <noun> is`.
_FACT_PATTERNS: List[Tuple[str, str]] = [
    (r"\bmy\s+name\s+is\s+([A-Z][\w-]*)\b", "user_name"),
    (r"\bcall\s+me\s+([A-Z][\w-]*)\b", "user_name"),
    (r"\bi'?m\s+called\s+([A-Z][\w-]*)\b", "user_name"),
    (r"\bi\s+(?:like|love|enjoy|prefer)\s+([a-z][\w\s]{1,40}?)(?:\.|\,|$)", "preference"),
    (r"\bi\s+don'?t\s+(?:like|enjoy|prefer)\s+([a-z][\w\s]{1,40}?)(?:\.|\,|$)", "dislike"),
    (r"\bi\s+(?:want|wanna|need|would\s+like)\s+to\s+([a-z][\w\s]{1,50}?)(?:\.|\,|$)", "goal"),
    (r"\bmy\s+favorite\s+([a-z]+)\s+is\s+([\w\s]+?)(?:\.|\,|$)", "favorite"),
    (r"\bmy\s+([a-z]+)\s+is\s+([\w\s]+?)(?:\.|\,|$)", "attribute"),
    (r"\bi\s+am?\s+([\w\s]{2,40}?)(?:\,|\.|$)", "self_description"),
]

# Duration words for timer/reminder facts.
_DURATION_RE = re.compile(r"(\d+)\s*(minute|min|second|sec|hour|hr)s?\b", re.IGNORECASE)

# --- Topic keywords ---------------------------------------------------------
# Maps a canonical topic to the words that imply it. Matched as substrings
# on the lower-cased utterance. Order matters — check in dict order.
_TOPIC_KEYWORDS: Dict[str, Tuple[str, ...]] = {
    "timers": ("timer", "remind", "reminder", "alarm", "minute", "seconds", "hour", "wake me"),
    "weather": ("weather", "rain", "sunny", "cloudy", "temperature", "forecast", "cold", "hot", "humid"),
    "time": ("what time", "o'clock", "current time", "clock", "what's the time"),
    "device_status": ("battery", "charge", "sensor", "status", "storage", "health"),
    "media": ("music", "play", "song", "album", "podcast", "radio", "video", "spotify"),
    "communication": ("call", "text", "message", "email", "contact", "notify"),
    "navigation": ("navigate", "direction", "where is", "route", "gps", "map"),
}
class Fact:
    """A durable fact about the user (or their environment)."""

    __slots__ = ("text", "kind", "created_at", "last_seen_at", "count")

    def __init__(self, text: str, kind: str,
                 created_at: Optional[float] = None,
                 last_seen_at: Optional[float] = None,
                 count: int = 1):
        now = time.time()
        self.text = text
        self.kind = kind
        self.created_at = created_at if created_at is not None else now
        self.last_seen_at = last_seen_at if last_seen_at is not None else now
        self.count = count

    def score(self, now: Optional[float] = None) -> float:
        """Strength = repetition count * recency decay (half-life ~2 days)."""
        now = now if now is not None else time.time()
        age_hours = max(0.0, (now - self.last_seen_at) / 3600.0)
        recency = 0.5 ** (age_hours / 48.0)
        return float(self.count) * recency

    def to_dict(self) -> Dict[str, Any]:
        return {"text": self.text, "kind": self.kind,
                "created_at": self.created_at, "last_seen_at": self.last_seen_at,
                "count": self.count}

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Fact":
        return cls(text=data.get("text", ""),
                   kind=data.get("kind", "unknown"),
                   created_at=data.get("created_at"),
                   last_seen_at=data.get("last_seen_at"),
                   count=data.get("count", 1))

    def __repr__(self) -> str:
        return f"Fact({self.kind}: {self.text!r}, x{self.count})"


def fact_to_second_person(text: str) -> str:
    """Rewrite a third-person fact for speech: 'User's name is Alex' ->
    \"You're Alex\", 'User likes tea' -> 'You like tea', etc. Unrecognized
    forms pass through unchanged."""
    t = (text or "").strip()
    lower = t.lower()
    for prefix, replacement in (
            ("user's name is ", "You're "),
            ("user's ", "Your "),
            ("user likes ", "You like "),
            ("user dislikes ", "You don't like "),
            ("user wants to ", "You want to "),
            ("user describes themselves as ", "You describe yourself as "),
            ("user requested a ", "You requested a ")):
        if lower.startswith(prefix):
            return replacement + t[len(prefix):]
    return t


# Common function words ignored when matching a transcript against facts.
_STOPWORDS = frozenset({
    "the", "a", "an", "and", "but", "so", "for", "with", "that", "this",
    "you", "your", "yours", "are", "was", "were", "is", "has", "had",
    "have", "do", "does", "did", "my", "me", "im", "am", "what", "whats",
    "who", "how", "why", "when", "where", "about", "remember", "forget",
    "recall", "tell", "say", "said", "know",
})


class FactMemory:
    """Durable user facts. Persisted as JSON so they survive agent restarts.

    Usage:
        fm = FactMemory(data_dir)
        fm.remember("my name is Alex and I like jazz")
        fm.recall(limit=5)  # -> sorted, most relevant first
    """

    FILENAME = "user_facts.json"
    _WRITE_LOCK = threading.RLock()

    def __init__(self, data_dir: Optional[str] = None):
        self.data_dir = data_dir
        self._facts: Dict[str, Fact] = {}
        self._path = None
        if data_dir:
            self._path = os.path.join(data_dir, self.FILENAME)
            self._load()

    # -- persistence -------------------------------------------------------
    def _load(self) -> None:
        try:
            if not self._path or not os.path.exists(self._path):
                return
            with open(self._path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            for item in data if isinstance(data, list) else []:
                fact = Fact.from_dict(item)
                if fact.text:
                    self._facts[fact.text.strip().lower()] = fact
        except Exception as exc:
            logger.warning("FactMemory load failed: %s", exc)

    def save(self) -> None:
        """Persist facts to disk (best-effort, thread-safe)."""
        if not self._path:
            return
        try:
            with self._WRITE_LOCK:
                payload = [f.to_dict() for f in self._facts.values()]
                tmp = self._path + ".tmp"
                with open(tmp, "w", encoding="utf-8") as fh:
                    json.dump(payload, fh, indent=2)
                os.replace(tmp, self._path)
        except Exception as exc:
            logger.warning("FactMemory save failed: %s", exc)

    def clear(self) -> None:
        self._facts.clear()
        self.save()

    def __len__(self) -> int:
        return len(self._facts)

    # -- writing -------------------------------------------------------
    def remember(self, utterance: str,
                 source: str = "user") -> List[str]:
        """Extract facts from an utterance and store them.

        Returns the list of fact texts that were newly added or refreshed.
        """
        if not utterance or not isinstance(utterance, str):
            return []
        extracted = self._extract(utterance)
        touched: List[str] = []
        for text, kind in extracted:
            key = text.strip().lower()
            now = time.time()
            if key in self._facts:
                fact = self._facts[key]
                fact.count += 1
                fact.last_seen_at = now
                # "i like X" -> "i don't like X" should downgrade, not stack.
                if kind == "dislike" and fact.kind == "preference":
                    fact.kind = "dislike"
            else:
                self._facts[key] = Fact(text=text.strip(), kind=kind)
            touched.append(text.strip())
        if extracted:
            self.save()
        return touched

    def remember_explicit(self, text: str) -> Optional[str]:
        """Phase 4: store a user-requested fact verbatim ("remember that
        my sister is Ana"). Returns the stored fact text, or None when the
        text is too short/long to be a usable fact."""
        text = (text or "").strip()
        if len(text) < 2 or len(text) > 160:
            return None
        key = text.lower()
        now = time.time()
        if key in self._facts:
            fact = self._facts[key]
            fact.count += 1
            fact.last_seen_at = now
        else:
            self._facts[key] = Fact(text=text, kind="explicit")
        self.save()
        return text

    def forget(self, keyword: str) -> int:
        """Phase 4: drop facts matching a keyword (substring, case-insensitive).
        Returns how many facts were removed."""
        kw = (keyword or "").strip().lower()
        if not kw:
            return 0
        doomed = [key for key, fact in self._facts.items()
                  if kw in key or kw in fact.text.lower()]
        for key in doomed:
            del self._facts[key]
        if doomed:
            self.save()
        return len(doomed)

    @staticmethod
    def _extract(utterance: str) -> List[Tuple[str, str]]:
        """Run all fact patterns against an utterance."""
        results: List[Tuple[str, str]] = []
        text = utterance.strip()
        for pattern, kind in _FACT_PATTERNS:
            try:
                m = re.search(pattern, text, re.IGNORECASE)
            except re.error:
                continue
            if not m:
                continue
            capture = " ".join(g for g in m.groups() if g).strip()
            # Reject over-captured lines (pattern articles forced a full phrase)
            if len(capture) < 2 or len(capture) > 80:
                continue
            # Over-greedy captures ("my X is Y and I like Z") — keep only the
            # first clause so facts stay atomic and topical.
            if kind in ("attribute", "self_description"):
                for joiner in (" and ", " but ", " so ", " i ",
                               " and i ", " i also "):
                    idx = capture.lower().find(joiner)
                    if idx > 0:
                        capture = capture[:idx].strip()
                        if len(capture) < 2:
                            break
            if len(capture) < 2:
                continue
            if kind == "user_name":
                results.append((f"User's name is {capture}", kind))
            elif kind == "favorite":
                results.append((f"User's favorite {m.group(1)} is {m.group(2).strip()}", kind))
            elif kind == "attribute":
                results.append((f"User's {m.group(1)} is {capture}", kind))
            elif kind == "self_description":
                results.append((f"User describes themselves as {capture}", kind))
            else:  # preference / dislike / goal
                results.append((f"User likes {capture}" if kind == "preference"
                                else f"User dislikes {capture}" if kind == "dislike"
                                else f"User wants to {capture}", kind))
        # Also catch durations ("for 5 minutes") as a fact
        dm = _DURATION_RE.search(text)
        if dm:
            results.append((f"User requested a {dm.group(1)} {dm.group(2)} duration",
                            "duration_preference"))
        return results
# -- reading -----------------------------------------------------------
    def recall(self, limit: int = 5,
               min_score: float = 0.0) -> List[Dict[str, Any]]:
        """Return top facts by score, most relevant first."""
        now = time.time()
        ranked = sorted(self._facts.values(),
                        key=lambda f: f.score(now), reverse=True)
        out: List[Dict[str, Any]] = []
        for fact in ranked:
            score = fact.score(now)
            if score < min_score:
                continue
            out.append({
                "text": fact.text,
                "kind": fact.kind,
                "score": round(score, 3),
                "count": fact.count,
                "last_seen_at": fact.last_seen_at,
            })
            if len(out) >= limit:
                break
        return out

    def facts_about(self, keyword: str, limit: int = 3) -> List[Dict[str, Any]]:
        """Facts that mention a keyword (used for question-time recall)."""
        kw = keyword.strip().lower()
        if not kw:
            return []
        matches = [f for f in self._facts.values()
                   if kw in f.text.lower()]
        matches.sort(key=lambda f: f.score(), reverse=True)
        return [{"text": f.text, "kind": f.kind,
                 "score": round(f.score(), 3)} for f in matches[:limit]]

    def relevant_facts(self, transcript: str,
                       limit: int = 4) -> List[Dict[str, Any]]:
        """Phase 5: facts most relevant to a transcript — keyword overlap
        first, then top-scored facts to fill remaining slots. Drives prompt
        injection so the agent surfaces what the user is talking about,
        not just the most repeated fact."""
        words = {w.replace("'", "") for w in
                 re.findall(r"[a-z0-9']+", (transcript or "").lower())
                 if len(w) >= 3 and w not in _STOPWORDS}
        if not words:
            return self.recall(limit=limit)
        ranked = []
        for fact in self._facts.values():
            fwords = {w.replace("'", "") for w in
                      re.findall(r"[a-z0-9']+", fact.text.lower())}
            overlap = len(words & fwords)
            ranked.append((overlap, fact.score(), fact))
        # Overlap dominates; score breaks ties; score order is the
        # fallback when nothing overlaps.
        ranked.sort(key=lambda item: (item[0], item[1]), reverse=True)
        return [{"text": f.text, "kind": f.kind, "score": round(f.score(), 3)}
                for _, _, f in ranked[:limit]]

    def as_flavor_text(self, limit: int = 4) -> str:
        """Short human-readable list for prompt injection ('- fact' lines)."""
        rows = self.recall(limit=limit)
        if not rows:
            return ""
        return "\n".join(f"  - {r['text']}" for r in rows)


class TopicTracker:
    """Tracks the current conversation topic with decay.

    A follow-up like "how long?" contains no topic keyword, so the tracker
    treats it as a continuation of the previous topic (with mild decay),
    keeping follow-up turns anchored to what started them.
    """

    def __init__(self, max_history: int = 12, decay_per_turn: float = 0.25):
        self._current: Optional[str] = None
        self._strength: float = 0.0
        self._history: deque = deque(maxlen=max_history)
        self._decay_per_turn = decay_per_turn

    @property
    def current_topic(self) -> Optional[str]:
        return self._current if self._strength > 0.15 else None

    def history(self) -> List[str]:
        return list(self._history)

    def track(self, utterance: str) -> Optional[str]:
        """Update topic state from a new utterance; return detected topic."""
        text = (utterance or "").strip().lower()
        detected = self._detect(text)
        if detected:
            self._current = detected
            self._strength = 1.0
        else:
            # Continuation: keep topic but decay confidence.
            self._strength -= self._decay_per_turn
        if self._current:
            self._history.append(self._current)
        return detected or self.current_topic

    @staticmethod
    def _detect(text: str) -> Optional[str]:
        for topic, keywords in _TOPIC_KEYWORDS.items():
            for kw in keywords:
                if kw in text:
                    return topic
        return None

    def as_context_text(self) -> str:
        topic = self.current_topic
        if not topic:
            return ""
        return (f"Current topic: {topic.replace('_', ' ')}. "
                "Follow-up utterances likely refer back to it.")

    def reset(self) -> None:
        self._current = None
        self._strength = 0.0
        self._history.clear()


class MemoryManager:
    """Composes FactMemory + TopicTracker with a single entry point."""

    def __init__(self, data_dir: Optional[str] = None):
        self.facts = FactMemory(data_dir)
        self.topics = TopicTracker()

    def on_user_input(self, transcript: str) -> Dict[str, Any]:
        """Record a user utterance: extract facts + update topic.

        Returns a small digest for logging / tests.
        """
        new_facts = self.facts.remember(transcript)
        topic = self.topics.track(transcript)
        return {"new_facts": new_facts, "topic": topic}

    def recall_context(self, limit: int = 4) -> Dict[str, Any]:
        """Everything the prompt builder needs to know about the user."""
        return {
            "facts": self.facts.recall(limit=limit),
            "current_topic": self.topics.current_topic,
            "topic_text": self.topics.as_context_text(),
        }

    def recall_fact_strings(self, limit: int = 4,
                            about: Optional[str] = None) -> List[str]:
        """Phase 5: fact strings for prompt injection. With ``about`` (the
        current transcript), relevance-ranked facts come first."""
        rows = (self.facts.relevant_facts(about, limit=limit) if about
                else self.facts.recall(limit=limit))
        return [r["text"] for r in rows]
        return len(self._facts)