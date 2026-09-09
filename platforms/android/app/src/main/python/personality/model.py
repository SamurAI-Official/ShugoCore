"""PersonalityModel — the living, developmental personality system.

Phase B of the CSFA architecture:

    Personality Model + Reasoning Model + Sensor Nervous System
    + Memory & Decision Engine = ShugoCore / SamurAIOS

Unlike the static PersonalityProfile (config file -> prompt text), the
PersonalityModel is *born* as a blank baby state holding only the frozen
PolicyPerimeters, then grows generation by generation from memory under
strict invariants:

  * policy never mutates — growth only moves the trait vector;
  * growth is clamped (GROWTH_RATE per generation) so identity shifts
    gradually and observably;
  * every generation is recorded (append-only history) and diffed
    against the previous one (compare_models);
  * the model persists atomically to data_dir/personality_model.json
    and survives process death like timers and facts do.
"""
import json
import math
import os
import time
from typing import Any, Dict, List, Optional

# Maximum movement any single trait can make in one generation. This is
# the "growth rate" — identity changes gradually, never in one jump.
GROWTH_RATE = 0.1

# Baby-state baselines: a blank personality, neither cold nor intense.
BASELINE_TRAITS: Dict[str, float] = {
    "warmth": 0.5,
    "humor": 0.4,
    "curiosity": 0.5,
    "verbosity": 0.5,
    "formality": 0.3,
    "proactivity": 0.3,
}


class TraitState:
    """One trait of the personality vector at a point in development."""

    __slots__ = ("value", "confidence", "evidence_count", "last_gen")

    def __init__(self, value: float, confidence: float = 0.0,
                 evidence_count: int = 0, last_gen: int = 0):
        self.value = max(0.0, min(1.0, float(value)))
        self.confidence = max(0.0, min(1.0, float(confidence)))
        self.evidence_count = int(evidence_count)
        self.last_gen = int(last_gen)

    def to_dict(self) -> Dict[str, Any]:
        return {"value": self.value, "confidence": self.confidence,
                "evidence_count": self.evidence_count,
                "last_gen": self.last_gen}

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "TraitState":
        return cls(value=data.get("value", 0.5),
                   confidence=data.get("confidence", 0.0),
                   evidence_count=data.get("evidence_count", 0),
                   last_gen=data.get("last_gen", 0))

    def __repr__(self) -> str:
        return (f"TraitState({self.value:.2f}, conf={self.confidence:.2f}, "
                f"ev={self.evidence_count})")


def _extract_policy(profile: Any) -> Dict[str, Any]:
    """Freeze the PolicyPerimeters out of a PersonalityProfile.

    The policy is the immutable seed of the baby state: boundaries,
    forbidden phrases and consent rules. Everything else about the
    personality is allowed to grow; this is not.
    """
    boundaries = dict(getattr(profile, "boundaries", {}) or {})
    speech = dict(getattr(profile, "speech", {}) or {})
    return {
        "boundaries": boundaries,
        "never_say": list(speech.get("never_say") or []),
        "first_person": bool(speech.get("first_person", True)),
        "refuse_harmful": bool(boundaries.get("refuse_harmful", True)),
    }


class PersonalityModel:
    """A developmental personality: frozen policy + growing trait vector."""

    def __init__(self, name: str, generation: int,
                 policy: Dict[str, Any],
                 traits: Dict[str, TraitState],
                 voice: Dict[str, Any],
                 history: Optional[List[Dict[str, Any]]] = None,
                 created_at: Optional[float] = None,
                 updated_at: Optional[float] = None):
        self.name = name
        self.generation = int(generation)
        self.policy = policy  # frozen — never mutated by growth
        self.traits = traits
        self.voice = voice  # emergent speech params (max_sentences, style)
        self.history = history if history is not None else []
        self.created_at = (created_at if created_at is not None
                           else time.time())
        self.updated_at = (updated_at if updated_at is not None
                           else time.time())

    # -- genesis ----------------------------------------------------------
    @classmethod
    def genesis(cls, profile: Any) -> "PersonalityModel":
        """Birth: a blank baby state holding only the policy perimeters.

        Traits start at neutral baselines with zero confidence and zero
        evidence — nothing has been learned yet. Generation 0. The name
        and voice seed come from the static config; everything else will
        be earned from memory.
        """
        policy = _extract_policy(profile)
        traits = {name: TraitState(value=baseline)
                  for name, baseline in BASELINE_TRAITS.items()}
        speech = dict(getattr(profile, "speech", {}) or {})
        voice = {
            "max_sentences": int(speech.get("max_sentences", 3)),
            "style": speech.get("style", "conversational, like a smart friend"),
        }
        model = cls(name=getattr(profile, "name", "Shugo"),
                    generation=0, policy=policy, traits=traits,
                    voice=voice,
                    history=[{"gen": 0, "note": "genesis (baby state)",
                              "deltas": {}, "at": time.time()}])
        return model

    # -- growth -------------------------------------------------------------
    def apply_delta(self, deltas: Dict[str, float],
                    note: str = "") -> Dict[str, float]:
        """Grow: move the trait vector by (clamped) deltas.

        Each trait moves at most GROWTH_RATE and stays within [0, 1].
        Evidence counters rise with every observation. The generation is
        bumped and the change is recorded in the append-only history.
        The policy is untouched — growth cannot rewrite perimeters.
        Returns the deltas actually applied (after clamping).
        """
        applied: Dict[str, float] = {}
        for trait, delta in (deltas or {}).items():
            state = self.traits.get(trait)
            if state is None:
                continue  # unknown traits cannot appear via growth
            bounded = max(-GROWTH_RATE, min(GROWTH_RATE, float(delta)))
            new_value = max(0.0, min(1.0, state.value + bounded))
            applied[trait] = round(new_value - state.value, 6)
            state.value = new_value
            state.evidence_count += 1
            state.confidence = min(1.0, state.confidence + 0.1)
            state.last_gen = self.generation + 1
        if applied:
            self.generation += 1
            self.updated_at = time.time()
            self.history.append({
                "gen": self.generation, "note": note,
                "deltas": applied, "at": self.updated_at})
        return applied

    # -- comparison -----------------------------------------------------------
    @staticmethod
    def compare_models(before: "PersonalityModel",
                       after: "PersonalityModel") -> Dict[str, Any]:
        """Compare a pre-existing model generation against its successor.

        Thin wrapper over before.diff(after) — i.e. read the change from the
        successor's perspective: per-trait value deltas (positive = growth
        in the successor) plus the drift scalar and any policy violations.
        Raises ValueError for different agent names so unrelated
        personalities can never be silently compared.
        """
        if before.name != after.name:
            raise ValueError(
                f"cannot compare models of different agents "
                f"({before.name!r} vs {after.name!r})")
        return before.diff(after)

    def diff(self, other: "PersonalityModel") -> Dict[str, Any]:
        """Compare this model against another (typically the previous
        generation). Returns per-trait deltas, a drift scalar in [0, 1]
        (RMS trait distance), and whether the policy perimeters moved —
        which would be an invariant violation, reported, never accepted.
        """
        trait_deltas = {
            trait: round(other.traits[trait].value - state.value, 6)
            for trait, state in self.traits.items()
            if trait in other.traits}
        if trait_deltas:
            drift = math.sqrt(
                sum(d * d for d in trait_deltas.values())
                / len(trait_deltas))
        else:
            drift = 0.0
        return {
            "from_gen": self.generation,
            "to_gen": other.generation,
            "trait_deltas": trait_deltas,
            "drift": round(drift, 6),
            "policy_violation": self.policy != other.policy,
        }

    # -- persistence ------------------------------------------------------------
    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "generation": self.generation,
            "policy": self.policy,
            "traits": {t: s.to_dict() for t, s in self.traits.items()},
            "voice": self.voice,
            "history": self.history,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> Optional["PersonalityModel"]:
        """Rebuild from its persisted form. Returns None for missing or
        malformed payloads — callers fall back to genesis."""
        try:
            traits_raw = data["traits"]
            policy = data["policy"]
            traits = {t: TraitState.from_dict(s)
                      for t, s in traits_raw.items()}
            history = list(data.get("history") or [])
            return cls(name=data.get("name", "Shugo"),
                       generation=data.get("generation", 0),
                       policy=policy,
                       traits=traits,
                       voice=dict(data.get("voice") or {}),
                       history=history,
                       created_at=data.get("created_at"),
                       updated_at=data.get("updated_at"))
        except (KeyError, TypeError, AttributeError):
            return None

    def save(self, path: str) -> str:
        """Atomically persist (tmp file + rename, like timers.json)."""
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self.to_dict(), fh, indent=2, ensure_ascii=False)
        os.replace(tmp, path)
        return path

    @staticmethod
    def load(path: str) -> Optional["PersonalityModel"]:
        """Load from data_dir/personality_model.json. None when absent,
        corrupt, or structurally malformed."""
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return None
        return PersonalityModel.from_dict(data)

    # -- consumption ---------------------------------------------------------
    _PHRASES = {
        "warmth": ("reserved but kind", "kind but measured",
                   "genuinely warm and caring"),
        "humor": ("serious in tone", "occasionally witty",
                  "quick with dry humor"),
        "curiosity": ("content to listen", "curious about the user",
                      "deeply curious, asks follow-up questions"),
        "verbosity": ("concise by nature", "balanced in length",
                      "expressive and expansive"),
        "formality": ("casual, like a smart friend", "relaxed but polite",
                      "proper and formal"),
        "proactivity": ("reactive, waits to be asked", "helpful when asked",
                        "proactive, offers help unprompted"),
    }

    @staticmethod
    def _bucket(value: float) -> int:
        if value < 0.4:
            return 0
        if value < 0.7:
            return 1
        return 2

    def as_profile(self) -> Any:
        """Render into a static PersonalityProfile for the prompt layer.

        The prompt module (personality/prompt.py) stays the single
        translator of config -> instructions; the living model feeds it
        by expressing its current trait vector as natural-language trait
        phrases and its frozen policy as boundaries / never_say.
        """
        from personality.loader import PersonalityProfile
        trait_text = {}
        for trait, state in self.traits.items():
            phrases = self._PHRASES.get(trait)
            trait_text[trait] = (phrases[self._bucket(state.value)]
                                 if phrases else f"{trait} {state.value:.2f}")
        return PersonalityProfile(
            name=self.name,
            traits=trait_text,
            speech={
                "style": self.voice.get("style", "conversational"),
                "formality": ("casual" if self.traits["formality"].value < 0.5
                              else "formal"),
                "max_sentences": int(self.voice.get("max_sentences", 3)),
                "first_person": bool(self.policy.get("first_person", True)),
                "never_say": list(self.policy.get("never_say") or []),
            },
            boundaries=dict(self.policy.get("boundaries") or {}),
        )

    def __repr__(self) -> str:
        trait_text = ", ".join(
            f"{t}:{s.value:.2f}" for t, s in self.traits.items())
        return f"PersonalityModel(gen={self.generation}, {trait_text})"
