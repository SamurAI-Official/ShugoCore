"""
ShugoCore personality system (v1.29).

Data-driven character definition: Shugo's personality, speech style and
boundaries live in an editable JSON config file — not in code. The loader
reads that config; the prompt module turns the traits into natural-language
instructions injected into the model system prompt.

The living ``PersonalityModel`` is now also a first-class reasoning signal:
``PersonalityGovernor`` annotates every proposed action with a structured
verdict (tone / verbosity / appropriateness) and can modify or reroute it —
sitting alongside the safety governor, never replacing it.

Exports:
    PersonalityProfile  — dataclass of traits/speech/boundaries
    load_personality()  — read personality.json from a data directory
    personality_system_prompt() — traits -> system prompt text
    PersonalityModel    — living developmental personality (CSFA)
    PersonalityGovernor — structured personality reasoning layer
    PersonalityVerdict  — annotation of a proposed action
"""
from personality.loader import PersonalityProfile, load_personality, save_personality
from personality.prompt import personality_system_prompt, DEFAULT_PERSONALITY
from personality.model import PersonalityModel, TraitState, GROWTH_RATE
from personality.governor import PersonalityGovernor, PersonalityVerdict

__all__ = [
    "PersonalityProfile",
    "load_personality",
    "save_personality",
    "personality_system_prompt",
    "DEFAULT_PERSONALITY",
    "PersonalityModel",
    "TraitState",
    "GROWTH_RATE",
    "PersonalityGovernor",
    "PersonalityVerdict",
]
