from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

from nrr.schema import SCHEMA_VERSION, Coordinate3D, DHCandidate, SceneEntity, NRRSensorEvent


@dataclass
class NRRRenderStats:
    """Timing / memory stats for one rendered frame (cf. NRR RenderStats)."""
    render_time_ms: float = 0.0
    neural_inference_time_ms: float = 0.0
    backend_overhead_ms: float = 0.0
    memory_used_mb: int = 0
    quality_metric: float = 0.0
    backend: str = "cpu"

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class NRRRenderResult:
    """Structured render result -- output handle + stats, no pixel bytes."""
    frame_id: str
    status: str = "ok"
    output_handle: str = ""
    width: int = 0
    height: int = 0
    stats: NRRRenderStats = field(default_factory=NRRRenderStats)
    error: str = ""
    schema_version: int = SCHEMA_VERSION

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @staticmethod
    def not_supported(frame_id: str, reason: str = "") -> "NRRRenderResult":
        return NRRRenderResult(frame_id=frame_id, status="not_supported",
                               error=str(reason)[:256])

    @staticmethod
    def from_dict(data: Dict[str, Any]) -> "NRRRenderResult":
        stats = data.get("stats") or {}
        return NRRRenderResult(
            frame_id=str(data.get("frame_id", "")),
            status=str(data.get("status", "ok")),
            output_handle=str(data.get("output_handle", ""))[:256],
            width=int(data.get("width", 0)),
            height=int(data.get("height", 0)),
            stats=NRRRenderStats(
                render_time_ms=float(stats.get("render_time_ms", 0.0)),
                neural_inference_time_ms=float(
                    stats.get("neural_inference_time_ms", 0.0)),
                backend_overhead_ms=float(
                    stats.get("backend_overhead_ms", 0.0)),
                memory_used_mb=int(stats.get("memory_used_mb", 0)),
                quality_metric=float(stats.get("quality_metric", 0.0)),
                backend=str(stats.get("backend", "cpu"))[:32],
            ),
            error=str(data.get("error", ""))[:256],
            schema_version=int(data.get("schema_version", SCHEMA_VERSION)),
        )


@dataclass
class NRRSceneResult:
    """Structured perception result from the peripheral worker."""
    frame_id: str
    status: str = "ok"
    source_device_id: str = ""
    sensor_provenance: str = ""
    schema_version: int = SCHEMA_VERSION
    scene_version: int = 1
    entities: List[SceneEntity] = field(default_factory=list)
    motion_events: List[NRRSensorEvent] = field(default_factory=list)
    motion_summary: Dict[str, Any] = field(default_factory=dict)
    model_contribution: Dict[str, Any] = field(default_factory=dict)
    error: str = ""

    def validate(self) -> None:
        if not self.frame_id or len(self.frame_id) > 128:
            raise ValueError("frame_id required")
        if not self.source_device_id or len(self.source_device_id) > 128:
            raise ValueError("source_device_id required")
        if len(self.sensor_provenance) > 128:
            raise ValueError("sensor_provenance too long")
        if not self.entities:
            raise ValueError("at least one scene entity required")
        for e in self.entities:
            if not isinstance(e, SceneEntity):
                raise ValueError("entities must be SceneEntity")
            e.validate()
        for ev in self.motion_events:
            if not isinstance(ev, NRRSensorEvent):
                raise ValueError("motion_events must be NRRSensorEvent")
            ev.validate()
        if not isinstance(self.motion_summary, dict):
            raise ValueError("motion_summary must be a dict")
        if not isinstance(self.model_contribution, dict):
            raise ValueError("model_contribution must be a dict")
        if not (0.0 <= self.motion_summary.get("motion_score", 0.0) <= 1.0):
            raise ValueError("motion_summary.motion_score out of range")
        if len(self.error) > 256:
            raise ValueError("error too long")

    def to_dict(self) -> Dict[str, Any]:
        self.validate()
        return {
            "frame_id": self.frame_id,
            "status": self.status,
            "source_device_id": self.source_device_id,
            "sensor_provenance": self.sensor_provenance,
            "schema_version": self.schema_version,
            "scene_version": self.scene_version,
            "entities": [e.to_dict() for e in self.entities],
            "motion_events": [ev.to_dict() for ev in self.motion_events],
            "motion_summary": dict(self.motion_summary),
            "model_contribution": dict(self.model_contribution),
            "error": self.error,
        }

    @staticmethod
    def from_dict(data: Dict[str, Any]) -> "NRRSceneResult":
        entities = []
        for ed in (data.get("entities") or []):
            if isinstance(ed, dict):
                entities.append(SceneEntity.from_dict(ed))
        events = []
        for ed in (data.get("motion_events") or []):
            if isinstance(ed, dict):
                events.append(NRRSensorEvent.from_dict(ed))
        return NRRSceneResult(
            frame_id=str(data.get("frame_id", "")),
            status=str(data.get("status", "ok")),
            source_device_id=str(data.get("source_device_id", ""))[:128],
            sensor_provenance=str(data.get("sensor_provenance", ""))[:128],
            schema_version=int(data.get("schema_version", SCHEMA_VERSION)),
            scene_version=int(data.get("scene_version", 1)),
            entities=entities,
            motion_events=events,
            motion_summary=dict(data.get("motion_summary") or {}),
            model_contribution=dict(data.get("model_contribution") or {}),
            error=str(data.get("error", ""))[:256],
        )

    @staticmethod
    def not_supported(frame_id: str, reason: str = "") -> "NRRSceneResult":
        return NRRSceneResult(frame_id=frame_id, status="not_supported",
                              error=str(reason)[:256])


def make_scene_result(
    frame_id: str,
    entities: List[SceneEntity],
    motion_events: Optional[List[NRRSensorEvent]] = None,
    motion_score: Optional[float] = None,
    motion_region: Optional[Coordinate3D] = None,
    model_contribution: Optional[Dict[str, Any]] = None,
    source_device_id: str = "",
    sensor_provenance: str = "",
    status: str = "ok",
) -> Dict[str, Any]:
    summary: Dict[str, Any] = {}
    if motion_score is not None:
        summary["motion_score"] = float(motion_score)
    if motion_region is not None and isinstance(motion_region, Coordinate3D):
        summary["motion_region"] = motion_region.to_dict()
    mc: Dict[str, Any] = {}
    if model_contribution is not None:
        mc.update(model_contribution)
    return NRRSceneResult(
        frame_id=frame_id, entities=list(entities),
        motion_events=list(motion_events) if motion_events else [],
        motion_summary=summary, model_contribution=mc,
        source_device_id=source_device_id, sensor_provenance=sensor_provenance,
        status=status,
    ).to_dict()


def make_not_supported_scene_result(frame_id: str, reason: str = "") -> Dict[str, Any]:
    """Refusal payload for a scene request (no entities, no validate trip).

    Built directly rather than through NRRSceneResult.to_dict(): a refusal
    carries no entities, and validate() requires them only for real scenes.
    """
    return {
        "frame_id": frame_id,
        "status": "not_supported",
        "source_device_id": "",
        "sensor_provenance": "",
        "schema_version": SCHEMA_VERSION,
        "scene_version": 1,
        "entities": [],
        "motion_events": [],
        "motion_summary": {},
        "model_contribution": {},
        "error": str(reason)[:256],
    }


def make_not_supported_result(frame_id: str, reason: str = "") -> Dict[str, Any]:
    """Refusal payload for a render request (primary-side convenience)."""
    return NRRRenderResult.not_supported(frame_id, reason).to_dict()


# Primary-side descriptor builder, re-exported so callers have one import
# surface for the contract: nrr.result covers describe + result.
from nrr.descriptor import describe_frame  # noqa: E402  (no cycle: descriptor -> schema only)
