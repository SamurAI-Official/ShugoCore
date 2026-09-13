"""NRR transport adapter: descriptor + scene/motion dispatch over the mobile namespace.

Binary-free: translates nrr.protocol messages into compute_request
publishes on the existing /shugocore/mobile/{device_id}/ topics and hands
inbound compute_result / scene_result messages to the caller.  Capability-aware
routing keeps pixel work off nodes that never advertised the workload; unpaired
devices are refused on both directions.  No pixel bytes ever cross the mesh.

Until the NRR runtime matures, the peripheral worker stub answers every
render/scene request with a not_supported result -- auditable and fail-closed.
"""
from typing import Any, Dict, List, Optional

import time

from mobile_nodes import node_supports_workload
from nrr.descriptor import NRRFrameDescriptor, NRRMotionRequest
from security import sanitize_text

import nrr.protocol as proto
from nrr.result import (
    NRRRenderResult,
    NRRRenderStats,
    NRRSceneResult,
    make_not_supported_result,
    make_not_supported_scene_result,
)

NRR_WORKLOAD = "nrr_render"


class NRRTransportAdapter:
    """DDS transport adapter for the NRR render + scene/motion contract."""

    def __init__(self, registry, broker=None, audit=None):
        self.registry = registry
        self.broker = broker
        self.audit = audit

    # ---- capability routing ---------------------------------------------

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

    def _capable(self, device_id: str, event: str) -> Optional[Dict[str, Any]]:
        """Shared refusal gate: pairing + workload advertisement.

        Returns a refusal envelope when the device may not receive the
        workload, None when it may.  Every refusal is audited.
        """
        if not self.registry.is_paired(device_id):
            self._audit(event + "_refused_unpaired",
                        {"device_id": sanitize_text(device_id, 48)})
            return {"status": "refused", "reason": "device not paired"}
        if not node_supports_workload(self.registry.manifest(device_id),
                                      NRR_WORKLOAD):
            self._audit(event + "_refused_no_capability",
                        {"device_id": sanitize_text(device_id, 48)})
            return {"status": "refused",
                    "reason": "device does not advertise '%s'" % NRR_WORKLOAD}
        return None

    # ---- render dispatch (primary side) ----------------------------------

    def render(self, device_id: str, descriptor: Dict[str, Any],
               timeout: Optional[float] = None) -> Dict[str, Any]:
        """Dispatch a validated frame descriptor; returns the result envelope.

        Fails closed: unpaired devices, unadvertised workload, and invalid
        descriptors are refused before anything is published.
        """
        try:
            desc = NRRFrameDescriptor.from_dict(descriptor)
            desc.validate()
        except Exception as exc:
            self._audit("nrr_render_refused_invalid_descriptor",
                        {"device_id": sanitize_text(device_id, 48),
                         "reason": str(exc)[:200]})
            return {"status": "refused",
                    "reason": "invalid frame descriptor: %s" % exc}
        refused = self._capable(device_id, "nrr_render")
        if refused is not None:
            return refused
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

    # ---- scene / motion dispatch (primary side) ---------------------------

    def request_scene(self, device_id: str, motion_request: Dict[str, Any],
                      timeout: Optional[float] = None) -> Dict[str, Any]:
        """Dispatch a validated motion/scene request; returns the envelope.

        Same fail-closed gate as render(): descriptor validity, pairing, and
        workload advertisement are checked before anything is published.
        """
        try:
            req = NRRMotionRequest.from_dict(motion_request)
            req.validate()
        except Exception as exc:
            self._audit("nrr_scene_refused_invalid_request",
                        {"device_id": sanitize_text(device_id, 48),
                         "reason": str(exc)[:200]})
            return {"status": "refused",
                    "reason": "invalid motion request: %s" % exc}
        refused = self._capable(device_id, "nrr_scene")
        if refused is not None:
            return refused
        if self.broker is None:
            return {"status": "refused", "reason": "no compute broker wired"}
        result = self.broker.request_compute(
            device_id, NRR_WORKLOAD,
            {"motion_request": req.to_dict()}, timeout=timeout)
        if result.get("status") == "success":
            self._audit("nrr_scene_dispatched",
                        {"device_id": sanitize_text(device_id, 48),
                         "frame_id": req.frame_id})
        return result

    # ---- inbound results ---------------------------------------------------

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

    def handle_scene_result(self, device_id: str,
                            payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Validate an inbound NRRSceneResult / NRRSensorEvent envelope.

        Structured scene data flows through the same consent gate as render
        results: paired devices only, known envelope type only, and every
        entity/event must satisfy schema validation before it can reach the
        caller.  Returns the scene dict, or None when refused.
        """
        if not self.registry.is_paired(device_id):
            self._audit("nrr_scene_result_refused_unpaired",
                        {"device_id": sanitize_text(device_id, 48)})
            return None
        t = proto.msg_type(payload)
        if t not in ("NRRSceneResult", "NRRSensorEvent"):
            self._audit("nrr_scene_result_refused_bad_type",
                        {"device_id": sanitize_text(device_id, 48),
                         "type": t[:64]})
            return None
        body = payload.get("result") if t == "NRRSceneResult" \
            else payload.get("event")
        try:
            if t == "NRRSceneResult":
                scene = NRRSceneResult.from_dict(body or {})
                scene.validate()
                out: Dict[str, Any] = scene.to_dict()
                event_id = scene.frame_id
            else:
                from nrr.schema import NRRSensorEvent as _Ev
                ev = _Ev.from_dict(body or {})
                ev.validate()
                out = ev.to_dict()
                event_id = out.get("frame_id", "")
        except Exception as exc:
            self._audit("nrr_scene_result_refused_invalid",
                        {"device_id": sanitize_text(device_id, 48),
                         "reason": str(exc)[:200]})
            return None
        self._audit("nrr_scene_result_received",
                    {"device_id": sanitize_text(device_id, 48),
                     "frame_id": str(event_id)[:64],
                     "type": t})
        return out

    # ---- peripheral worker stubs -------------------------------------------

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
            desc.validate()
        except Exception as exc:
            return proto.make_render_result(
                request_id,
                make_not_supported_result("", "invalid: %s" % exc))
        return proto.make_render_result(
            request_id, make_not_supported_result(desc.frame_id,
                                                  "nrr backend unavailable"))

    @staticmethod
    def worker_stub_scene(request: Dict[str, Any]) -> Dict[str, Any]:
        """Answer a scene/motion request with not_supported (peripheral no-op).

        Validates the motion request, then returns the stub envelope built
        directly (no entities -- a refusal must not trip scene validation).
        """
        request_id = str(request.get("request_id", ""))[:64]
        motion_request = request.get("motion_request") or {}
        try:
            req = NRRMotionRequest.from_dict(motion_request)
            req.validate()
        except Exception as exc:
            return proto.make_scene_result(
                request_id,
                make_not_supported_scene_result("", "invalid: %s" % exc))
        return proto.make_scene_result(
            request_id,
            make_not_supported_scene_result(req.frame_id,
                                            "nrr scene backend unavailable"))

    # ---- internal ------------------------------------------------------------

    def _audit(self, event_type: str, payload: Dict[str, Any]) -> None:
        if self.audit is None:
            return
        try:
            self.audit.append(event_type, payload)
        except Exception:
            pass



class NRRRenderWorker:
    """Peripheral worker that answers render requests with a real backend.

    The transport adapter's ``worker_stub`` is the fail-closed placeholder.
    This is the step up from it: the same request/response envelopes, but the
    frame is produced by a local ``frame_source`` and rendered by a local
    ``renderer``.  Both are injected, so this class holds no device or
    framework dependency and is fully testable offline.

    Contract preserved from the stub:
      * the descriptor is validated before anything runs (fail-closed),
      * a request that cannot be served answers ``not_supported`` with a
        reason rather than raising or returning a half-rendered frame,
      * no pixel bytes appear in any envelope -- only an output handle and
        stats, so nothing leaks onto the mesh.

    ``frame_source(width, height) -> bytes | None``  packed RGBA8, local only.
    ``renderer(width, height, rgba) -> bytes | None`` packed RGB8.
    """

    def __init__(self, renderer=None, frame_source=None, backend: str = "cpu",
                 capabilities_provider=None, audit=None):
        self.renderer = renderer
        self.frame_source = frame_source
        self.backend = sanitize_text(str(backend), 32) or "cpu"
        self.capabilities_provider = capabilities_provider
        self.audit = audit

    @property
    def available(self) -> bool:
        """True only when both halves of the pipeline are wired."""
        return callable(self.renderer) and callable(self.frame_source)

    # ---- render -------------------------------------------------------------

    def render(self, request: Dict[str, Any]) -> Dict[str, Any]:
        """Answer one render request with a render-result envelope."""
        request_id = str(request.get("request_id", ""))[:64]
        descriptor = request.get("descriptor") or {}
        try:
            desc = NRRFrameDescriptor.from_dict(descriptor)
            desc.validate()
        except Exception as exc:
            self._audit("nrr_worker_refused_invalid_descriptor",
                        {"request_id": request_id, "reason": str(exc)[:200]})
            return proto.make_render_result(
                request_id, make_not_supported_result("", "invalid: %s" % exc))

        if not self.available:
            return proto.make_render_result(
                request_id,
                make_not_supported_result(desc.frame_id,
                                          "nrr backend unavailable"))

        width, height = int(desc.width), int(desc.height)
        try:
            rgba = self.frame_source(width, height)
        except Exception as exc:
            self._audit("nrr_worker_frame_failed",
                        {"request_id": request_id, "reason": str(exc)[:200]})
            return proto.make_render_result(
                request_id,
                make_not_supported_result(desc.frame_id,
                                          "frame capture failed: %s" % exc))
        if not rgba:
            return proto.make_render_result(
                request_id,
                make_not_supported_result(desc.frame_id, "no local frame"))

        started = time.monotonic()
        try:
            rgb = self.renderer(width, height, rgba)
        except Exception as exc:
            self._audit("nrr_worker_render_failed",
                        {"request_id": request_id, "reason": str(exc)[:200]})
            return proto.make_render_result(
                request_id,
                make_not_supported_result(desc.frame_id,
                                          "render failed: %s" % exc))
        if not rgb:
            return proto.make_render_result(
                request_id,
                make_not_supported_result(desc.frame_id, "renderer returned none"))

        elapsed_ms = (time.monotonic() - started) * 1000.0
        result = NRRRenderResult(
            frame_id=desc.frame_id,
            status="ok",
            output_handle="local:%s" % desc.frame_id,
            width=width,
            height=height,
            stats=NRRRenderStats(render_time_ms=elapsed_ms,
                                 neural_inference_time_ms=elapsed_ms,
                                 backend=self.backend),
        ).to_dict()
        self._audit("nrr_worker_rendered",
                    {"request_id": request_id, "frame_id": desc.frame_id,
                     "render_time_ms": round(elapsed_ms, 3)})
        return proto.make_render_result(request_id, result)

    # ---- capability advertisement -------------------------------------------

    def compute_caps(self) -> Dict[str, Any]:
        """``compute_caps`` block for the pairing manifest.

        Advertises ``nrr_render`` only when the worker can actually serve it,
        so the primary's ``nodes_for_workload`` routing never sends pixel work
        to a node that would answer ``not_supported``.
        """
        if not self.available:
            return {}
        caps: Dict[str, Any] = {"workloads": ["nrr_render"]}
        if self.capabilities_provider is not None:
            try:
                caps.update(self.capabilities_provider() or {})
            except Exception:
                pass
        return caps

    def _audit(self, event_type: str, payload: Dict[str, Any]) -> None:
        if self.audit is None:
            return
        try:
            self.audit.append(event_type, payload)
        except Exception:
            pass



def android_native_worker(model_path: str, frame_source=None,
                          backend: str = "CPU", audit=None):
    """Build an :class:`NRRRenderWorker` backed by the native Android runtime.

    Returns ``None`` when the native bridge is unavailable -- not on Android,
    Chaquopy absent, ``libnrr_jni.so`` missing, device creation failed, or the
    model would not load.  Callers keep the fail-closed ``worker_stub`` in
    that case rather than degrading silently.

    ``frame_source(width, height) -> bytes`` supplies local RGBA8 pixels
    (camera/screen capture); pixels never leave the device.
    """
    try:
        from java import jclass  # provided by Chaquopy on Android
    except Exception:
        return None

    try:
        bridge_cls = jclass("com.samurai.shugocore.inference.NRRBridge")
        bridge = bridge_cls(model_path, backend)
        if not bridge.initialize():
            return None
    except Exception:
        return None

    def _render(width: int, height: int, rgba):
        return bridge.render(width, height, rgba)

    def _caps():
        """Advertise only what the runtime actually does.

        ``execution_provider`` is reported by the native layer as "CPU"
        unless NNAPI was genuinely appended, so ``neural_acceleration`` is
        derived from that rather than from a vendor backend's wish list.
        """
        try:
            caps = dict(bridge.capabilities())
        except Exception:
            return {}
        out: Dict[str, Any] = {}
        out["int8"] = caps.get("int8") in ("basic", "optimized", "full")
        out["fp16"] = caps.get("fp16") in ("basic", "optimized", "full")
        nnapi = bool(caps.get("supports_nnapi"))
        out["neural_acceleration"] = "full" if nnapi else "absent"
        out["execution_provider"] = str(caps.get("execution_provider", "CPU"))
        try:
            out["vram_mb"] = int(caps.get("vram_mb", 0))
        except (TypeError, ValueError):
            out["vram_mb"] = 0
        return out

    return NRRRenderWorker(renderer=_render, frame_source=frame_source,
                           backend=backend.lower(),
                           capabilities_provider=_caps, audit=audit)

