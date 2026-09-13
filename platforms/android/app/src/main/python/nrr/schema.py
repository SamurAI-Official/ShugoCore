"""NRR perception contract schema (versioned).

Covers the 3-axis coordinate model, frame-of-reference plus origin plus sensor
provenance tagging, DH articulated-joint parameters, and the vocabularies that
keep coordinates interoperable across multi-camera / multi-device / multi-sensor
inputs without silently conflating heterogeneous evidence.

Shugocore NRR here is the perception plus structured scene contract layered on
top of the NRR rendering envelope (frame descriptors / capability matrix /
render results).  It is NOT the upstream neural-rendering runtime
(SamurAI-Official/NRR) -- the rendering envelope is reused as the transport
shape, but the semantic content Shugocore carries is perception, motion, and
structured scene context for the reasoner.

All types carry schema_version.  Validation is fail-closed: unknown
vocabularies, out-of-range values, and missing required fields raise
ValueError before anything is published or handed to the reasoner.
"""

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, Optional

SCHEMA_VERSION = 2

# --- rendering envelope constants (kept for nrr.descriptor / nrr.result) ---
KNOWN_PIXEL_FORMATS = ("RGB8", "RGBA8", "YUV420", "GRAY8")
KNOWN_QUALITY_HINTS = ("draft", "balanced", "quality")

# --- coordinate vocabularies ---
KNOWN_UNITS = ("m", "cm", "mm", "pixel", "normalized")

KNOWN_FRAMES = (
    "camera_local",        # origin at a specific camera optical center
    "world_aligned",       # a system-level merged world frame
    "sensor_fused",        # output of the multi-input merger
    "dh_link",             # DH link frame
    "dh_joint",            # DH joint frame
    "device_chassis",      # local device/body frame
)

KNOWN_AXES_CONVENTIONS = (
    "cartesian_xyz_right_handed_y_up",   # X right, Y up, Z forward (meters)
    "cartesian_xyz_right_handed_z_up",   # X right, Y forward, Z up
    "pixel_xy_image",                     # image row/col (y-down)
    "dh_standard",                        # standard DH a/alpha/d/theta
    "spherical_radius_azimuth_elevation",# r, azimuth, elevation
)

KNOWN_ORIGINS = (
    "camera_optical_center",
    "world_origin",
    "device_chassis_origin",
    "link_base",
    "link_i",
    "joint_i",
    "fused_origin",
)

KNOWN_SENSOR_TYPES = (
    "camera_rgb",
    "camera_depth",
    "camera_infrared",
    "lidar",
    "radar",
    "ultrasonic",
    "imu",
    "pose_estimator",
    "fused_motion",
    "nrr_model",
)


@dataclass
class Coordinate3D:
    """A single 3-axis measurement with explicit frame + provenance."""

    x: float = 0.0
    y: float = 0.0
    z: float = 0.0
    unit: str = "m"
    frame: str = "camera_local"
    axes_convention: str = "cartesian_xyz_right_handed_y_up"
    origin: str = "camera_optical_center"
    sensor_provenance: str = ""
    sensor_device_id: str = ""
    confidence: float = 0.0
    age_ms: int = 0
    source_frame_id: str = ""
    schema_version: int = SCHEMA_VERSION

    def validate(self) -> None:
        if not isinstance(self.x, (int, float)) or self.x != self.x:
            raise ValueError("x must be a finite number")
        if not isinstance(self.y, (int, float)) or self.y != self.y:
            raise ValueError("y must be a finite number")
        if not isinstance(self.z, (int, float)) or self.z != self.z:
            raise ValueError("z must be a finite number")
        if self.unit not in KNOWN_UNITS:
            raise ValueError("unknown unit " + repr(self.unit))
        if self.frame not in KNOWN_FRAMES:
            raise ValueError("unknown frame " + repr(self.frame))
        if self.axes_convention not in KNOWN_AXES_CONVENTIONS:
            raise ValueError("unknown axes_convention " + repr(self.axes_convention))
        if self.origin not in KNOWN_ORIGINS:
            raise ValueError("unknown origin " + repr(self.origin))
        if not (0.0 <= self.confidence <= 1.0):
            raise ValueError("confidence out of range 0..1")
        if self.age_ms < 0:
            raise ValueError("age_ms must be >= 0")
        if len(self.sensor_provenance) > 128:
            raise ValueError("sensor_provenance too long")
        if len(self.sensor_device_id) > 128:
            raise ValueError("sensor_device_id too long")
        if len(self.source_frame_id) > 128:
            raise ValueError("source_frame_id too long")

    def to_dict(self) -> Dict[str, Any]:
        self.validate()
        return asdict(self)

    @staticmethod
    def from_dict(data: Dict[str, Any]) -> "Coordinate3D":
        return Coordinate3D(
            x=float(data.get("x", 0.0)),
            y=float(data.get("y", 0.0)),
            z=float(data.get("z", 0.0)),
            unit=str(data.get("unit", "m")),
            frame=str(data.get("frame", "camera_local")),
            axes_convention=str(data.get("axes_convention",
                "cartesian_xyz_right_handed_y_up")),
            origin=str(data.get("origin", "camera_optical_center")),
            sensor_provenance=str(data.get("sensor_provenance", ""))[:128],
            sensor_device_id=str(data.get("sensor_device_id", ""))[:128],
            confidence=float(data.get("confidence", 0.0)),
            age_ms=int(data.get("age_ms", 0)),
            source_frame_id=str(data.get("source_frame_id", ""))[:128],
            schema_version=int(data.get("schema_version", SCHEMA_VERSION)),
        )


@dataclass
class DHCandidate:
    """A single DH joint/link candidate (standard DH parameters)."""

    link_id: str = ""
    a: float = 0.0
    alpha: float = 0.0
    d: float = 0.0
    theta: float = 0.0
    joint_type: str = "revolute"
    confidence: float = 0.0
    age_ms: int = 0
    source_frame_id: str = ""
    schema_version: int = SCHEMA_VERSION

    def validate(self) -> None:
        if not self.link_id or len(self.link_id) > 64:
            raise ValueError("link_id required")
        if self.joint_type not in ("revolute", "prismatic", "fixed"):
            raise ValueError("unknown joint_type " + repr(self.joint_type))
        if not (0.0 <= self.confidence <= 1.0):
            raise ValueError("confidence out of range 0..1")
        if self.age_ms < 0:
            raise ValueError("age_ms must be >= 0")

    def to_dict(self) -> Dict[str, Any]:
        self.validate()
        return asdict(self)

    @staticmethod
    def from_dict(data: Dict[str, Any]) -> "DHCandidate":
        return DHCandidate(
            link_id=str(data.get("link_id", ""))[:64],
            a=float(data.get("a", 0.0)),
            alpha=float(data.get("alpha", 0.0)),
            d=float(data.get("d", 0.0)),
            theta=float(data.get("theta", 0.0)),
            joint_type=str(data.get("joint_type", "revolute")),
            confidence=float(data.get("confidence", 0.0)),
            age_ms=int(data.get("age_ms", 0)),
            source_frame_id=str(data.get("source_frame_id", ""))[:128],
            schema_version=int(data.get("schema_version", SCHEMA_VERSION)),
        )


@dataclass
class SceneEntity:
    """One detected / reasoned-about thing in the scene."""

    id: str = ""
    label: str = ""
    position: Coordinate3D = field(default_factory=Coordinate3D)
    extents: Optional[Coordinate3D] = None
    dh: Optional[DHCandidate] = None
    confidence: float = 0.0
    age_ms: int = 0
    source_frame_id: str = ""
    schema_version: int = SCHEMA_VERSION

    def validate(self) -> None:
        if not self.id or len(self.id) > 128:
            raise ValueError("entity id required")
        if len(self.label) > 128:
            raise ValueError("label too long")
        self.position.validate()
        if self.extents is not None:
            self.extents.validate()
        if self.dh is not None:
            self.dh.validate()
        if not (0.0 <= self.confidence <= 1.0):
            raise ValueError("confidence out of range 0..1")
        if self.age_ms < 0:
            raise ValueError("age_ms must be >= 0")

    def to_dict(self) -> Dict[str, Any]:
        self.validate()
        return {
            "id": self.id,
            "label": self.label,
            "position": self.position.to_dict(),
            "extents": self.extents.to_dict() if self.extents else None,
            "dh": self.dh.to_dict() if self.dh else None,
            "confidence": self.confidence,
            "age_ms": self.age_ms,
            "source_frame_id": self.source_frame_id,
            "schema_version": self.schema_version,
        }

    @staticmethod
    def from_dict(data: Dict[str, Any]) -> "SceneEntity":
        pos = data.get("position") or {}
        ext = data.get("extents")
        dh = data.get("dh")
        return SceneEntity(
            id=str(data.get("id", ""))[:128],
            label=str(data.get("label", ""))[:128],
            position=Coordinate3D.from_dict(pos),
            extents=Coordinate3D.from_dict(ext) if isinstance(ext, dict) else None,
            dh=DHCandidate.from_dict(dh) if isinstance(dh, dict) else None,
            confidence=float(data.get("confidence", 0.0)),
            age_ms=int(data.get("age_ms", 0)),
            source_frame_id=str(data.get("source_frame_id", ""))[:128],
            schema_version=int(data.get("schema_version", SCHEMA_VERSION)),
        )


@dataclass
class NRRSensorEvent:
    """One motion / change-detection event from the peripheral worker."""

    event_id: str = ""
    event_type: str = "motion"
    region: Optional[Coordinate3D] = None
    extents: Optional[Coordinate3D] = None
    motion_score: float = 0.0
    source_frame_id: str = ""
    previous_frame_id: str = ""
    timestamp_ms: int = 0
    confidence: float = 0.0
    age_ms: int = 0
    schema_version: int = SCHEMA_VERSION

    def validate(self) -> None:
        if not self.event_id or len(self.event_id) > 128:
            raise ValueError("event_id required")
        if self.event_type not in ("motion", "appearance", "disappearance", "change"):
            raise ValueError("unknown event_type " + repr(self.event_type))
        if self.region is not None:
            self.region.validate()
        if self.extents is not None:
            self.extents.validate()
        if not (0.0 <= self.motion_score <= 1.0):
            raise ValueError("motion_score out of range 0..1")
        if not (0.0 <= self.confidence <= 1.0):
            raise ValueError("confidence out of range 0..1")
        if self.age_ms < 0:
            raise ValueError("age_ms must be >= 0")

    def to_dict(self) -> Dict[str, Any]:
        self.validate()
        return {
            "event_id": self.event_id,
            "event_type": self.event_type,
            "region": self.region.to_dict() if self.region else None,
            "extents": self.extents.to_dict() if self.extents else None,
            "motion_score": self.motion_score,
            "source_frame_id": self.source_frame_id,
            "previous_frame_id": self.previous_frame_id,
            "timestamp_ms": self.timestamp_ms,
            "confidence": self.confidence,
            "age_ms": self.age_ms,
            "schema_version": self.schema_version,
        }

    @staticmethod
    def from_dict(data: Dict[str, Any]) -> "NRRSensorEvent":
        region = data.get("region")
        extents = data.get("extents")
        return NRRSensorEvent(
            event_id=str(data.get("event_id", ""))[:128],
            event_type=str(data.get("event_type", "motion")),
            region=Coordinate3D.from_dict(region) if isinstance(region, dict) else None,
            extents=Coordinate3D.from_dict(extents) if isinstance(extents, dict) else None,
            motion_score=float(data.get("motion_score", 0.0)),
            source_frame_id=str(data.get("source_frame_id", ""))[:128],
            previous_frame_id=str(data.get("previous_frame_id", ""))[:128],
            timestamp_ms=int(data.get("timestamp_ms", 0)),
            confidence=float(data.get("confidence", 0.0)),
            age_ms=int(data.get("age_ms", 0)),
            schema_version=int(data.get("schema_version", SCHEMA_VERSION)),
        )