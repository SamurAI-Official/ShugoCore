from dataclasses import asdict, dataclass
from typing import Any, Dict

from nrr.schema import SCHEMA_VERSION, KNOWN_PIXEL_FORMATS, KNOWN_QUALITY_HINTS


@dataclass
class NRRFrameDescriptor:
    """Lightweight render request -- no pixel bytes, descriptor only."""
    frame_id: str
    width: int
    height: int
    pixel_format: str = "RGB8"
    model_id: str = ""
    reference_id: str = ""
    frame_index: int = 0
    delta_time: float = 0.0
    quality_hint: str = "balanced"
    schema_version: int = SCHEMA_VERSION

    def validate(self) -> None:
        if not self.frame_id or len(self.frame_id) > 64:
            raise ValueError("frame_id required (<=64 chars)")
        if not (1 <= self.width <= 8192 and 1 <= self.height <= 8192):
            raise ValueError("resolution out of range (1..8192 each axis)")
        if self.pixel_format not in KNOWN_PIXEL_FORMATS:
            raise ValueError("unknown pixel_format '%s'" % self.pixel_format)
        if len(self.model_id) > 128:
            raise ValueError("model_id too long (<=128 chars)")
        if len(self.reference_id) > 128:
            raise ValueError("reference_id too long (<=128 chars)")
        if self.frame_index < 0:
            raise ValueError("frame_index must be >= 0")
        if not (0.0 <= self.delta_time < 3600.0):
            raise ValueError("delta_time out of range")
        if self.quality_hint not in KNOWN_QUALITY_HINTS:
            raise ValueError("unknown quality_hint '%s'" % self.quality_hint)

    def to_dict(self) -> Dict[str, Any]:
        self.validate()
        return asdict(self)

    @staticmethod
    def from_dict(data: Dict[str, Any]) -> "NRRFrameDescriptor":
        desc = NRRFrameDescriptor(
            frame_id=str(data.get("frame_id", "")),
            width=int(data.get("width", 0)),
            height=int(data.get("height", 0)),
            pixel_format=str(data.get("pixel_format", "RGB8")),
            model_id=str(data.get("model_id", ""))[:128],
            reference_id=str(data.get("reference_id", ""))[:128],
            frame_index=int(data.get("frame_index", 0)),
            delta_time=float(data.get("delta_time", 0.0)),
            quality_hint=str(data.get("quality_hint", "balanced")),
            schema_version=int(data.get("schema_version", SCHEMA_VERSION)),
        )
        desc.validate()
        return desc