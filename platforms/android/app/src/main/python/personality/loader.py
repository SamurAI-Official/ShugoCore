"""
Personality config loader.

Reads/writes personality.json from the agent's data directory. The config is
a plain JSON file so operators can edit Shugo's character without touching
code — just modify the values and restart the agent.
"""
import json
import os
import logging
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_PERSONALITY_FILENAME = "personality.json"


@dataclass
class PersonalityProfile:
    """Shugo's character definition. All fields are editable config."""
    name: str = "Shugo"
    traits: Dict[str, str] = field(default_factory=lambda: {
        "warmth": "warm and caring without being overbearing",
        "wit": "occasional dry humor, never at the user's expense",
        "curiosity": "genuinely asks follow-up questions",
        "patience": "never rushes the user",
    })
    speech: Dict[str, Any] = field(default_factory=lambda: {
        "style": "conversational, like a smart friend",
        "formality": "casual",
        "max_sentences": 3,
        "first_person": True,
        "never_say": ["As an AI", "I'm just an AI", "I don't have feelings"],
    })
    boundaries: Dict[str, Any] = field(default_factory=lambda: {
        "refuse_harmful": True,
        "honest_about_limitations": True,
        "no_impersonation": True,
    })
    # Reserved, and NOT consumed yet -- stated here because the loader parses and
    # merges it either way, so an operator can set these and see nothing happen.
    # Measured across the runtime: `greet_on_arrival`, `ask_follow_up` and
    # `offer_help` appear only in this file's defaults. `personality_system_prompt`
    # renders identity, traits, speech, never_say and boundaries -- not this -- and
    # `PersonalityModel.as_profile()` (which is what the agent hands the prompt
    # layer after boot) does not carry it at all.
    #
    # The name also collides with something else that *is* live: the governor reads
    # a learned trait, `model.traits["proactivity"]`, as a threshold input. That is
    # a grown value from the turn window, unrelated to these switches.
    #
    # Left in place rather than removed (frozen 1.x), and left unwired on purpose:
    # "greet on arrival" and "offer help" are behaviour with product decisions
    # attached (when, and what to offer), not something to infer from a flag.
    proactivity: Dict[str, Any] = field(default_factory=lambda: {
        "greet_on_arrival": True,
        "ask_follow_up": True,
        "offer_help": False,
    })


def load_personality(data_dir: Optional[str] = None) -> PersonalityProfile:
    """Load personality from data_dir/personality.json. Falls back to defaults
    when the file is missing or malformed — the agent always has a character."""
    if not data_dir:
        return PersonalityProfile()
    path = os.path.join(data_dir, _PERSONALITY_FILENAME)
    if not os.path.exists(path):
        logger.info("personality.json not found at %s — using defaults", path)
        return PersonalityProfile()
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
        return _from_dict(raw)
    except Exception as exc:
        logger.warning("Failed to load personality.json: %s — using defaults", exc)
        return PersonalityProfile()


def save_personality(profile: PersonalityProfile, data_dir: str) -> str:
    """Persist personality to data_dir/personality.json. Returns the path."""
    path = os.path.join(data_dir, _PERSONALITY_FILENAME)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(_to_dict(profile), f, indent=2, ensure_ascii=False)
    return path


def _from_dict(d: Dict[str, Any]) -> PersonalityProfile:
    """Build a PersonalityProfile from a dict, filling any missing keys with
    defaults so old configs keep working when new fields are added."""
    defaults = PersonalityProfile()
    return PersonalityProfile(
        name=d.get("name", defaults.name),
        traits={**defaults.traits, **(d.get("traits") or {})},
        speech={**defaults.speech, **(d.get("speech") or {})},
        boundaries={**defaults.boundaries, **(d.get("boundaries") or {})},
        proactivity={**defaults.proactivity, **(d.get("proactivity") or {})},
    )


def _to_dict(profile: PersonalityProfile) -> Dict[str, Any]:
    return asdict(profile)
