"""
ShugoCore mobile fleet management (host side)
=============================================

Host-side counterpart of the Android node runtime: pairing, topic ACL
enforcement, sensor ingestion with sanitization, heartbeat liveness, and
compute offload to paired Android nodes.

Security model (DDS Security is unavailable on every DDS stack today, so
trust is established at the application layer):

- **Pairing = consent.** Only operator-allowlisted device_ids are accepted;
  a pairing grant carries a TTL (default 12h) and is audited.
- **Topic ACL.** A paired device may only surface data on
  ``/shugocore/mobile/{device_id}/{contract_topic}``. Inbound data on any
  other topic is refused and audited - actuation topics are unreachable.
- **Untrusted input.** Every payload is sanitized (NaN/Inf rejection, size
  caps, bounded strings) before it can reach memory or decisions.
- **Compute offload** is a side-effecting action: it leaves the host and
  executes on a personal device, so it flows through the same consent gate
  as other side effects.
"""

import logging
import threading
import time
import uuid
from typing import Any, Callable, Dict, List, Optional, Tuple

from policy import (
    MOBILE_ACTION_TYPES,
    MOBILE_READ_ACTION_TYPES,
)
from security import sanitize_text
from telemetry import get_tracer

logger = logging.getLogger(__name__)
tracer = get_tracer("shugocore.mobile")

DEFAULT_PAIRING_TTL_HOURS = 12.0
_MAX_SNAPSHOT_BYTES = 4096


def parse_mobile_topic(topic: str) -> Optional[Tuple[str, str]]:
    """Split ``/shugocore/mobile/{device_id}/{tail}``; None when not in
    the mobile namespace."""
    parts = str(topic or "").strip("/").split("/")
    if (len(parts) == 4 and parts[0] == "shugocore"
            and parts[1] == "mobile"):
        return parts[2], parts[3]
    return None


class MobileNodeRegistry:
    """Operator-managed pairing of Android compute nodes, with TTLs."""

    def __init__(self, audit: Optional[Any] = None,
                 pairing_ttl_hours: float = DEFAULT_PAIRING_TTL_HOURS,
                 heartbeat_timeout: float = 30.0):
        self.audit = audit
        self.pairing_ttl_hours = max(0.01, float(pairing_ttl_hours))
        self.heartbeat_timeout = max(1.0, float(heartbeat_timeout))
        self._paired: Dict[str, Dict[str, Any]] = {}
        self._last_heartbeat: Dict[str, float] = {}
        self._lock = threading.Lock()

    def pair(self, device_id: str, manifest: Optional[Dict[str, Any]] = None,
             paired_by: str = "operator") -> Dict[str, Any]:
        device = sanitize_text(device_id, 48)
        if not device:
            raise ValueError("device_id required")
        entry = {
            "device_id": device,
            "manifest": manifest if isinstance(manifest, dict) else {},
            "paired_by": sanitize_text(paired_by, 120),
            "paired_at": time.time(),
            "expires_at": time.time() + self.pairing_ttl_hours * 3600.0,
        }
        with self._lock:
            self._paired[device] = entry
            self._last_heartbeat[device] = time.monotonic()
        self._audit("mobile_node_paired", {"device_id": device,
                                           "paired_by": entry["paired_by"]})
        return dict(entry)

    def unpair(self, device_id: str) -> bool:
        with self._lock:
            removed = self._paired.pop(str(device_id), None)
            self._last_heartbeat.pop(str(device_id), None)
        if removed:
            self._audit("mobile_node_unpaired",
                        {"device_id": sanitize_text(device_id, 48)})
        return removed is not None

    def is_paired(self, device_id: str) -> bool:
        now = time.time()
        with self._lock:
            entry = self._paired.get(str(device_id))
        return entry is not None and entry["expires_at"] > now

    def heartbeat(self, device_id: str) -> bool:
        device = str(device_id)
        with self._lock:
            if device not in self._paired:
                return False
            self._last_heartbeat[device] = time.monotonic()
        return True

    def alive(self, device_id: str) -> bool:
        with self._lock:
            last = self._last_heartbeat.get(str(device_id))
        return last is not None and (time.monotonic() - last) <= self.heartbeat_timeout

    def expired(self) -> List[str]:
        now = time.time()
        with self._lock:
            return [d for d, e in self._paired.items() if e["expires_at"] <= now]

    def list_nodes(self) -> List[Dict[str, Any]]:
        now = time.time()
        with self._lock:
            out = []
            for device, entry in sorted(self._paired.items()):
                if entry["expires_at"] <= now:
                    continue
                out.append({
                    "device_id": device,
                    "manifest": dict(entry["manifest"]),
                    "paired_by": entry["paired_by"],
                    "expires_at": entry["expires_at"],
                    "alive": (time.monotonic()
                              - self._last_heartbeat.get(device, 0.0)
                              <= self.heartbeat_timeout),
                })
            return out

    def manifest(self, device_id: str) -> Dict[str, Any]:
        with self._lock:
            entry = self._paired.get(str(device_id))
        return dict(entry["manifest"]) if entry else {}

    def _audit(self, event_type: str, payload: Dict[str, Any]) -> None:
        if self.audit is None:
            return
        try:
            self.audit.append(event_type, payload)
        except Exception:
            pass

class MobileNodeManager:
    """
    Ingests sensor/telemetry data from paired Android nodes over a ROS 2
    interface, enforcing the topic ACL and sanitizing everything before it
    is stored. Also tracks liveness via heartbeats.
    """

    def __init__(self, ros2: Any, registry: MobileNodeRegistry,
                 capabilities: Any, memory: Optional[Any] = None,
                 fallbacks: Optional[Any] = None,
                 audit: Optional[Any] = None):
        self._ros2 = ros2
        self.registry = registry
        self.capabilities = capabilities
        self.memory = memory
        self.fallbacks = fallbacks
        self.audit = audit
        self._snapshots: Dict[str, Dict[str, Any]] = {}  # device -> {tail: data}
        self._lock = threading.Lock()
        self._refused: Dict[str, int] = {}

    def subscribe_device(self, device_id: str) -> bool:
        """Subscribe to all contract topics for a paired device."""
        if not self.registry.is_paired(device_id):
            return False
        for tail in self.capabilities.mobile_sensor_topics:
            topic = f"/shugocore/mobile/{device_id}/{tail}"
            self._ros2.create_subscriber(
                topic, "std", self._make_callback(device_id, tail))
        return True

    def _make_callback(self, device_id: str, tail: str) -> Callable:
        def _cb(message: Any) -> None:
            self.ingest(device_id, tail, message)
        return _cb

    def ingest(self, device_id: str, tail: str, message: Any) -> Optional[Dict[str, Any]]:
        """
        Topic-ACL check + sanitization + storage. Returns the stored record
        or None when refused (refusals are counted and audited).
        """
        ok, reason = self.capabilities.validate_mobile_topic(device_id, tail)
        if not ok:
            with self._lock:
                key = f"{device_id}/{tail}"
                self._refused[key] = self._refused.get(key, 0) + 1
            self._audit("mobile_topic_refused",
                        {"device_id": sanitize_text(device_id, 48),
                         "tail": sanitize_text(tail, 32),
                         "reason": reason})
            if self.fallbacks is not None and self._refused.get(
                    f"{device_id}/{tail}", 0) >= 3:
                self.fallbacks.report_violation(
                    "mobile_sensor_anomaly",
                    f"repeated ACL refusals from {device_id}/{tail}")
            return None
        if not self.registry.is_paired(device_id):
            return None
        if tail == "heartbeat":
            self.registry.heartbeat(device_id)
        data = self._sanitize(message)
        if data is None:
            self._audit("mobile_payload_refused",
                        {"device_id": sanitize_text(device_id, 48),
                         "tail": sanitize_text(tail, 32),
                         "reason": "payload failed sanitization"})
            return None
        record = {"device_id": device_id, "tail": tail,
                  "data": data, "ts": time.time()}
        with self._lock:
            self._snapshots.setdefault(device_id, {})[tail] = record
        if self.memory is not None and tail != "heartbeat":
            try:
                self.memory.tier1.record("mobile_sensor", {
                    "device_id": device_id, "tail": tail,
                    "summary": sanitize_text(str(data), 200)})
            except Exception as exc:
                logger.debug("Episodic record failed: %s", exc)
        return record

    def _sanitize(self, message: Any) -> Optional[Any]:
        """Bound payloads: reject oversize, keep finite numbers, sanitize
        strings, cap containers."""
        if isinstance(message, bool):
            return message
        if isinstance(message, (int, float)):
            return message if -1e15 < float(message) < 1e15 else 0.0
        text = str(message)
        if len(text) > _MAX_SNAPSHOT_BYTES:
            return None
        if isinstance(message, dict):
            return {sanitize_text(str(k), 48): self._sanitize(v)
                    for k, v in list(message.items())[:32]}
        if isinstance(message, (list, tuple)):
            return [self._sanitize(v) for v in list(message)[:32]]
        return sanitize_text(text, 400)

    def get_sensor_snapshot(self, device_id: str) -> Dict[str, Any]:
        with self._lock:
            snaps = self._snapshots.get(str(device_id), {})
            return {tail: dict(record) for tail, record in sorted(snaps.items())}

    def check_liveness(self) -> List[str]:
        """Report lost nodes to the fallback controller; returns lost ids."""
        nodes = self.registry.list_nodes()
        lost = [d["device_id"] for d in nodes if not d["alive"]]
        if lost and self.fallbacks is not None:
            self.fallbacks.report_violation(
                "mobile_node_lost", f"heartbeat lost: {', '.join(lost)}")
        return lost

    def _audit(self, event_type: str, payload: Dict[str, Any]) -> None:
        if self.audit is None:
            return
        try:
            self.audit.append(event_type, payload)
        except Exception:
            pass

class MobileComputeBroker:
    """
    Topic-based request/reply compute offload to a paired Android node
    (portable across jros2 and rosbridge since jros2 has no services yet):
    publish ``{request_id, workload, payload}`` to the device's
    ``compute_request`` topic, wait for the correlated ``compute_result``.
    """

    def __init__(self, ros2: Any, registry: MobileNodeRegistry,
                 capabilities: Any, audit: Optional[Any] = None):
        self._ros2 = ros2
        self.registry = registry
        self.capabilities = capabilities
        self.audit = audit
        self._results: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.Lock()
        self._listening: set = set()

    def _listen(self, device_id: str) -> None:
        if device_id in self._listening:
            return
        self._listening.add(device_id)
        result_topic = f"/shugocore/mobile/{device_id}/compute_result"
        self._ros2.create_subscriber(result_topic, "std", self._on_result)

    def _on_result(self, message: Any) -> None:
        if not isinstance(message, dict):
            return
        request_id = str(message.get("request_id", ""))
        if request_id:
            with self._lock:
                self._results[request_id] = message

    def request_compute(self, device_id: str, workload: str,
                        payload: Dict[str, Any],
                        timeout: Optional[float] = None) -> Dict[str, Any]:
        """
        Offload ``workload`` to ``device_id``. Blocks up to ``timeout``
        (default: ``mobile_compute_timeout``). Fails closed: unpaired or
        dead devices are refused; timeout returns an error result.
        """
        if not self.registry.is_paired(device_id):
            return {"status": "refused", "reason": "device not paired"}
        if not self.registry.alive(device_id):
            return {"status": "refused", "reason": "device heartbeat lost"}
        request_id = uuid.uuid4().hex[:16]
        timeout = float(timeout or self.capabilities.mobile_compute_timeout)
        self._listen(device_id)
        request_topic = f"/shugocore/mobile/{device_id}/compute_request"
        with tracer.start_span("mobile.compute_request",
                               {"device_id": device_id,
                                "workload": workload}) as span:
            try:
                self._ros2.publish(request_topic, {
                    "request_id": request_id,
                    "workload": sanitize_text(workload, 32),
                    "payload": payload if isinstance(payload, dict) else {},
                })
            except Exception as exc:
                span.set_attribute("status", "publish_failed")
                return {"status": "error", "reason": f"publish failed: {exc}"}
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                with self._lock:
                    result = self._results.pop(request_id, None)
                if result is not None:
                    span.set_attribute("status", "ok")
                    self._audit("mobile_compute_completed",
                                {"device_id": device_id, "request_id": request_id})
                    return {"status": "success", "result": result}
                time.sleep(0.02)
        span.set_attribute("status", "timeout")
        self._audit("mobile_compute_timeout",
                    {"device_id": device_id, "request_id": request_id})
        return {"status": "error", "reason": f"compute timeout after {timeout:.1f}s"}

    def _audit(self, event_type: str, payload: Dict[str, Any]) -> None:
        if self.audit is None:
            return
        try:
            self.audit.append(event_type, payload)
        except Exception:
            pass


class MobileExecutionHandler:
    """
    Execution-layer handler for the mobile action types, mirroring the
    robotics handler pattern. Consent for ``mobile_request_compute`` is
    enforced by the engine's action-level gate before this handler runs.
    """

    def __init__(self, manager: MobileNodeManager, broker: MobileComputeBroker):
        self.manager = manager
        self.broker = broker
        self._lock = threading.Lock()

    def handle(self, decision: Dict[str, Any]) -> Dict[str, Any]:
        action_type = str(decision.get("action_type", ""))
        params = decision.get("params") or {}
        if action_type == "mobile_list_nodes":
            return {"status": "success", "action": "mobile_list_nodes",
                    "nodes": self.registry().list_nodes()}
        if action_type == "mobile_node_status":
            device_id = sanitize_text(str(params.get("device_id", "")), 48)
            if not self.registry().is_paired(device_id):
                return {"status": "refused", "reason": "device not paired"}
            return {"status": "success", "action": "mobile_node_status",
                    "device_id": device_id,
                    "paired": True,
                    "alive": self.registry().alive(device_id),
                    "sensors": self.manager.get_sensor_snapshot(device_id)}
        if action_type == "mobile_request_compute":
            device_id = sanitize_text(str(params.get("device_id", "")), 48)
            workload = str(params.get("workload", "vision"))
            result = self.broker.request_compute(
                device_id, workload,
                payload=params.get("payload") if isinstance(params.get("payload"), dict) else {},
                timeout=params.get("timeout") if isinstance(params.get("timeout"), (int, float)) else None)
            return {"status": result["status"],
                    "action": "mobile_request_compute",
                    "device_id": device_id, **result}
        return {"status": "refused", "reason": f"unknown mobile action '{action_type}'"}

    def registry(self) -> MobileNodeRegistry:
        return self.manager.registry

# __HANDLER_END_SENTINEL__


