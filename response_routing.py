#!/usr/bin/env python3
"""Which device should answer? The one closest to the operator.

A hive has several mouths but one operator: the primary decides *what* to say and
the nearest device to that human says it. This module turns the perception facts
each node already publishes (camera face/gaze, VAD, speech attribution, recent
utterance, attention verdict) into a proximity score per device, so the choice is
measured rather than assumed -- and a device that cannot speak is never chosen,
however close it is.

Scoring is deliberately simple and inspectable (see ``SCORE_WEIGHTS``), it decays
with the age of the evidence, and it reports *why* a candidate won or lost.
Silence is the honest answer when nobody reports the operator present: the agent
never picks a device at random and speaks into an empty room.
"""
from typing import Any, Dict, Iterable, List, Optional, Tuple

# What each signal is worth, and why. Faces and directed speech dominate because
# they are the signals that mean "a person is here, with us"; ambient audio
# contributes nothing on its own (a TV is not an operator).
SCORE_WEIGHTS = {
    "face_present": 0.35,
    "gaze_toward_camera": 0.15,
    "speech_directed": 0.30,
    "speech_person": 0.20,
    "voice_active": 0.10,
    "utterance_recent_s": 5.0,
    "utterance_recent": 0.10,
    "utterance_stale_s": 15.0,
    "utterance_stale": 0.05,
    "attention_attending": 0.10,
}
# Evidence older than this is discounted, then ignored: a face seen two minutes
# ago does not mean the operator is standing there now.
_STALE_HALF_LIFE_S = 15.0
_IGNORE_AFTER_S = 45.0
# Below this a candidate is not "near" by any useful measure.
DEFAULT_FLOOR = 0.20

_DIRECTED = {"instruction_directed", "verified_person", "person_talking"}
_AMBIENT = {"ambient_noise", "unattributed_audio", "unattributed_speech", "none", ""}


def normalise_facts(facts: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Accept both local and ``remote_*`` spellings for the same facts.

    The primary reads its own observation with local keys and a peer's facts with
    the ``remote_`` prefix the shell streams, and both must score identically.
    """
    out: Dict[str, Any] = {}
    for key, value in (facts or {}).items():
        bare = key[7:] if str(key).startswith("remote_") else str(key)
        out[bare] = value
    faces = out.get("face_count")
    if "face_present" not in out and isinstance(faces, (int, float)):
        out["face_present"] = int(faces) > 0
    return out


def proximity_score(facts: Optional[Dict[str, Any]], *,
                    age_s: Optional[float] = None) -> Tuple[float, List[str]]:
    """(score 0..1, reasons) for one device. Pure: no clocks, no I/O."""
    f = normalise_facts(facts)
    score = 0.0
    why: List[str] = []
    if f.get("face_present"):
        score += SCORE_WEIGHTS["face_present"]
        why.append("face")
    if f.get("gaze_toward_camera"):
        score += SCORE_WEIGHTS["gaze_toward_camera"]
        why.append("gaze")
    source = str(f.get("speech_source") or "").strip().lower()
    if source in _DIRECTED:
        score += SCORE_WEIGHTS["speech_directed"]
        why.append(f"speech:{source}")
    elif source and source not in _AMBIENT:
        score += SCORE_WEIGHTS["speech_person"]
        why.append(f"speech:{source}")
    if f.get("voice_active"):
        score += SCORE_WEIGHTS["voice_active"]
        why.append("voice")
    utterance_age = f.get("utterance_age_s", f.get("transcript_age_s"))
    if isinstance(utterance_age, (int, float)):
        if utterance_age <= SCORE_WEIGHTS["utterance_recent_s"]:
            score += SCORE_WEIGHTS["utterance_recent"]
            why.append("just-spoke")
        elif utterance_age <= SCORE_WEIGHTS["utterance_stale_s"]:
            score += SCORE_WEIGHTS["utterance_stale"]
            why.append("spoke-recently")
    elif f.get("speech_recent"):
        score += SCORE_WEIGHTS["utterance_recent"]
        why.append("speech-recent")
    attention = str(f.get("attention_state") or "").strip().lower()
    if attention == "attending":
        score += SCORE_WEIGHTS["attention_attending"]
        why.append("attending")
    elif attention in ("absent", "diverted") and not f.get("face_present"):
        # The attention layer says the human is NOT engaged here: whatever else
        # this device heard, it is not the place to answer.
        return 0.0, [f"attention:{attention}"]
    elif attention in ("absent", "diverted"):
        why.append(f"attention:{attention}")
    if isinstance(age_s, (int, float)):
        if age_s >= _IGNORE_AFTER_S:
            return 0.0, why + [f"stale({age_s:.0f}s)"]
        if age_s > _STALE_HALF_LIFE_S:
            score *= 0.5
            why.append(f"aged({age_s:.0f}s)")
    return min(1.0, round(score, 3)), why


def rank(candidates: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Score every candidate and order them deterministically.

    Order: score desc, then speaking ability, then election priority (lower
    wins), then device id -- so two runs on identical evidence pick the same
    mouth.
    """
    scored: List[Dict[str, Any]] = []
    for candidate in candidates or []:
        if not isinstance(candidate, dict):
            continue
        device = str(candidate.get("device_id") or "").strip()
        if not device:
            continue
        score, why = proximity_score(candidate.get("facts"),
                                     age_s=candidate.get("age_s"))
        scored.append({"device_id": device, "score": score, "why": why,
                       "can_speak": bool(candidate.get("can_speak", True)),
                       "is_self": bool(candidate.get("is_self")),
                       "priority": int(candidate.get("priority", 500))})
    scored.sort(key=lambda c: (-c["score"], not c["can_speak"],
                               c["priority"], c["device_id"]))
    return scored


def select(candidates: Iterable[Dict[str, Any]], *,
           floor: float = DEFAULT_FLOOR,
           forced: Optional[str] = None) -> Tuple[Optional[Dict[str, Any]], str]:
    """(chosen candidate or None, reason). Never chooses a device at random."""
    ranked = rank(candidates)
    speaking = [c for c in ranked if c["can_speak"]]
    if forced and str(forced).strip() not in ("", "auto"):
        wanted = str(forced).strip()
        for candidate in ranked:
            if candidate["device_id"] != wanted:
                continue
            if not candidate["can_speak"]:
                return None, (f"operator forced '{wanted}' but that device "
                              f"reports no speech output")
            return candidate, f"operator forced '{wanted}'"
        return None, f"operator forced '{wanted}' but it is not a known device"
    if not speaking:
        return None, "no device reports speech output"
    best = speaking[0]
    if best["score"] < floor:
        return None, (f"nobody reports the operator present (best is "
                      f"{best['device_id']} at {best['score']:.2f} < "
                      f"{floor:.2f})")
    return best, (f"{best['device_id']} is closest to the operator "
                  f"(score {best['score']:.2f}: "
                  f"{', '.join(best['why']) or 'no signals'})")

