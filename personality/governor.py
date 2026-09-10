"""
Personality Governor -- the personality model as a structured reasoning layer.

The personality model is no longer only a prompt-shaping input (the old
one-way street: as_profile() -> system prompt). It is now a first-class
signal the decision engine consults on every proposed action:

    proposed action  ->  governor.annotate()  ->  PersonalityVerdict
                                                ->  governor.apply()  ->  (possibly rerouted) action

The verdict annotates the action with tone, verbosity and appropriateness
scores derived *deterministically* from the living trait vector and frozen
policy.  The apply() step can modify params or reroute (e.g. speak ->
ask_user, or truncate an over-verbose response).

Safety invariant (non-negotiable)
---------------------------------
The personality governor governs a *different axis* than the safety governor
(ExecutionGovernor) and the policy gate (ApprovalBroker / ConsentRegistry /
CapabilityRegistry).  Those remain dominant on the harm / consent / approval
axis.  The personality governor can only RESTRICT or MODIFY, never AUTHORIZE:

  * a personality PASS does NOT override a policy BLOCK;
  * a personality MODIFY cannot reclassify a side-effecting action as safe;
  * a personality verdict never unlocks consent or approval.

All verdicts are journaled (the decision engine attaches them to the decision
dict) so the personality signal is as auditable as the safety signal.
"""
import re
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

from personality.model import PersonalityModel


# Appropriateness at or below this forces a reroute to a safe fallback.
BLOCK_THRESHOLD = 0.3

# A single trait may not push tone_score below this per-dimension floor.
_TONE_FLOOR = 0.2


# ---------------------------------------------------------------------------
# Verdict
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class PersonalityVerdict:
    """Structured personality annotation of a proposed action.

    verdict: "pass" | "modify" | "reroute"
    tone_score: 0..1 -- how well text matches warmth/formality traits.
    verbosity_ok: bool -- within the verbosity budget.
    appropriateness: 0..1 -- 1.0 clean; <= BLOCK_THRESHOLD forces reroute.
    adjustments: concrete param changes for modify/reroute.
    route_to: action type to redirect to on reroute.
    reason: human-readable, auditable explanation.
    """
    verdict: str = "pass"
    tone_score: float = 1.0
    verbosity_ok: bool = True
    appropriateness: float = 1.0
    adjustments: dict = field(default_factory=dict)
    route_to: Optional[str] = None
    reason: str = ""

    def to_dict(self) -> dict:
        return {"verdict": self.verdict, "tone_score": self.tone_score,
                "verbosity_ok": self.verbosity_ok,
                "appropriateness": self.appropriateness,
                "adjustments": self.adjustments, "route_to": self.route_to,
                "reason": self.reason}

# ---------------------------------------------------------------------------
# Governor
# ---------------------------------------------------------------------------

class PersonalityGovernor:
    """Deterministic, auditable personality reasoning layer.

    Wraps a living ``PersonalityModel`` and exposes a small structured policy
    derived from its trait vector and frozen policy.  No LLM calls, no I/O --
    annotate() is O(text-length) string ops, safe for the 1 Hz agent hot path.
    """

    def __init__(self, model: PersonalityModel):
        self.model = model

    # -- structured policy ---------------------------------------------------

    @property
    def policy(self) -> dict:
        """The derived structured policy -- the governor's 'mind'.

        Deterministic function of the model's trait vector + frozen policy.
        Consulted by annotate(); exposed for inspection/auditing.
        """
        voice = self.model.voice
        traits = self.model.traits
        max_sentences = int(voice.get("max_sentences", 3))
        verbosity = traits["verbosity"].value if "verbosity" in traits else 0.5
        warmth = traits["warmth"].value if "warmth" in traits else 0.5
        formality = traits["formality"].value if "formality" in traits else 0.3
        proactivity = traits["proactivity"].value if "proactivity" in traits else 0.3

        # Verbosity budget: high-verbosity trait raises the cap; low lowers it.
        # Floor of 1 sentence, ceiling of 8.
        verbosity_budget = max(1, min(8, round(max_sentences * (0.5 + verbosity))))

        return {
            "verbosity_budget": verbosity_budget,
            "warmth": warmth,
            "formality": formality,
            "proactivity": proactivity,
            "tone": "formal" if formality >= 0.5 else "casual",
            "proactivity_threshold": 0.4,
            "content_rules": {
                "never_say": list(self.model.policy.get("never_say") or []),
                "boundaries": dict(self.model.policy.get("boundaries") or {}),
                "first_person": bool(self.model.policy.get("first_person", True)),
                "refuse_harmful": bool(self.model.policy.get("refuse_harmful", True)),
            },
        }

    # -- core: annotate ------------------------------------------------------

    def annotate(self, proposed_action: dict, context: Optional[dict] = None
                 ) -> "PersonalityVerdict":
        """Annotate a proposed action with a personality verdict.

        Always returns a verdict (never raises).  ``context`` may carry
        ``self_initiated`` (bool) to enable the proactivity check.
        """
        verdict = PersonalityVerdict()
        try:
            action_type = proposed_action.get("action_type")
            params = proposed_action.get("params") or {}
            text = self._extract_text(action_type, params)
            pol = self.policy
            rules = pol["content_rules"]

            # 1. Content rules (never_say / boundaries) -- the hard gate.
            #    A hit is a direct violation of frozen policy: force reroute.
            hits = [phrase for phrase in rules["never_say"]
                    if text and phrase and phrase.lower() in text.lower()]
            if hits:
                verdict.appropriateness = 0.0
                verdict.verdict = "reroute"
                verdict.route_to = "speak"
                verdict.adjustments = {
                    "text": "I'd rather not say that.",
                    "route_to": "speak",
                    "personality_blocked": hits,
                }
                verdict.reason = f"content rule hit: forbidden phrase(s) {hits!r}"
                return verdict

            # 2. Verbosity check.
            if text:
                sentence_count = self._count_sentences(text)
                verdict.verbosity_ok = sentence_count <= pol["verbosity_budget"]
                if not verdict.verbosity_ok:
                    verdict.tone_score = max(
                        _TONE_FLOOR,
                        verdict.tone_score - 0.1 * (
                            sentence_count - pol["verbosity_budget"]))

            # 3. Tone / register match.
            if text:
                verdict.tone_score = self._tone_match(text, pol)

            # 4. Proactivity check -- only for self-initiated speaks.
            is_self_initiated = bool(context and context.get("self_initiated"))
            if (is_self_initiated and action_type == "speak"
                    and pol["proactivity"] < pol["proactivity_threshold"]):
                verdict.verdict = "reroute"
                verdict.route_to = "ask_user"
                verdict.adjustments = {"route_to": "ask_user"}
                verdict.reason = (
                    f"proactivity suppressed: trait {pol['proactivity']:.2f} "
                    f"< threshold {pol['proactivity_threshold']:.2f}")
                return verdict

            # 5. Disposition.
            if not verdict.verbosity_ok:
                verdict.verdict = "modify"
                truncated = self._truncate_to_budget(text, pol["verbosity_budget"])
                verdict.adjustments = {"text": truncated}
                verdict.reason = (
                    f"verbosity {sentence_count} > budget "
                    f"{pol['verbosity_budget']}; truncated")
            else:
                verdict.verdict = "pass"
                verdict.reason = "within personality policy"

            return verdict
        except Exception as exc:
            # Fail open (pass) but record the reason for audit.
            verdict.verdict = "pass"
            verdict.reason = f"governor error (fail open): {exc}"
            return verdict

    # -- core: apply ---------------------------------------------------------

    def apply(self, proposed_action: dict, verdict: "PersonalityVerdict") -> dict:
        """Return the (possibly modified / rerouted) action.

        Pure function of action + verdict: never re-consults the model.
        The safety gate still runs AFTER this on the returned action --
        personality never pre-approves.
        """
        if verdict.verdict == "pass" or not verdict.adjustments:
            return proposed_action

        action = dict(proposed_action)
        params = dict(action.get("params") or {})

        if verdict.verdict == "reroute":
            route_to = verdict.adjustments.get("route_to", verdict.route_to)
            if route_to:
                action["action_type"] = route_to
            if "text" in verdict.adjustments:
                params["text"] = verdict.adjustments["text"]
            action["params"] = params
            action["personality_rerouted"] = True
            return action

        if verdict.verdict == "modify":
            for key, value in verdict.adjustments.items():
                params[key] = value
            action["params"] = params
            action["personality_modified"] = True
            return action

        return action

    # -- helpers -------------------------------------------------------------

    @staticmethod
    def _extract_text(action_type, params: dict) -> str:
        """Pull the human-readable text out of an action's params."""
        if not params:
            return ""
        for key in ("text", "question", "content", "message"):
            val = params.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()
        return ""

    @staticmethod
    def _count_sentences(text: str) -> int:
        """Robust sentence count -- splits on terminal punctuation."""
        if not text:
            return 0
        parts = re.split(r'[.!?]+', text)
        count = len([p for p in parts if p.strip()])
        return max(1, count)

    def _truncate_to_budget(self, text: str, budget: int) -> str:
        """Keep at most ``budget`` sentences, preserving terminal punctuation."""
        sentences = re.split(r'(?<=[.!?])\s+', text)
        kept = sentences[:budget]
        result = " ".join(kept).strip()
        if result and not re.search(r'[.!?]\s*$', result):
            result += "."
        return result

    def _tone_match(self, text: str, pol: dict) -> float:
        """Score how well the text's register matches the formality trait.

        Deterministic heuristic: contraction density => casual register.
        Returns 0..1 (1.0 = perfect match).
        """
        if not text:
            return 1.0
        words = text.split()
        if not words:
            return 1.0
        contractions = len(re.findall(
            r"\w+n't|\w+'re|\w+'ll|\w+'ve|\w+'d|\w+'m", text))
        density = contractions / len(words)
        casual_score = min(1.0, density / 0.15)
        formality = pol["formality"]
        if formality >= 0.5:
            score = max(_TONE_FLOOR, 1.0 - casual_score)
        else:
            score = max(_TONE_FLOOR, casual_score + 0.3)
        return max(0.0, min(1.0, score))
