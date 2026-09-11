"""NRR transport adapter: descriptor dispatch over the mobile namespace.

Binary-free: translates nrr.protocol messages into compute_request
publishes on the existing /shugocore/mobile/{device_id}/ topics and hands
inbound compute_result messages to the caller.  Capability-aware routing
keeps pixel work off nodes that never advertised the workload; unpaired
devices are refused on both directions.

Until the NRR runtime matures, the peripheral worker stub answers every
render request with a not_supported result -- auditable and fail-closed.
"""
from typing import Any, Dict, List, Optional

from mobile_nodes import node_supports_workload
from security import sanitize_text

import nrr.protocol as proto
from nrr.descriptor import NRRFrameDescriptor
from nrr.result import NRRRenderResult, make_not_supported_result

NRR_WORKLOAD = "nrr_render"


class NRRTransportAdapter:
    """DDS transport adapter for the NRR render contract."""

    def __init__(self, registry, broker=None, audit=None):
        self.registry = registry
        self.broker = broker
        self.audit = audit

    def capable_nodes(self) -> List[Dict[str, Any]]:
        """Paired nodes advertising the nrr_render workload."""
        try:
            return self.registry.nodes_for_workload(NRR_WORKLOAD)
        except AttributeError:
            out = []
            for node in self.registry.list_nodes():
                if node_supports_workload(node.get("manifest", {}),
                                          NRR_WORKLOAD):
                    out.append(node)
            return out

    def render(self, device_id: str, descriptor: Dict[str, Any],
               timeout: Optional[float] = None) -> Dict[str, Any]:
        """Dispatch a validated descriptor; returns the result envelope.

        Fails closed: unpaired devices, unadvertised workload, and invalid
        descriptors are refused before anything is published.
        """
        try:
            desc = NRRFrameDescriptor.from_dict(descriptor)
        except Exception as exc:
            self._audit("nrr_render_refused_invalid_descriptor",
                        {"device_id": sanitize_text(device_id, 48),
                         "reason": str(exc)[:200]})
            return {"status": "refused",
                    "reason": "invalid frame descriptor: %s" % exc}
        if not self.registry.is_paired(device_id):
            self._audit("nrr_render_refused_unpaired",
                        {"device_id": sanitize_text(device_id, 48)})
            return {"status": "refused", "reason": "device not paired"}
        if not node_supports_workload(self.registry.manifest(device_id),
                                      NRR_WORKLOAD):
            self._audit("nrr_render_refused_no_capability",
                        {"device_id": sanitize_text(device_id, 48)})
            return {"status": "refused",
                    "reason": "device does not advertise 'nrr_render'"}
        if self.broker is None:
            return {"status": "refused", "reason": "no compute broker wired"}
        result = self.broker.request_compute(
            device_id, NRR_WORKLOAD,
            {"descriptor": desc.to_dict()}, timeout=timeout)
        if result.get("status") == "success":
            self._audit("nrr_render_dispatched",
                        {"device_id": sanitize_text(device_id, 48),
                         "frame_id": desc.frame_id})
        return result

    def handle_result(self, device_id: str,
                      payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Validate an inbound NRRResult envelope; None when refused."""
        if not self.registry.is_paired(device_id):
            self._audit("nrr_result_refused_unpaired",
                        {"device_id": sanitize_text(device_id, 48)})
            return None
        if proto.msg_type(payload) != "NRRResult":
            self._audit("nrr_result_refused_bad_type",
                        {"device_id": sanitize_text(device_id, 48),
                         "type": proto.msg_type(payload)[:64]})
            return None
        try:
            result = NRRRenderResult.from_dict(payload.get("result") or {})
        except Exception as exc:
            self._audit("nrr_result_refused_invalid",
                        {"device_id": sanitize_text(device_id, 48),
                         "reason": str(exc)[:200]})
            return None
        self._audit("nrr_result_received",
                    {"device_id": sanitize_text(device_id, 48),
                     "frame_id": result.frame_id[:64],
                     "status": result.status[:32]})
        return result.to_dict()

    @staticmethod
    def worker_stub(request: Dict[str, Any]) -> Dict[str, Any]:
        """Answer a render request with not_supported (peripheral no-op).

        Validates the descriptor, then returns the stub result envelope.
        Swap for the real nrr_render() call when a backend exists.
        """
        request_id = str(request.get("request_id", ""))[:64]
        descriptor = request.get("descriptor") or {}
        try:
            desc = NRRFrameDescriptor.from_dict(descriptor)
        except Exception as exc:
            return proto.make_render_result(
                request_id,
                make_not_supported_result("", "invalid: %s" % exc))
        return proto.make_render_result(
            request_id, make_not_supported_result(desc.frame_id,
                                                  "nrr backend unavailable"))

    def _audit(self, event_type: str, payload: Dict[str, Any]) -> None:
        if self.audit is None:
            return
        try:
            self.audit.append(event_type, payload)
        except Exception:
            pass