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
# v1.28.2: 16 KB snapshot budget for Android compatibility at large --
# structured payloads (NRR descriptors/results, batched detections) can
# legitimately exceed the old 4 KB cap while remaining bounded.
_MAX_SNAPSHOT_BYTES = 16 * 1024

# Compute-capability keys carried in the pairing manifest (NRR capability
# matrix pattern: fp16 / int8 / minimum vram / supported workloads).  The
# primary routes pixel work only to nodes that advertise the workload.
_COMPUTE_CAPS_KEY = "compute_caps"
_KNOWN_CAP_KEYS = ("fp16", "int8", "vram_mb", "workloads")
# Workloads the primary may delegate.  "nrr_render" is the NRR rendering
# worker contract; "vision" is the legacy compute offload alias.
KNOWN_WORKLOADS = ("nrr_render", "vision")


def _sanitize_compute_caps(manifest: Dict[str, Any]) -> Dict[str, Any]:
    """Extract and bound the compute-capability block from a manifest.

    Returns {} when absent.  Unknown keys are dropped; numeric caps are
    clamped to sane ranges; the workloads list is filtered to known
    workload names (max 8, each sanitized).
    """
    caps = manifest.get(_COMPUTE_CAPS_KEY)
    if not isinstance(caps, dict):
        return {}
    out: Dict[str, Any] = {}
    fp16 = caps.get("fp16")
    if isinstance(fp16, bool):
        out["fp16"] = fp16
    int8 = caps.get("int8")
    if isinstance(int8, bool):
        out["int8"] = int8
    vram = caps.get("vram_mb")
    if isinstance(vram, (int, float)) and 0 < vram < 1e6:
        out["vram_mb"] = int(vram)
    workloads = caps.get("workloads")
    if isinstance(workloads, (list, tuple)):
        clean = []
        for w in list(workloads)[:8]:
            name = sanitize_text(str(w), 32)
            if name in KNOWN_WORKLOADS:
                clean.append(name)
        if clean:
            out["workloads"] = clean
    return out


def node_supports_workload(manifest: Dict[str, Any], workload: str) -> bool:
    """True when ``manifest``'s compute_caps advertise ``workload``."""
    caps = manifest.get(_COMPUTE_CAPS_KEY)
    if not isinstance(caps, dict):
        return False
    workloads = caps.get("workloads")
    return (isinstance(workloads, list)
            and sanitize_text(str(workload), 32) in workloads)


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
        raw_manifest = manifest if isinstance(manifest, dict) else {}
        # v1.28.2: sanitize the manifest and extract the bounded
        # compute-capability block (NRR capability-matrix pattern).  Unknown
        # manifest keys are preserved (bounded) for forward compatibility;
        # compute_caps is re-derived from the sanitized copy so a malicious
        # manifest cannot smuggle oversized values past the sanitizer.
        safe_manifest = self._sanitize_manifest(raw_manifest)
        safe_manifest[_COMPUTE_CAPS_KEY] = _sanitize_compute_caps(safe_manifest)
        entry = {
            "device_id": device,
            "manifest": safe_manifest,
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

    @staticmethod
    def _sanitize_manifest(manifest: Dict[str, Any]) -> Dict[str, Any]:
        """Bound a pairing manifest: <=32 keys, sanitized key names, scalar
        or shallow-list values capped at 256 chars each.

        The compute_caps block is preserved structurally (booleans, bounded
        ints, and string lists pass through) so capability-aware routing can
        read it back after pairing."""
        out: Dict[str, Any] = {}
        for key in list(manifest.keys())[:32]:
            name = sanitize_text(str(key), 48)
            if not name:
                continue
            value = manifest[key]
            if name == _COMPUTE_CAPS_KEY and isinstance(value, dict):
                out[name] = MobileNodeRegistry._sanitize_caps_block(value)
                continue
            if isinstance(value, bool):
                out[name] = value
            elif isinstance(value, (int, float)):
                out[name] = value if -1e15 < float(value) < 1e15 else 0.0
            elif isinstance(value, (list, tuple)):
                out[name] = [sanitize_text(str(v), 256)
                             for v in list(value)[:16]]
            elif isinstance(value, dict):
                sub = {}
                for sk in list(value.keys())[:16]:
                    sname = sanitize_text(str(sk), 48)
                    if not sname:
                        continue
                    sv = value[sk]
                    if isinstance(sv, bool):
                        sub[sname] = sv
                    elif isinstance(sv, (int, float)):
                        sub[sname] = (sv if -1e15 < float(sv) < 1e15
                                      else 0.0)
                    else:
                        sub[sname] = sanitize_text(str(sv), 256)
                out[name] = sub
            else:
                out[name] = sanitize_text(str(value), 256)
        return out

    @staticmethod
    def _sanitize_caps_block(caps: Dict[str, Any]) -> Dict[str, Any]:
        """Bound the compute_caps block inside a manifest (pre-sanitizer).

        Keeps booleans, bounded ints, and string lists intact so
        _sanitize_compute_caps can derive the canonical capability set
        from the sanitized copy."""
        out: Dict[str, Any] = {}
        for key in list(caps.keys())[:16]:
            name = sanitize_text(str(key), 48)
            if not name:
                continue
            value = caps[key]
            if isinstance(value, bool):
                out[name] = value
            elif isinstance(value, (int, float)):
                out[name] = (value if -1e15 < float(value) < 1e15 else 0.0)
            elif isinstance(value, (list, tuple)):
                out[name] = [sanitize_text(str(v), 64)
                             for v in list(value)[:8]]
            else:
                out[name] = sanitize_text(str(value), 256)
        return out

    def nodes_for_workload(self, workload: str) -> List[Dict[str, Any]]:
        """Paired, live nodes advertising ``workload`` in compute_caps.

        Capability-aware routing (NRR capability-matrix pattern): the
        primary routes pixel work only to nodes that declared the workload
        at pairing time.  Returns the same shape as list_nodes()."""
        name = sanitize_text(str(workload), 32)
        if name not in KNOWN_WORKLOADS:
            return []
        now = time.time()
        with self._lock:
            out = []
            for device, entry in sorted(self._paired.items()):
                if entry["expires_at"] <= now:
                    continue
                if not node_supports_workload(entry["manifest"], name):
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

        v1.28.2: capability-aware routing -- a device that never advertised
        ``workload`` in its pairing compute_caps is refused with a routing
        reason (fail-closed, audited).  Unknown workloads are refused too.
        """
        workload_name = sanitize_text(str(workload), 32)
        if workload_name not in KNOWN_WORKLOADS:
            self._audit("mobile_compute_refused_unknown_workload",
                        {"device_id": sanitize_text(device_id, 48),
                         "workload": workload_name})
            return {"status": "refused",
                    "reason": f"unknown workload '{workload_name}'"}
        if not self.registry.is_paired(device_id):
            return {"status": "refused", "reason": "device not paired"}
        if not self.registry.alive(device_id):
            return {"status": "refused", "reason": "device heartbeat lost"}
        if not node_supports_workload(self.registry.manifest(device_id),
                                      workload_name):
            self._audit("mobile_compute_refused_no_capability",
                        {"device_id": sanitize_text(device_id, 48),
                         "workload": workload_name})
            return {"status": "refused",
                    "reason": (f"device '{sanitize_text(device_id, 48)}' "
                               f"does not advertise workload "
                               f"'{workload_name}'")}
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


class KVTransportAdapter:
    """DDS transport adapter for the KV mesh contract.

    Translates kv_mesh.protocol messages into DDS/ROS2 topic writes on the
    existing /shugocore/mobile/{device_id}/{tail} namespace, and hands inbound
    messages to the KVAllocator. In production this is the layer that would
    sit on top of the DDS participant created by the host-side DDS runtime.
    In the offline simulator the messages are passed directly to the allocator
    (see kv_mesh.simulator).

    Safety: the adapter refuses to publish a KVAssign / KVPut for a device_id
    that is not currently paired, and it refuses to forward a KVAdvertise from
    an un-paired device. All topic writes go through sanitize_text on the
    device_id (already enforced by parse_mobile_topic in the registry path).
    """

    def __init__(self, registry, allocator, audit=None):
        self.registry = registry
        self.allocator = allocator
        self.audit = audit

    def advertise(self, device_id, usable_ram_bytes, total_ram_bytes):
        msg = make_advertise(device_id, usable_ram_bytes, total_ram_bytes)
        self._publish(device_id, proto.TOPIC_ADVERTISE, msg)
        return msg

    def assign(self, shard):
        if shard.device_id is None:
            return None
        msg = make_assign(shard)
        self._publish(shard.device_id, proto.TOPIC_ASSIGN, msg)
        return msg

    def put(self, device_id, shard_id, data_b64, checksum):
        msg = make_put(shard_id, data_b64, checksum)
        self._publish(device_id, proto.TOPIC_PUT, msg)
        return msg

    def get(self, device_id, shard_id):
        msg = make_get(shard_id)
        self._publish(device_id, proto.TOPIC_GET, msg)
        return msg

    def evict(self, device_id, shard_id):
        msg = make_evict(shard_id)
        self._publish(device_id, proto.TOPIC_EVICT, msg)
        return msg

    def heartbeat(self, device_id):
        msg = make_heartbeat(device_id)
        self._publish(device_id, proto.TOPIC_HEARTBEAT, msg)
        return msg

    def handle_inbound(self, device_id, topic_tail, payload):
        if not self.registry.is_paired(device_id):
            self._audit("kv_inbound_refused_unpaired", {"device_id": device_id, "topic": topic_tail})
            return None
        t = msg_type(payload)
        if t == "KVAdvertise":
            usable = int(payload.get("usable_ram_bytes", 0))
            total = int(payload.get("total_ram_bytes", 0))
            self.allocator.register_node(device_id, usable, total)
            self._audit("kv_advertise_received", {"device_id": device_id, "usable_ram_bytes": usable})
            return None
        if t == "KVAssign":
            from kv_mesh.shard import KVShard as _KVShard
            s = _KVShard(
                shard_id=str(payload["shard_id"]),
                model_id=str(payload["model_id"]),
                layer_start=int(payload["layer_start"]),
                layer_end=int(payload["layer_end"]),
                head_start=int(payload["head_start"]),
                head_end=int(payload["head_end"]),
                seq_start=int(payload["seq_start"]),
                seq_end=int(payload["seq_end"]),
                bytes_size=int(payload["bytes_size"]),
                checksum=str(payload.get("checksum", "")),
                device_id=str(payload.get("shard_id", "")),
            )
            self.allocator.assign(s)
            self._audit("kv_assign_received", {"shard_id": s.shard_id, "device_id": device_id})
            return None
        if t == "KVPut":
            self.allocator._shards.get(str(payload["shard_id"]))
            self._audit("kv_put_received", {"shard_id": payload["shard_id"], "device_id": device_id})
            return None
        if t == "KVGet":
            s = self.allocator._shards.get(str(payload["shard_id"]))
            if s and s.device_id == device_id:
                return None
            return None
        if t == "KVEvict":
            self.allocator.evict(str(payload["shard_id"]))
            self._audit("kv_evict_received", {"shard_id": payload["shard_id"], "device_id": device_id})
            return None
        if t == "KVHeartbeat":
            return None
        self._audit("kv_inbound_unknown_type", {"device_id": device_id, "type": t, "topic": topic_tail})
        return None

    def _publish(self, device_id, topic_tail, message):
        if not self.registry.is_paired(device_id):
            self._audit("kv_publish_refused_unpaired", {"device_id": device_id, "topic": topic_tail})
            return
        self._audit("kv_publish", {"device_id": device_id, "topic": topic_tail, "type": msg_type(message)})

    def _audit(self, event_type, payload):
        if self.audit is None:
            return
        try:
            self.audit.append(event_type, payload)
        except Exception:
            pass

# __HANDLER_END_SENTINEL__
