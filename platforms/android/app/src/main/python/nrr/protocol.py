"""NRR contract topics + message makers (same style as kv_mesh/protocol.py).

Descriptors and results ride the existing /shugocore/mobile/{device_id}/
namespace as structured payloads inside the consent-gated compute_request /
compute_result envelope -- no new raw transport, no pixel bytes on the mesh."""
import time
from typing import Any, Dict

# Contract tails for capability advertisement + render dispatch.
TOPIC_CAPABILITIES = "nrr/capabilities"
TOPIC_RENDER = "nrr/render"
TOPIC_RESULT = "nrr/result"

# Scene / motion perception contract tails.
TOPIC_SCENE_REQUEST = "nrr/scene_request"
TOPIC_SCENE_RESULT = "nrr/scene_result"
TOPIC_MOTION_EVENT = "nrr/motion_event"


def nrr_topic(device_id: str, tail: str) -> str:
    """Canonical NRR contract topic for a device."""
    return "/shugocore/mobile/%s/%s" % (device_id, tail)


def make_capabilities(device_id: str, fp16: bool = False,
                      int8: bool = False, vram_mb: int = 0,
                      workloads=None) -> Dict[str, Any]:
    """Capability advertisement payload (NRR capability-matrix pattern)."""
    return {"type": "NRRCapabilities", "device_id": device_id,
            "fp16": bool(fp16), "int8": bool(int8),
            "vram_mb": int(vram_mb),
            "workloads": list(workloads or []), "ts": time.time()}


def make_render_request(request_id: str,
                        descriptor: Dict[str, Any]) -> Dict[str, Any]:
    """Wrap a validated descriptor in a compute_request-style envelope."""
    return {"type": "NRRRender", "request_id": request_id,
            "descriptor": dict(descriptor), "ts": time.time()}


def make_render_result(request_id: str,
                       result: Dict[str, Any]) -> Dict[str, Any]:
    """Wrap a render result in a compute_result-style envelope."""
    return {"type": "NRRResult", "request_id": request_id,
            "result": dict(result), "ts": time.time()}


def make_scene_result(request_id: str,
                       result: Dict[str, Any]) -> Dict[str, Any]:
    """Wrap a structured scene result in a scene_result envelope."""
    return {"type": "NRRSceneResult", "request_id": request_id,
            "result": dict(result), "ts": time.time()}


def make_motion_event(request_id: str, event: Dict[str, Any]) -> Dict[str, Any]:
    """Wrap a single motion/change event in a motion_event envelope."""
    return {"type": "NRRSensorEvent", "request_id": request_id,
            "event": dict(event), "ts": time.time()}


def msg_type(msg: Dict[str, Any]) -> str:
    return str(msg.get("type", ""))


def is_scene_message(msg: Dict[str, Any]) -> bool:
    t = msg_type(msg)
    return t in ("NRRSceneResult", "NRRSensorEvent")