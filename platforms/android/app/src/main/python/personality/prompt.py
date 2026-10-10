"""
Personality -> system prompt text.

Turns a PersonalityProfile into the natural-language block injected into the
model system prompt. This is the ONLY place that translates config into
instructions — edit the JSON, change the character.
"""
from typing import List

from personality.loader import PersonalityProfile, normalise_proactivity

# Default personality used when no config file exists.
DEFAULT_PERSONALITY = PersonalityProfile()


def personality_system_prompt(profile: PersonalityProfile) -> str:
    """Build the system prompt block from a personality profile.

    Returns a multi-paragraph string that instructs the model how to speak,
    what to avoid, and what character to embody. This goes at the TOP of
    every conversational prompt so the model always knows who it is.
    """
    parts: List[str] = []

    # --- Identity ---
    parts.append(f"You are {profile.name}.")

    # --- Traits ---
    if profile.traits:
        trait_text = "; ".join(profile.traits.values())
        parts.append(f"You are {trait_text}.")

    # --- Speech style ---
    speech = profile.speech
    style_parts: List[str] = []
    if speech.get("style"):
        style_parts.append(f"Speak {speech['style']}")
    if speech.get("formality"):
        style_parts.append(f"Be {speech['formality']}")
    if speech.get("first_person"):
        style_parts.append("Speak in first person (use 'I', 'me', 'my')")
    if speech.get("max_sentences"):
        parts.append(
            f"Keep responses concise — {speech['max_sentences']} sentences "
            f"for simple questions, longer only for complex topics.")
    if style_parts:
        parts.append(". ".join(style_parts) + ".")

    # --- Things to avoid ---
    never = speech.get("never_say") or []
    if never:
        quoted = ", ".join(f'"{s}"' for s in never)
        parts.append(f"Never say {quoted}.")

    # --- Boundaries ---
    bounds = profile.boundaries
    boundary_parts: List[str] = []
    if bounds.get("refuse_harmful"):
        boundary_parts.append("refuse harmful requests")
    if bounds.get("honest_about_limitations"):
        boundary_parts.append("be honest about your limitations")
    if bounds.get("no_impersonation"):
        boundary_parts.append("never impersonate others without permission")
    if boundary_parts:
        parts.append("You " + ", ".join(boundary_parts) + ".")

    # --- Proactivity: what the agent may originate, not just answer ---
    # The operator's permission switches, rendered as instructions. `offer_help`
    # is written in the negative when off: the default is to not volunteer, and a
    # model that is told nothing tends to help itself to helpfulness.
    switches = normalise_proactivity(getattr(profile, "proactivity", None))
    proactive_parts: List[str] = []
    if switches["greet_on_arrival"]:
        proactive_parts.append("greet someone when they arrive")
    if switches["ask_follow_up"]:
        proactive_parts.append(
            "ask a follow-up question when it would genuinely help")
    if switches["offer_help"]:
        proactive_parts.append(
            "offer help you were not asked for when you notice it is needed")
    if proactive_parts:
        parts.append("You may " + ", ".join(proactive_parts) + ".")
    if not switches["offer_help"]:
        parts.append("Do not volunteer help that was not asked for.")

    return "\n".join(parts)
