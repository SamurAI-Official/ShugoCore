"""The ask: which window, at which rate, and what may be done with the answer.

One descriptor per window, exactly like NRR's frame descriptor -- and the same
non-negotiable: no samples in it. ``policy`` is where the audio-specific privacy
decision is made explicit rather than implied:

  ``local``  the analysis happens on the device that captured it, and only the
             resulting summary/labels ever travel. The default.
  ``mesh``   the *descriptor* may travel so a peer can analyse audio it receives
             itself (a phone that hears but cannot run a model, a host that can).

Note what ``mesh`` is *not*: permission to send audio. Nothing in this contract can
carry samples, and ``validate`` refuses a descriptor that asks for it.
"""
from dataclasses import asdict, dataclass
from typing import Any, Dict

from sound.schema import (MAX_LABELS, MIN_LABEL_CONFIDENCE, SAMPLE_RATE,
                          SCHEMA_VERSION, WINDOW_SAMPLES)

KNOWN_POLICIES = ("local", "mesh")


@dataclass
class SoundWindowDescriptor:
    """A request to analyse one window of audio that the receiver already holds."""

    window_id: str
    sample_rate: int = SAMPLE_RATE
    samples: int = WINDOW_SAMPLES
    want_labels: bool = True
    want_speech: bool = True
    want_level: bool = True
    max_labels: int = MAX_LABELS
    min_confidence: float = MIN_LABEL_CONFIDENCE
    policy: str = "local"
    schema_version: int = SCHEMA_VERSION

    def validate(self) -> None:
        if not self.window_id or len(self.window_id) > 64:
            raise ValueError("window_id required (<=64 chars)")
        if int(self.sample_rate) != SAMPLE_RATE:
            # Only 16 kHz is supported: both models were verified at that rate, and
            # silently resampling on the device is how you get a "broken" classifier.
            raise ValueError("unsupported sample_rate %r (only %d)"
                             % (self.sample_rate, SAMPLE_RATE))
        if int(self.samples) != WINDOW_SAMPLES:
            raise ValueError("unsupported window length %r (only %d samples)"
                             % (self.samples, WINDOW_SAMPLES))
        if not 1 <= int(self.max_labels) <= MAX_LABELS:
            raise ValueError("max_labels out of range 1..%d" % MAX_LABELS)
        if not 0.0 <= float(self.min_confidence) <= 1.0:
            raise ValueError("min_confidence out of range 0..1")
        if self.policy not in KNOWN_POLICIES:
            raise ValueError("unknown policy " + repr(self.policy))

    def to_dict(self) -> Dict[str, Any]:
        self.validate()
        return asdict(self)

    @staticmethod
    def from_dict(data: Dict[str, Any]) -> "SoundWindowDescriptor":
        desc = SoundWindowDescriptor(
            window_id=str(data.get("window_id", "")),
            sample_rate=int(data.get("sample_rate", SAMPLE_RATE)),
            samples=int(data.get("samples", WINDOW_SAMPLES)),
            want_labels=bool(data.get("want_labels", True)),
            want_speech=bool(data.get("want_speech", True)),
            want_level=bool(data.get("want_level", True)),
            max_labels=int(data.get("max_labels", MAX_LABELS)),
            min_confidence=float(data.get("min_confidence", MIN_LABEL_CONFIDENCE)),
            policy=str(data.get("policy", "local")),
            schema_version=int(data.get("schema_version", SCHEMA_VERSION)),
        )
        desc.validate()
        return desc


def describe_window(window_id: str, **kwargs) -> Dict[str, Any]:
    """Build + validate a descriptor dict in one call."""
    return SoundWindowDescriptor(window_id=window_id, **kwargs).to_dict()
