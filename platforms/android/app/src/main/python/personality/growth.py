"""Personality growth — memory-driven development of the PersonalityModel.

Phase B2 of the CSFA architecture. The growth cycle is deterministic in
v1: it reads what happened since the last generation (facts learned,
question ratio, commands, tool outcomes, explicit user feedback) and
synthesizes clamped trait deltas. The model, not the memory, decides how
far any single generation may move (GROWTH_RATE).

    memory (what happened) -> growth synthesis -> PersonalityModel gen n+1
                                                           -> compare to gen n
"""
import re
from typing import Any, Dict, List, Optional, Tuple

from personality.model import PersonalityModel

# Feedback lexicon: explicit user feedback about HOW Shugo is being,
# not what Shugo knows. Maps trait -> (positive patterns, negative ones).
FEEDBACK_PATTERNS: Dict[str, Tuple[Tuple[str, ...], Tuple[str, ...]]] = {
    "verbosity": (
        (r"more detail", r"explain more", r"go on", r"tell me more",
         r"longer answers?", r"in depth"),
        (r"talk(ing)? too much", r"too long", r"be (more )?(brief|short(er)?)",
         r"less detail", r"stop rambling", r"shorter answers?"),
    ),
    "humor": (
        (r"you'?re funny", r"so funny", r"love your jokes", r"hilarious",
         r"crack(s|ing)? me up", r"good (one|joke)"),
        (r"not funny", r"annoying", r"cringe", r"bad joke", r"too silly"),
    ),
    "warmth": (
        (r"thank(s| you)", r"you'?re the best", r"love you",
         r"appreciate (you|that)", r"you'?re (sweet|kind)"),
        (r"you (don'?t|do not) care", r"so? cold", r"(how )?rude",
         r"you'?re heartless"),
    ),
}


def extract_feedback(transcript: str) -> Dict[str, int]:
    """Scan one utterance for explicit personality feedback.

    Returns net counts per trait: positive values praise the current
    behavior (reinforce), negative values criticize it (adjust).
    """
    text = (transcript or "").lower()
    if not text:
        return {}
    net: Dict[str, int] = {}
    for trait, (pos, neg) in FEEDBACK_PATTERNS.items():
        hits = 0
        for pattern in pos:
            if re.search(pattern, text):
                hits += 1
        for pattern in neg:
            if re.search(pattern, text):
                hits -= 1
        if hits:
            net[trait] = net.get(trait, 0) + hits
    return net


def synthesize_deltas(stats: Dict[str, Any]) -> Tuple[Dict[str, float], str]:
    """Turn interaction stats into proposed trait deltas + reasons.

    Stats keys (all optional, all best-effort):
        turns          — conversational turns since last generation
        questions      — how many were questions
        commands       — how many were commands
        tool_failures  — failed tool executions
        tool_runs      — total tool executions
        new_facts      — durable facts learned from the user
        feedback       — net feedback counts, e.g. {"verbosity": -2}
    """
    turns = max(0, int(stats.get("turns") or 0))
    if turns == 0:
        return {}, ""
    questions = max(0, int(stats.get("questions") or 0))
    commands = max(0, int(stats.get("commands") or 0))
    tool_runs = max(0, int(stats.get("tool_runs") or 0))
    tool_failures = max(0, int(stats.get("tool_failures") or 0))
    new_facts = max(0, int(stats.get("new_facts") or 0))
    feedback = dict(stats.get("feedback") or {})

    q_ratio = questions / turns
    command_ratio = commands / turns
    fail_ratio = (tool_failures / tool_runs) if tool_runs else 0.0

    deltas: Dict[str, float] = {}
    reasons: List[str] = []

    def bump(trait: str, amount: float, reason: str) -> None:
        deltas[trait] = deltas.get(trait, 0.0) + amount
        reasons.append(reason)

    # Inquisitive users grow a more curious companion.
    if q_ratio >= 0.3:
        bump("curiosity", 0.08, f"questions {q_ratio:.0%}")
    # Sharing facts is an act of trust — meet it with warmth.
    if new_facts >= 3:
        bump("warmth", 0.06, f"{new_facts} facts shared")
    elif new_facts >= 1:
        bump("warmth", 0.04, f"{new_facts} fact(s) shared")
    # Long sessions deepen the bond.
    if turns >= 20:
        bump("warmth", 0.05, f"long session ({turns} turns)")
    # A command-heavy user wants initiative.
    if command_ratio >= 0.5:
        bump("proactivity", 0.05, f"commands {command_ratio:.0%}")
    # Repeated tool failure should soften assertiveness.
    if fail_ratio > 0.3:
        bump("proactivity", -0.06, f"tool failures {fail_ratio:.0%}")
    # Explicit feedback dominates — the user is literally telling us.
    for trait, net in feedback.items():
        if net > 0:
            bump(trait, min(0.1, 0.06 * net), f"praised {trait}")
        elif net < 0:
            bump(trait, max(-0.1, 0.06 * net), f"criticized {trait}")

    deltas = {t: round(v, 4) for t, v in deltas.items() if v}
    return deltas, "; ".join(reasons)


def grow_from_memory(model: PersonalityModel,
                     stats: Dict[str, Any],
                     note: str = "memory-driven growth") -> Optional[Dict[str, Any]]:
    """One growth generation: synthesize deltas from stats, apply them
    (clamped by the model), and return a report — or None when there is
    nothing to learn. The caller is responsible for persistence.
    """
    deltas, reasons = synthesize_deltas(stats)
    if not deltas:
        return None
    applied = model.apply_delta(deltas, note=f"{note}: {reasons}")
    if not applied:
        return None
    return {"generation": model.generation, "applied": applied,
            "reasons": reasons}