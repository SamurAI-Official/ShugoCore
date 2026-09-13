from dataclasses import asdict, dataclass
from typing import Any, Dict, Optional, List

from nrr.schema import SCHEMA_VERSION, Coordinate3D, KNOWN_PIXEL_FORMATS, KNOWN_QUALITY_HINTS


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
            raise ValueError("unknown pixel_format " + repr(self.pixel_format))
        if len(self.model_id) > 128:
            raise ValueError("model_id too long (<=128 chars)")
        if len(self.reference_id) > 128:
            raise ValueError("reference_id too long (<=128 chars)")
        if self.frame_index < 0:
            raise ValueError("frame_index must be >= 0")
        if not (0.0 <= self.delta_time < 3600.0):
            raise ValueError("delta_time out of range")
        if self.quality_hint not in KNOWN_QUALITY_HINTS:
            raise ValueError("unknown quality_hint " + repr(self.quality_hint))

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


@dataclass
class NRRMotionRequest:
    """Motion / change-detection request descriptor (peripheral worker)."""

    frame_id: str
    previous_frame_id: str = ""
    motion_threshold: float = 0.15
    min_change_area_pixels: int = 8
    change_detection: bool = True
    motion_detection: bool = True
    max_detections: int = 16
    region_of_interest: Optional[Coordinate3D] = None
    call_nrr_model: bool = True
    schema_version: int = SCHEMA_VERSION

    def validate(self) -> None:
        if not self.frame_id or len(self.frame_id) > 64:
            raise ValueError("frame_id required (<=64 chars)")
        if len(self.previous_frame_id) > 64:
            raise ValueError("previous_frame_id too long (<=64 chars)")
        if not (0.0 <= self.motion_threshold <= 1.0):
            raise ValueError("motion_threshold out of range 0..1")
        if self.min_change_area_pixels < 1:
            raise ValueError("min_change_area_pixels must be >= 1")
        if self.max_detections < 1 or self.max_detections > 256:
            raise ValueError("max_detections out of range 1..256")
        if self.region_of_interest is not None:
            if not isinstance(self.region_of_interest, Coordinate3D):
                raise ValueError("region_of_interest must be Coordinate3D")
            self.region_of_interest.validate()

    def to_dict(self) -> Dict[str, Any]:
        self.validate()
        return {
            "frame_id": self.frame_id,
            "previous_frame_id": self.previous_frame_id,
            "motion_threshold": self.motion_threshold,
            "min_change_area_pixels": self.min_change_area_pixels,
            "change_detection": self.change_detection,
            "motion_detection": self.motion_detection,
            "max_detections": self.max_detections,
            "region_of_interest": self.region_of_interest.to_dict() if self.region_of_interest else None,
            "call_nrr_model": self.call_nrr_model,
            "schema_version": self.schema_version,
        }

    @staticmethod
    def from_dict(data: Dict[str, Any]) -> "NRRMotionRequest":
        roi = data.get("region_of_interest")
        return NRRMotionRequest(
            frame_id=str(data.get("frame_id", "")),
            previous_frame_id=str(data.get("previous_frame_id", ""))[:64],
            motion_threshold=float(data.get("motion_threshold", 0.15)),
            min_change_area_pixels=int(data.get("min_change_area_pixels", 8)),
            change_detection=bool(data.get("change_detection", True)),
            motion_detection=bool(data.get("motion_detection", True)),
            max_detections=int(data.get("max_detections", 16)),
            region_of_interest=Coordinate3D.from_dict(roi) if isinstance(roi, dict) else None,
            call_nrr_model=bool(data.get("call_nrr_model", True)),
            schema_version=int(data.get("schema_version", SCHEMA_VERSION)),
        )


def describe_frame(
    frame_id: str, width: int, height: int,
    pixel_format: str = "RGB8", model_id: str = "",
    reference_id: str = "", frame_index: int = 0,
    delta_time: float = 0.0, quality_hint: str = "balanced") -> Dict[str, Any]:
    """Build + validate a descriptor dict in one call (primary side)."""
    return NRRFrameDescriptor(
        frame_id=frame_id, width=width, height=height,
        pixel_format=pixel_format, model_id=model_id,
        reference_id=reference_id, frame_index=frame_index,
        delta_time=delta_time, quality_hint=quality_hint).to_dict()