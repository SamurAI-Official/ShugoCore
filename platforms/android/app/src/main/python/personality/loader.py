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

# The proactivity switches: the operator's permission for the agent to originate
# speech of a given kind, rather than only answering. One source of truth -- the
# dataclass field below defaults from this, `PersonalityModel` freezes it with the
# rest of the policy at genesis, and `personality_system_prompt` renders it.
#
#   greet_on_arrival  the agent may greet a person when their arrival is detected
#                     (an attention transition into a person-present state)
#   ask_follow_up     the agent may ask a follow-up question of its own
#   offer_help        the agent may volunteer help that was not asked for
#
# Permission is not the same as readiness. `offer_help` additionally requires the
# *learned* proactivity trait to clear the personality governor's threshold (see
# `PersonalityGovernor.policy["proactivity_threshold"]`), because volunteering
# unrequested help is exactly what that grown trait governs. A greeting is a social
# convention the operator asked for by name, so it needs permission only.
PROACTIVITY_DEFAULTS: Dict[str, bool] = {
    "greet_on_arrival": True,
    "ask_follow_up": True,
    "offer_help": False,
}


def normalise_proactivity(raw: Any) -> Dict[str, bool]:
    """Fold a config block onto the known switches, dropping anything else.

    Unknown keys are dropped rather than carried: this is a closed vocabulary, and a
    typo ('greet_on_arrival ' or 'offerHelp') must not read as a permission.
    """
    settings = dict(raw or {}) if isinstance(raw, dict) else {}
    return {key: bool(settings.get(key, default))
            for key, default in PROACTIVITY_DEFAULTS.items()}



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
    # The operator's permission for the agent to originate speech (see
    # PROACTIVITY_DEFAULTS above for what each switch means, and for the
    # permission-versus-readiness distinction). Consumed in three places:
    # `personality_system_prompt` renders it into the system prompt,
    # `PersonalityModel.genesis` freezes it into the policy so `as_profile()` --
    # which is what the agent holds as `self.personality` after boot -- carries it
    # back out, and the agent gates its own unprompted speech on it
    # (`ShugoAgent.proactive_permitted`).
    #
    # NOTE the name collision: the governor separately reads a *learned* trait,
    # `PersonalityModel.traits["proactivity"]`, as a 0..1 readiness value grown from
    # the turn window. That is not this. This is a yes/no permission from config.
    proactivity: Dict[str, Any] = field(
        default_factory=lambda: dict(PROACTIVITY_DEFAULTS))


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
        proactivity=normalise_proactivity(d.get("proactivity")),
    )


def _to_dict(profile: PersonalityProfile) -> Dict[str, Any]:
    return asdict(profile)
