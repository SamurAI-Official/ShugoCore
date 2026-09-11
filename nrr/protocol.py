"""NRR contract topics + message makers (same style as kv_mesh/protocol.py).

Descriptors and results ride the existing /shugocore/mobile/{device_id}/
namespace as structured payloads inside the consent-gated compute_request /
compute_result envelope -- no new raw transport, no pixel bytes on the mesh.
"""
import time
from typing import Any, Dict

# Contract tails for capability advertisement + render dispatch.
TOPIC_CAPABILITIES = "nrr/capabilities"
TOPIC_RENDER = "nrr/render"
TOPIC_RESULT = "nrr/result"


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


def msg_type(msg: Dict[str, Any]) -> str:
    return str(msg.get("type", ""))