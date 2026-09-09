"""
ShugoCore personality system (v1.29).

Data-driven character definition: Shugo's personality, speech style and
boundaries live in an editable JSON config file — not in code. The loader
reads that config; the prompt module turns the traits into natural-language
instructions injected into the model system prompt.

Exports:
    PersonalityProfile  — dataclass of traits/speech/boundaries
    load_personality()  — read personality.json from a data directory
    personality_system_prompt() — traits -> system prompt text
    PersonalityModel    — living developmental personality (CSFA)
"""
from personality.loader import PersonalityProfile, load_personality, save_personality
from personality.prompt import personality_system_prompt, DEFAULT_PERSONALITY
from personality.model import PersonalityModel, TraitState, GROWTH_RATE

__all__ = [
    "PersonalityProfile",
    "load_personality",
    "save_personality",
    "personality_system_prompt",
    "DEFAULT_PERSONALITY",
    "PersonalityModel",
    "TraitState",
    "GROWTH_RATE",
]
