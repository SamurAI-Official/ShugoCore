"""
Dynamic prompt builder.

Assembles the full prompt for the conversational fast path. Combines:
  1. System: personality + speech rules (from personality.prompt)
  2. Perception: who is present, gaze, environment
  3. Memory: relevant entity facts from Tier-2 memory
  4. History: last N conversation turns
  5. Instruction: respond to what was just said

The output is a single prompt string ready to send to the model.
The model is instructed to respond with a speak action in JSON.
"""
import logging
from typing import Any, Dict, List, Optional

from personality.prompt import personality_system_prompt
from personality.loader import PersonalityProfile

logger = logging.getLogger(__name__)


def build_conversational_prompt(
    transcript: str,
    personality: PersonalityProfile,
    history_text: str = "",
    facts: Optional[List[str]] = None,
    perception: Optional[Dict[str, Any]] = None,
) -> str:
    """Build a full conversational prompt.

    Args:
        transcript: What the user just said.
        personality: Shugo's personality profile.
        history_text: Formatted conversation history ('ROLE: text' lines).
        facts: Relevant memory facts to inject.
        perception: Dict with person_present, face_count, gaze_direction, etc.

    Returns:
        Complete prompt string for the model.
    """
    sections: List[str] = []

    # 1. System: personality
    sections.append(personality_system_prompt(personality))

    # 2. Perception context
    perc = perception or {}
    perc_parts: List[str] = []
    if perc.get("person_present"):
        fc = perc.get("face_count", 1)
        perc_parts.append(f"You can see {fc} person(s) in front of you.")
        gaze = perc.get("gaze_direction")
        if gaze == "toward_camera":
            perc_parts.append("They are looking at you.")
        elif gaze == "away":
            perc_parts.append("They are looking away.")
    if perc.get("speech_source"):
        src = perc["speech_source"]
        if src == "instruction_directed":
            perc_parts.append("They are speaking directly to you by name.")
        elif src == "verified_person":
            perc_parts.append("A visible person is talking to you.")
    if perc_parts:
        sections.append(" ".join(perc_parts))

    # 3. Memory facts
    if facts:
        fact_lines = []
        for f in facts[:5]:
            fact_lines.append(f"  - {f}")
        if fact_lines:
            sections.append("Relevant facts:\n" + "\n".join(fact_lines))

    # 4. Conversation history
    if history_text:
        sections.append(f"Recent conversation:\n{history_text}")

    # 5. The user's current message
    sections.append(f'The person just said to you: "{transcript}"')

    # 6. Response instruction
    sections.append(
        "Respond naturally in first person. Be warm, concise and helpful. "
        "Ask a follow-up question if appropriate. "
        "Respond ONLY with a single-line JSON object: "
        '{"action_type": "speak", "params": {"text": "your response here"}, "confidence": 0.9}'
    )

    return "\n\n".join(sections)
