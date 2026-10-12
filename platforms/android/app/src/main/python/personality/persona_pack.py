"""The persona pack contract — how a personality model arrives from outside.

ShugoCore does not train personality models and does not produce weights (see the
scope note in the README). What it does is *accept* them. A persona pack is a
versioned, hash-pinned record of a personality produced elsewhere — the trainer and
any fine-tuned weights live in their own repository — and this module is the
boundary that decides whether a given pack may be adopted by a node.

Open-ended by construction, in three places:

* ``traits`` is an **open vocabulary**. A pack may carry traits this build has never
  heard of: a trainer that learns ``playfulness`` is not blocked by a governor that
  only knows six. Unknown traits are kept, because the character belongs to the pack
  — the governor simply has nothing to say about them, and they persist and grow
  with the rest.
* ``voice_model`` is an optional **reference** to trained weights (an adapter, a
  gguf, whatever the trainer produced), recorded with its uri, sha256, base model
  and licence. ShugoCore records what an operator pointed it at; it never fetches,
  converts or produces weights itself.
* ``schema`` is versioned, and an unrecognised one is **refused**, so a future
  format fails loudly here rather than being half-understood.

Fail-closed, and *additive-only* where safety is at stake. A pack may add to the
frozen perimeters and never remove from them: ``never_say`` is unioned and every
boundary boolean is OR-ed, so a persona from anywhere cannot talk a node out of its
own boundaries. A pack also cannot touch the **proactivity permissions** — those are
the operator's grant, not the artifact's, and a pack able to grant ``offer_help``
would be an artifact that changes what a node is allowed to *do*.
"""
import json
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Tuple

from personality.loader import PersonalityProfile, normalise_proactivity
# `_extract_policy` is the same helper `genesis()` uses to freeze a node's own
# perimeters; comparing against it is what lets the report say "the pack reinforced
# this boundary" rather than re-deriving the answer a second way and drifting.
from personality.model import (BASELINE_TRAITS, PersonalityModel, TraitState,
                               _extract_policy)

# The one schema this build understands. A pack naming anything else is refused by
# name, because guessing at a format is how a half-understood character ships.
PERSONA_SCHEMA = "shugocore.persona/1"

# Trait names are an open vocabulary, but they are *identifiers*: a name that is not
# a clean slug cannot be read back, logged or grown by key.
_TRAIT_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
_PACK_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")


class PersonaError(ValueError):
    """A pack that may not be adopted, carrying the reason it was refused."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _unit_float(value: Any, where: str) -> float:
    """A number in [0, 1], or a refusal naming the field."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PersonaError(f"{where} must be a number in [0, 1], got {value!r}")
    number = float(value)
    if not 0.0 <= number <= 1.0:
        raise PersonaError(f"{where} must be in [0, 1], got {number}")
    return round(number, 6)


def traits_from_raw(raw: Any, where: str = "traits") -> Dict[str, TraitState]:
    """Read a pack's trait block into ``TraitState``s.

    Two accepted forms, because a pack may be hand-written or machine-produced::

        "warmth": 0.62
        "warmth": {"value": 0.62, "confidence": 0.4, "evidence_count": 90}

    A bare number is a value with no evidence behind it. Unknown trait *names* are
    deliberately accepted — that is the open vocabulary; malformed *values* are
    refused, because a trait outside [0, 1] is not a character, it is a bug.
    """
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise PersonaError(f"{where} must be an object, got {type(raw).__name__}")
    traits: Dict[str, TraitState] = {}
    for name, spec in raw.items():
        key = str(name)
        if not _TRAIT_NAME_RE.match(key):
            raise PersonaError(
                f"{where}: {key!r} is not a usable trait name (lowercase letters, "
                f"digits and underscores, up to 32 characters)")
        if isinstance(spec, dict):
            traits[key] = TraitState(
                value=_unit_float(spec.get("value"), f"{where}.{key}.value"),
                confidence=_unit_float(spec.get("confidence", 0.0),
                                       f"{where}.{key}.confidence"),
                evidence_count=max(0, int(spec.get("evidence_count") or 0)),
                last_gen=max(0, int(spec.get("last_gen") or 0)))
        else:
            traits[key] = TraitState(value=_unit_float(spec, f"{where}.{key}"))
    return traits


@dataclass
class PersonaPack:
    """A validated persona: what it declares, and what it is allowed to change."""

    schema: str
    persona_id: str
    version: str
    name: str = ""
    traits: Dict[str, TraitState] = field(default_factory=dict)
    voice: Dict[str, Any] = field(default_factory=dict)
    speech: Dict[str, Any] = field(default_factory=dict)
    policy: Dict[str, Any] = field(default_factory=dict)
    proactivity: Dict[str, bool] = field(default_factory=dict)
    voice_model: Optional[Dict[str, Any]] = None
    provenance: Dict[str, Any] = field(default_factory=dict)
    licence: Dict[str, Any] = field(default_factory=dict)
    digest: str = ""
    declared_hash: Optional[str] = None
    source: str = ""

    @property
    def may_redistribute(self) -> bool:
        """False only when the licence says so explicitly.

        An artifact published *for adoption* is presumed redistributable; one that
        says otherwise must not be committed to this MIT repository — the same
        question MODELS.md decides for every other model the project touches.
        """
        return self.licence.get("redistributable") is not False

    @property
    def identity(self) -> str:
        """``id@version`` — what a node records as the character's origin."""
        return f"{self.persona_id}@{self.version}"

    def meta(self, adopted_at: Optional[float] = None) -> Dict[str, Any]:
        """The provenance this node stores on the model it was born from."""
        record: Dict[str, Any] = {
            "id": self.persona_id,
            "version": self.version,
            "schema": self.schema,
            "digest": self.digest,
            "adopted_at": adopted_at if adopted_at is not None else time.time(),
        }
        if self.source:
            record["source"] = self.source
        if self.voice_model:
            # Recorded, never fetched: enough to identify the weights without this
            # process pretending it produced or verified them.
            record["voice_model"] = {
                key: self.voice_model.get(key)
                for key in ("kind", "uri", "sha256", "base", "licence")
                if self.voice_model.get(key) is not None}
        return record


def compute_digest(payload: Dict[str, Any]) -> str:
    """The pack's canonical hash, using the engine's own hashing primitive.

    `security.canonical_hash` is what binds a policy verdict to a decision, so a
    persona carries the same kind of tamper-evident digest as everything else that
    crosses a trust boundary here.
    """
    from security import canonical_hash
    body = {k: v for k, v in (payload or {}).items() if k != "hash"}
    return canonical_hash(body)


def from_dict(data: Any, source: str = "") -> PersonaPack:
    """Validate a raw pack, or raise ``PersonaError`` naming the problem.

    Every refusal here is deliberate: an unrecognised schema, an unusable id, a
    trait outside [0, 1], a weight reference with no uri, or content that does not
    match the hash the pack carries. A persona is adopted once and then *lives* on a
    node, so the last moment to notice something wrong is now.
    """
    if not isinstance(data, dict):
        raise PersonaError(
            f"a persona pack must be an object, got {type(data).__name__}")

    schema = str(data.get("schema") or "")
    if schema != PERSONA_SCHEMA:
        raise PersonaError(
            f"unsupported persona schema {schema or '(missing)'!r}; this build "
            f"reads {PERSONA_SCHEMA!r}")

    persona_id = str(data.get("persona_id") or "")
    if not _PACK_ID_RE.match(persona_id):
        raise PersonaError(
            f"persona_id {persona_id or '(missing)'!r} is not usable (lowercase "
            f"letters, digits, dot, dash and underscore, up to 64 characters)")

    version = str(data.get("version") or "").strip()
    if not version or len(version) > 64:
        raise PersonaError("a persona pack must declare a version (up to 64 chars)")

    for key in ("licence", "provenance", "voice", "speech", "policy",
                "voice_model"):
        value = data.get(key)
        if value is not None and not isinstance(value, dict):
            raise PersonaError(f"{key} must be an object, got {type(value).__name__}")

    voice_model = data.get("voice_model")
    if isinstance(voice_model, dict) and not str(voice_model.get("uri") or "").strip():
        # A reference to weights that does not say where they are cannot be acted
        # on, or audited, or refused for a licence reason later.
        raise PersonaError("voice_model must carry a uri")

    digest = compute_digest(data)
    declared = data.get("hash")
    if declared is not None and str(declared) != digest:
        raise PersonaError(
            "the pack does not match its own hash: declared "
            f"{str(declared)[:16]}…, computed {digest[:16]}… — it has been edited "
            "or corrupted since it was produced")

    return PersonaPack(
        schema=schema,
        persona_id=persona_id,
        version=version,
        name=str(data.get("name") or ""),
        traits=traits_from_raw(data.get("traits")),
        voice=dict(data.get("voice") or {}),
        speech=dict(data.get("speech") or {}),
        policy=dict(data.get("policy") or {}),
        # Recorded for the report, never applied: permissions are the operator's.
        proactivity=normalise_proactivity(data.get("proactivity")),
        voice_model=dict(voice_model) if isinstance(voice_model, dict) else None,
        provenance=dict(data.get("provenance") or {}),
        licence=dict(data.get("licence") or {}),
        digest=digest,
        declared_hash=str(declared) if declared is not None else None,
        source=source)


def load(path: str) -> PersonaPack:
    """Read and validate a persona pack from a file. Never a traceback."""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except OSError as exc:
        raise PersonaError(
            f"cannot read {path}: {exc.strerror or exc}") from exc
    except ValueError as exc:
        raise PersonaError(f"{path} is not valid JSON: {exc}") from exc
    return from_dict(data, source=os.path.basename(str(path)))


# ---------------------------------------------------------------------------
# Adoption -- additive only
# ---------------------------------------------------------------------------

def _merge_never_say(local: Any, declared: Any) -> Tuple[list, list]:
    """Union the forbidden phrases. Returns ``(merged, added)``.

    Union rather than replace is the whole point: a persona that ships its own
    ``never_say`` must not be able to *remove* one this node already has.
    """
    merged = [str(p).strip() for p in (local or []) if str(p).strip()]
    added: list = []
    for phrase in (declared or []):
        text = str(phrase).strip()
        if text and text not in merged:
            merged.append(text)
            added.append(text)
    return merged, added


def _merge_boundaries(local: Any, declared: Any) -> Tuple[dict, list, list]:
    """OR the boundary booleans per key. Returns ``(merged, reinforced, refused)``.

    A pack may switch a boundary **on** and may never switch one **off**: relaxing
    what a node refuses is not a personality change, and an artifact that can do it
    is one that can talk a node out of its own perimeters.
    """
    merged = dict(local or {})
    reinforced: list = []
    refused: list = []
    for key, value in (declared or {}).items():
        if not isinstance(value, bool):
            refused.append(f"{key}={value!r} is not a boolean")
        elif value and not merged.get(key):
            merged[key] = True
            reinforced.append(key)
        elif not value and merged.get(key):
            refused.append(f"{key}=false cannot relax a boundary this node has on")
        else:
            merged.setdefault(key, value)
    return merged, reinforced, refused


def _or_flag(local: Any, declared: Any) -> bool:
    """True wins: a pack may assert a perimeter, never clear one."""
    return bool(local) or bool(declared)


def birth(pack: PersonaPack, local: PersonalityProfile, *,
          adopted_at: Optional[float] = None
          ) -> Tuple[PersonalityModel, Dict[str, Any]]:
    """Birth a living model from ``pack`` over a node's own config.

    Returns ``(model, report)``. The report names what was adopted, what was added
    and what was **refused**, so a node's log can answer "what did this artifact
    actually change?" without re-reading the pack — and so a pack that tried to
    relax a boundary leaves a trace rather than a shrug.

    Two things a pack is allowed to own outright, because they *are* the character:
    the trait vector (including traits this build has never heard of, which are kept
    and grown but not yet reasoned about) and the speech params/wording. Two things
    it may only add to: the frozen perimeters and the forbidden phrases. One thing
    it may not touch at all: the proactivity permissions, which are the operator's
    grant and not an artifact's.

    The generation starts at 0 regardless of how much training went into the pack:
    the counter measures growth *this node* has applied, and the pack's own training
    history travels in ``provenance`` and in the model's ``persona`` record.
    """
    model = PersonalityModel.genesis(local)
    report: Dict[str, Any] = {
        "pack": pack.identity, "digest": pack.digest,
        "traits": [], "added_traits": [], "wording": [], "voice": [],
        "reinforced": [], "refused": [], "warnings": [],
    }

    # 1. The trait vector -- the character. Replaces known values (a trained model
    #    brings its own evidence), and extends the vocabulary with what it knows.
    for name, state in pack.traits.items():
        model.traits[name] = state
        if name in BASELINE_TRAITS:
            report["traits"].append(name)
        else:
            report["added_traits"].append(name)
            report["warnings"].append(
                f"trait {name!r} is not one this build reasons about yet: kept and "
                f"grown with the rest, but no rule consults it")

    # 2. Voice and wording.
    for key in ("max_sentences", "style"):
        if pack.voice.get(key) is not None:
            model.voice[key] = pack.voice[key]
            report["voice"].append(key)
    for key in ("greeting", "offer"):
        declared = pack.speech.get(key)
        if isinstance(declared, str) and declared.strip():
            model.policy[key] = declared
            report["wording"].append(key)

    # 3. The perimeters -- additive only.
    declared_never = list(pack.speech.get("never_say") or [])
    declared_never += [p for p in (pack.policy.get("never_say") or [])
                       if p not in declared_never]
    if pack.speech.get("never_say") or pack.policy.get("never_say"):
        merged, added = _merge_never_say(model.policy.get("never_say"), declared_never)
        model.policy["never_say"] = merged
        if added:
            report["reinforced"].append(f"never_say += {added}")
    if pack.policy.get("boundaries"):
        merged, reinforced, refused = _merge_boundaries(
            model.policy.get("boundaries"), pack.policy.get("boundaries"))
        model.policy["boundaries"] = merged
        for key in reinforced:
            report["reinforced"].append(f"boundaries.{key} = true")
        report["refused"].extend(f"boundaries: {item}" for item in refused)
    for key in ("refuse_harmful", "first_person"):
        if key in pack.policy:
            model.policy[key] = _or_flag(model.policy.get(key), pack.policy[key])
            if pack.policy[key] and not bool(_extract_policy(local).get(key)):
                report["reinforced"].append(f"{key} = true")

    # 4. Permissions are the operator's; the pack's declaration is recorded, not used.
    if pack.proactivity != normalise_proactivity(None):
        report["refused"].append(
            f"proactivity {pack.proactivity} — an artifact cannot change what this "
            f"node is permitted to originate; the operator's config stands")

    if not pack.may_redistribute:
        report["warnings"].append(
            "the pack's licence forbids redistribution: fine to run here, but it "
            "must not be committed or shipped (see MODELS.md)")

    # 5. The history must not claim a blank birth for a trained character.
    stamp = adopted_at if adopted_at is not None else time.time()
    model.history = [{"gen": 0, "note": f"adopted persona {pack.identity}",
                      "deltas": {}, "at": stamp}]
    model.persona = pack.meta(stamp)
    return model, report
