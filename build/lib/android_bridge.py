"""
ShugoCore Android bridge transports
===================================

Two pluggable ``BaseROS2Interface`` implementations for running ShugoCore on
Android as a first-class ROS 2 participant:

- ``JavaBridgeROS2Interface`` - in-process bridge to
  `jros2 <https://github.com/ihmcrobotics/jros2>`_ (Fast-DDS native Android
  libs) through a Kotlin shell object injected via
  `Chaquopy <https://chaquo.com/chaquopy/>`_ Python-Java interop. This is the
  primary path: one app process hosts both the ROS 2 transport and Python.

- ``RosBridgeInterface`` - JSON-over-WebSocket client for the standard
  ``rosbridge_suite`` server. Used for Termux deployments (no app shell
  needed) and for testing against any ROS 2 machine. Requires the optional
  ``websocket-client`` package.

Both reuse the shared validators (``sanitize_twist``,
``validate_joint_trajectory``) so safety-critical message validation cannot
drift between transports.

Kotlin bridge contract (duck-typed; see platforms/android/ for the reference
shell). All handles are opaque. Polling (``drainMessages``) is deliberately
used instead of Java->Python callbacks: it avoids cross-thread callback
marshaling hazards under Chaquopy and keeps ``spin_once`` deterministic.

    createNode(name: str, domainId: int) -> node
    createPublisher(node, topic: str, msgType: str, qos: int) -> pubHandle
    publish(pubHandle, jsonPayload: str) -> bool
    createSubscriber(node, topic: str, msgType: str, qos: int) -> subHandle
    drainMessages(subHandle) -> List[str]        # JSON payloads, oldest first
    getSubscriptionCount(pubHandle) -> int
    closeHandle(handle) -> None
    destroyNode(node) -> None
    isAlive() -> bool
"""

import json
import logging
import threading
import time
from typing import Any, Callable, Dict, List, Optional

from ros2_interface import (
    BaseROS2Interface,
    JointState,
    JointTrajectory,
    LaserScan,
    Pose,
    Twist,
    Vector3,
    sanitize_twist,
    validate_joint_trajectory,
)

logger = logging.getLogger(__name__)

# Our message classes -> ROS 2 type strings (rosbridge + bridge contracts).
ROS_TYPE_NAMES: Dict[str, str] = {
    "Twist": "geometry_msgs/Twist",
    "JointTrajectory": "trajectory_msgs/JointTrajectory",
    "JointState": "sensor_msgs/JointState",
    "LaserScan": "sensor_msgs/LaserScan",
    "Pose": "geometry_msgs/Pose",
}


def ros_type_of(msg_type: str) -> str:
    """Map our message class name to a ROS 2 type string."""
    return ROS_TYPE_NAMES.get(str(msg_type), "std_msgs/String")


def to_payload(message: Any) -> Dict[str, Any]:
    """
    Convert a ShugoCore message object into a JSON-safe dict matching the
    ROS 2 message fields. Unknown objects fall back to a JSON string field.
    """
    if isinstance(message, dict):
        return message
    if hasattr(message, "to_dict"):
        return dict(message.to_dict())
    return {"data": str(message)}


def message_from_payload(payload: Any) -> Any:
    """
    Rebuild a ShugoCore message object from a ROS-field dict. Returns the
    original dict for unrecognized shapes (never raises on malformed input).
    """
    if not isinstance(payload, dict):
        return payload
    keys = set(payload.keys())
    try:
        if {"linear", "angular"} <= keys:
            return Twist(
                linear=Vector3(**{k: float(v) for k, v in payload["linear"].items()
                                  if k in ("x", "y", "z")}),
                angular=Vector3(**{k: float(v) for k, v in payload["angular"].items()
                                   if k in ("x", "y", "z")}))
        if {"name", "position"} <= keys:
            return JointState(name=list(payload.get("name") or []),
                              position=[float(v) for v in payload.get("position") or []],
                              velocity=[float(v) for v in payload.get("velocity") or []],
                              effort=[float(v) for v in payload.get("effort") or []])
        if {"ranges", "angle_min"} <= keys:
            return LaserScan(ranges=[float(v) for v in payload.get("ranges") or []],
                             angle_min=float(payload.get("angle_min", 0.0)),
                             angle_max=float(payload.get("angle_max", 0.0)),
                             range_min=float(payload.get("range_min", 0.0)),
                             range_max=float(payload.get("range_max", 0.0)))
        if {"joint_names", "points"} <= keys:
            return JointTrajectory(
                joint_names=list(payload.get("joint_names") or []),
                points=list(payload.get("points") or []),
            )
    except (TypeError, ValueError, AttributeError) as exc:
        logger.debug("Payload reconstruction degraded to dict: %s", exc)
        return payload
    return payload


# ---------------------------------------------------------------------------
# JavaBridgeROS2Interface (Chaquopy + jros2, in-process Fast-DDS)
# ---------------------------------------------------------------------------
class JavaBridgeROS2Interface(BaseROS2Interface):
    """
    ROS 2 transport via the Kotlin/jros2 bridge object (see module docstring
    for the contract). Messages cross the Java boundary as JSON payloads;
    inbound messages are drained on ``spin_once`` and dispatched to Python
    callbacks, so no Java thread ever enters Python directly.
    """

    def __init__(self, bridge: Any, node_name: str = "shugocore_android",
                 domain_id: int = 0, rate_limit_hz: float = 30.0):
        self._bridge = bridge
        self._node_name = str(node_name)[:64]
        self._domain_id = max(0, min(232, int(domain_id)))  # DDS-valid range
        self._rate_limit_hz = max(0.1, float(rate_limit_hz))
        self._min_interval = 1.0 / self._rate_limit_hz
        self._publishers: Dict[str, Dict[str, Any]] = {}   # topic -> {handle, msg_type}
        self._subscribers: Dict[str, Dict[str, Any]] = {}  # topic -> {handle, callback}
        self._published_log: List[Dict[str, Any]] = []
        self._last_publish_time: Dict[str, float] = {}
        self._lock = threading.RLock()
        self._shutdown = False
        self._node = self._bridge.createNode(self._node_name, self._domain_id)
        logger.info("JavaBridge ROS 2 node '%s' created (domain %s)",
                    self._node_name, self._domain_id)

    def is_available(self) -> bool:
        try:
            return bool(self._bridge.isAlive()) and not self._shutdown
        except Exception:
            return False

    def create_publisher(self, topic: str, msg_type: str, qos: int = 10) -> None:
        with self._lock:
            if topic in self._publishers:
                return
            handle = self._bridge.createPublisher(self._node, str(topic),
                                                  ros_type_of(msg_type), int(qos))
            self._publishers[str(topic)] = {"handle": handle, "msg_type": msg_type}

    def create_subscriber(self, topic: str, msg_type: str,
                          callback: Callable, qos: int = 10) -> None:
        with self._lock:
            if topic in self._subscribers:
                return
            handle = self._bridge.createSubscriber(self._node, str(topic),
                                                   ros_type_of(msg_type), int(qos))
            self._subscribers[str(topic)] = {"handle": handle,
                                             "callback": callback,
                                             "msg_type": msg_type}

    def publish(self, topic: str, message: Any) -> None:
        topic = str(topic)
        now = time.monotonic()
        with self._lock:
            if self._shutdown:
                return
            entry = self._publishers.get(topic)
            if entry is None:
                logger.debug("Publish on unregistered topic '%s' ignored", topic)
                return
            # Emergency stop (zero Twist) bypasses rate limiting, mirroring
            # the stub/real rclpy transports.
            is_stop = (isinstance(message, Twist)
                       and message.linear.x == 0.0 and message.linear.y == 0.0
                       and message.angular.z == 0.0)
            last = self._last_publish_time.get(topic)
            if not is_stop and last is not None and (now - last) < self._min_interval:
                return
            self._last_publish_time[topic] = now
            if isinstance(message, Twist):
                message = sanitize_twist(message)
            elif isinstance(message, JointTrajectory):
                message = validate_joint_trajectory(message)
            payload = to_payload(message)
            ok = bool(self._bridge.publish(entry["handle"], json.dumps(payload)))
            if ok:
                self._published_log.append(
                    {"topic": topic, "timestamp": time.time(), "message": message})
                del self._published_log[:-512]  # bounded introspection log

    def get_subscription_count(self, topic: str) -> int:
        with self._lock:
            entry = self._publishers.get(str(topic))
        if entry is None:
            return 0
        try:
            return max(0, int(self._bridge.getSubscriptionCount(entry["handle"])))
        except Exception:
            return 0

    def spin_once(self, timeout: float = 0.1) -> None:
        """Drain inbound bridge messages and dispatch registered callbacks."""
        deadline = time.monotonic() + max(0.0, timeout)
        with self._lock:
            subs = list(self._subscribers.items())
            shutdown = self._shutdown
        if shutdown:
            return
        for topic, entry in subs:
            try:
                payloads = self._bridge.drainMessages(entry["handle"]) or []
            except Exception as exc:
                logger.warning("Drain failed on '%s': %s", topic, exc)
                continue
            for raw in payloads:
                try:
                    payload = json.loads(raw) if isinstance(raw, str) else raw
                except (TypeError, ValueError):
                    continue
                message = message_from_payload(payload)
                try:
                    entry["callback"](message)
                except Exception as exc:
                    logger.warning("Subscriber callback failed on '%s': %s", topic, exc)
            if time.monotonic() >= deadline:
                break

    def shutdown(self) -> None:
        with self._lock:
            if self._shutdown:
                return
            self._shutdown = True
            handles = [e["handle"] for e in self._publishers.values()]
            handles += [e["handle"] for e in self._subscribers.values()]
            self._publishers.clear()
            self._subscribers.clear()
        for handle in handles:
            try:
                self._bridge.closeHandle(handle)
            except Exception:
                pass
        try:
            self._bridge.destroyNode(self._node)
        except Exception:
            pass
        logger.info("JavaBridge ROS 2 node shut down")

    def get_published_messages(self, topic: Optional[str] = None) -> List[Dict[str, Any]]:
        with self._lock:
            items = list(self._published_log)
        if topic:
            return [item for item in items if item["topic"] == str(topic)]
        return items

    def check_connection(self) -> bool:
        return self.is_available()


# ---------------------------------------------------------------------------
# RosBridgeInterface (rosbridge_suite JSON over WebSocket - Termux path)
# ---------------------------------------------------------------------------
class RosBridgeInterface(BaseROS2Interface):
    """
    rosbridge v2.0 protocol client. Used on Termux (no app shell) and for
    interop testing. ``ws_factory`` is injectable for tests; by default the
    optional ``websocket-client`` package is used and lazily connected.
    """

    def __init__(self, host: str = "127.0.0.1", port: int = 9090,
                 rate_limit_hz: float = 30.0, ws_factory: Optional[Callable] = None):
        self._host = str(host)
        self._port = max(1, min(65535, int(port)))
        self._rate_limit_hz = max(0.1, float(rate_limit_hz))
        self._min_interval = 1.0 / self._rate_limit_hz
        self._ws_factory = ws_factory
        self._ws: Any = None
        self._publishers: Dict[str, str] = {}              # topic -> ros type
        self._subscribers: Dict[str, Callable] = {}        # topic -> callback
        self._advertised: set = set()
        self._published_log: List[Dict[str, Any]] = []
        self._last_publish_time: Dict[str, float] = {}
        self._lock = threading.RLock()
        self._shutdown = False

    def _ensure_ws(self) -> bool:
        if self._ws is not None:
            return True
        if self._shutdown:
            return False
        factory = self._ws_factory
        if factory is None:
            try:
                from websocket import create_connection  # optional dep
            except Exception:
                logger.warning("websocket-client not installed; "
                               "RosBridgeInterface unavailable")
                return False
            factory = create_connection
        try:
            self._ws = factory(f"ws://{self._host}:{self._port}",
                               timeout=max(0.5, self._min_interval * 2))
            logger.info("rosbridge connected to %s:%s", self._host, self._port)
            return True
        except Exception as exc:
            logger.warning("rosbridge connect failed: %s", exc)
            self._ws = None
            return False

    def is_available(self) -> bool:
        return not self._shutdown and self._ensure_ws()

    def _send(self, op: Dict[str, Any]) -> None:
        if not self._ensure_ws():
            return
        try:
            self._ws.send(json.dumps(op))
        except Exception as exc:
            logger.warning("rosbridge send failed: %s", exc)
            try:
                self._ws.close()
            except Exception:
                pass
            self._ws = None

    def create_publisher(self, topic: str, msg_type: str, qos: int = 10) -> None:
        with self._lock:
            self._publishers[str(topic)] = ros_type_of(msg_type)

    def create_subscriber(self, topic: str, msg_type: str,
                          callback: Callable, qos: int = 10) -> None:
        topic = str(topic)
        with self._lock:
            self._subscribers[topic] = callback
        self._send({"op": "subscribe", "topic": topic,
                    "type": ros_type_of(msg_type)})

    def publish(self, topic: str, message: Any) -> None:
        topic = str(topic)
        now = time.monotonic()
        with self._lock:
            ros_type = self._publishers.get(topic)
            if ros_type is None or self._shutdown:
                return
            is_stop = (isinstance(message, Twist)
                       and message.linear.x == 0.0 and message.linear.y == 0.0
                       and message.angular.z == 0.0)
            last = self._last_publish_time.get(topic)
            if not is_stop and last is not None and (now - last) < self._min_interval:
                return
            self._last_publish_time[topic] = now
            if isinstance(message, Twist):
                message = sanitize_twist(message)
            elif isinstance(message, JointTrajectory):
                message = validate_joint_trajectory(message)
            if topic not in self._advertised:
                self._advertised.add(topic)
                self._send({"op": "advertise", "topic": topic, "type": ros_type})
            self._send({"op": "publish", "topic": topic,
                        "msg": to_payload(message)})
            self._published_log.append(
                {"topic": topic, "timestamp": time.time(), "message": message})
            del self._published_log[:-512]

    def get_subscription_count(self, topic: str) -> int:
        # rosbridge does not expose graph counts; subscribers imply delivery.
        return 1 if str(topic) in self._subscribers else 0

    def spin_once(self, timeout: float = 0.1) -> None:
        with self._lock:
            ws = self._ws
            callbacks = dict(self._subscribers)
            shutdown = self._shutdown
        if shutdown or ws is None:
            return
        try:
            ws.settimeout(max(0.01, float(timeout)))
            raw = ws.recv()
        except Exception:
            return  # timeout or transient socket error - not fatal
        try:
            packet = json.loads(raw) if isinstance(raw, (str, bytes)) else {}
        except (TypeError, ValueError):
            return
        if not isinstance(packet, dict):
            return  # valid JSON but not an object (e.g. a bare string)
        if packet.get("op") != "publish":
            return
        callback = callbacks.get(str(packet.get("topic", "")))
        if callback is not None:
            try:
                callback(message_from_payload(packet.get("msg")))
            except Exception as exc:
                logger.warning("rosbridge callback failed: %s", exc)

    def shutdown(self) -> None:
        with self._lock:
            if self._shutdown:
                return
            self._shutdown = True
            for topic in list(self._subscribers):
                self._send({"op": "unsubscribe", "topic": topic})
            self._subscribers.clear()
            ws, self._ws = self._ws, None
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass

    def get_published_messages(self, topic: Optional[str] = None) -> List[Dict[str, Any]]:
        with self._lock:
            items = list(self._published_log)
        if topic:
            return [item for item in items if item["topic"] == str(topic)]
        return items

    def check_connection(self) -> bool:
        return self._ws is not None and not self._shutdown




