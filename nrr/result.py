from dataclasses import asdict, dataclass, field
from typing import Any, Dict

from nrr.schema import SCHEMA_VERSION


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


def make_not_supported_result(frame_id: str,
                              reason: str = "") -> Dict[str, Any]:
    """Peripheral worker stub response until the NRR runtime matures."""
    return NRRRenderResult.not_supported(frame_id, reason).to_dict()


def describe_frame(frame_id: str, width: int, height: int,
                   pixel_format: str = "RGB8", model_id: str = "",
                   reference_id: str = "", frame_index: int = 0,
                   delta_time: float = 0.0,
                   quality_hint: str = "balanced") -> Dict[str, Any]:
    """Build + validate a descriptor dict in one call (primary side)."""
    from nrr.descriptor import NRRFrameDescriptor
    return NRRFrameDescriptor(
        frame_id=frame_id, width=width, height=height,
        pixel_format=pixel_format, model_id=model_id,
        reference_id=reference_id, frame_index=frame_index,
        delta_time=delta_time, quality_hint=quality_hint).to_dict()