"""What came back: a summary, and (maybe) labels. Never samples.

Mirrors NRR's result envelope, including the fail-closed shape: a worker with no
backend answers ``not_supported`` with a reason, so a consumer can tell "the device
cannot do this" from "the device did it and heard nothing".
"""
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from sound.schema import MAX_LABELS, SCHEMA_VERSION

KNOWN_STATUSES = ("ok", "not_supported", "refused")


@dataclass
class SoundEventResult:
    """One analysed window, as it travels: numbers and short strings only."""

    window_id: str
    status: str = "ok"
    summary: Dict[str, Any] = field(default_factory=dict)
    labels: List[Dict[str, Any]] = field(default_factory=list)
    speech_prob: Optional[float] = None
    reason: str = ""
    schema_version: int = SCHEMA_VERSION

    def validate(self) -> None:
        if not self.window_id or len(self.window_id) > 64:
            raise ValueError("window_id required (<=64 chars)")
        if self.status not in KNOWN_STATUSES:
            raise ValueError("unknown status " + repr(self.status))
        if len(self.labels) > MAX_LABELS:
            raise ValueError("too many labels (>%d)" % MAX_LABELS)
        for label in self.labels:
            confidence = float(label.get("confidence", -1.0))
            if not 0.0 <= confidence <= 1.0:
                raise ValueError("label confidence out of range")
            if not str(label.get("name") or ""):
                raise ValueError("label without a name")
        if self.speech_prob is not None and not 0.0 <= float(self.speech_prob) <= 1.0:
            raise ValueError("speech_prob out of range")
        if self.status != "ok" and not self.reason:
            raise ValueError("a non-ok result must say why")

    def to_dict(self) -> Dict[str, Any]:
        self.validate()
        out: Dict[str, Any] = {
            "window_id": self.window_id,
            "status": self.status,
            "schema_version": self.schema_version,
        }
        if self.status == "ok":
            # Only a real analysis carries a summary. "We could not listen" must never be
            # shaped like "we listened and the room was empty" -- that difference is the
            # whole point of this contract, so it is structural rather than conventional.
            out["summary"] = dict(self.summary)
            out["labels"] = [dict(label) for label in self.labels]
            if self.speech_prob is not None:
                out["speech_prob"] = round(float(self.speech_prob), 3)
        if self.reason:
            out["reason"] = self.reason
        return out

    @staticmethod
    def from_dict(data: Dict[str, Any]) -> "SoundEventResult":
        result = SoundEventResult(
            window_id=str(data.get("window_id", "")),
            status=str(data.get("status", "ok")),
            summary=dict(data.get("summary") or {}),
            labels=[dict(label) for label in (data.get("labels") or [])
                    if isinstance(label, dict)],
            speech_prob=(float(data["speech_prob"])
                         if data.get("speech_prob") is not None else None),
            reason=str(data.get("reason", ""))[:160],
            schema_version=int(data.get("schema_version", SCHEMA_VERSION)),
        )
        result.validate()
        return result


def not_supported(window_id: str, reason: str) -> Dict[str, Any]:
    """The fail-closed answer: honest absence, with the reason, never a guess."""
    return SoundEventResult(window_id=window_id, status="not_supported",
                            reason=reason).to_dict()


def ok_result(window_id: str, summary: Dict[str, Any],
              labels: Optional[List[Dict[str, Any]]] = None,
              speech_prob: Optional[float] = None) -> Dict[str, Any]:
    return SoundEventResult(window_id=window_id, summary=dict(summary or {}),
                            labels=list(labels or []),
                            speech_prob=speech_prob).to_dict()
