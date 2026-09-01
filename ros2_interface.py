"""
ShugoCore ROS 2 interface layer
==============================

Provides the target API for the agent to act on the real world through ROS 2.
The agent publishes high-level intent messages (velocity commands, joint
trajectories, gripper commands) and receives sensor feedback.

All ROS 2 dependencies (``rclpy``, ``geometry_msgs``, etc.) are optional.
Without them, :class:`ROS2Interface` falls back to :class:`StubROS2Interface`,
which records all published messages in memory and returns canned sensor data.
The rest of ShugoCore runs unaffected.

Message types are abstracted so the handler doesn't import ROS 2 types directly.
"""

import logging
import math
import threading
import time
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Abstract message types (no ROS 2 dependency required)
# ---------------------------------------------------------------------------

class Vector3:
    """Abstract 3D vector (mirrors geometry_msgs/Vector3)."""
    def __init__(self, x: float = 0.0, y: float = 0.0, z: float = 0.0):
        self.x = float(x)
        self.y = float(y)
        self.z = float(z)

    def to_dict(self) -> Dict[str, float]:
        return {"x": self.x, "y": self.y, "z": self.z}

    def __repr__(self) -> str:
        return f"Vector3(x={self.x:.3f}, y={self.y:.3f}, z={self.z:.3f})"


class Twist:
    """
    Abstract velocity command (mirrors geometry_msgs/Twist).
    For /cmd_vel — mobile base velocity.
    """
    def __init__(self, linear: Optional[Vector3] = None, angular: Optional[Vector3] = None):
        self.linear = linear if linear is not None else Vector3()
        self.angular = angular if angular is not None else Vector3()

    def to_dict(self) -> Dict[str, Any]:
        return {"linear": self.linear.to_dict(), "angular": self.angular.to_dict()}

    def __repr__(self) -> str:
        return f"Twist(linear={self.linear}, angular={self.angular})"


class JointTrajectoryPoint:
    """Single point in a joint trajectory (mirrors trajectory_msgs/JointTrajectoryPoint)."""
    def __init__(self, positions: Optional[List[float]] = None,
                 velocities: Optional[List[float]] = None,
                 accelerations: Optional[List[float]] = None,
                 time_from_start: float = 0.0):
        self.positions = list(positions or [])
        self.velocities = list(velocities or [])
        self.accelerations = list(accelerations or [])
        self.time_from_start = float(time_from_start)


class JointTrajectory:
    """
    Abstract joint trajectory (mirrors trajectory_msgs/JointTrajectory).
    For /arm_controller/follow_joint_trajectory.
    """
    def __init__(self, joint_names: Optional[List[str]] = None,
                 points: Optional[List[JointTrajectoryPoint]] = None):
        self.joint_names = list(joint_names or [])
        self.points = list(points or [])


class JointState:
    """Abstract joint state (mirrors sensor_msgs/JointState)."""
    def __init__(self, name: Optional[List[str]] = None,
                 position: Optional[List[float]] = None,
                 velocity: Optional[List[float]] = None,
                 effort: Optional[List[float]] = None):
        self.name = list(name or [])
        self.position = list(position or [])
        self.velocity = list(velocity or [])
        self.effort = list(effort or [])


class LaserScan:
    """Abstract laser scan (mirrors sensor_msgs/LaserScan)."""
    def __init__(self, ranges: Optional[List[float]] = None,
                 angle_min: float = 0.0, angle_max: float = 2 * math.pi,
                 range_min: float = 0.1, range_max: float = 10.0):
        self.ranges = list(ranges or [])
        self.angle_min = float(angle_min)
        self.angle_max = float(angle_max)
        self.range_min = float(range_min)
        self.range_max = float(range_max)


class Pose:
    """Abstract pose (mirrors geometry_msgs/Pose)."""
    def __init__(self, position: Optional[Vector3] = None,
                 orientation: Optional[Vector3] = None):
        self.position = position if position is not None else Vector3()
        self.orientation = orientation if orientation is not None else Vector3()


# ---------------------------------------------------------------------------
# Base interface
# ---------------------------------------------------------------------------

class BaseROS2Interface:
    """Interface for all ROS 2 abstractions (real and stub)."""

    def is_available(self) -> bool:
        raise NotImplementedError

    def create_publisher(self, topic: str, msg_type: str, qos: int = 10) -> None:
        raise NotImplementedError

    def create_subscriber(self, topic: str, msg_type: str,
                          callback: Callable, qos: int = 10) -> None:
        raise NotImplementedError

    def publish(self, topic: str, message: Any) -> None:
        raise NotImplementedError

    def get_subscription_count(self, topic: str) -> int:
        raise NotImplementedError

    def spin_once(self, timeout: float = 0.1) -> None:
        raise NotImplementedError

    def shutdown(self) -> None:
        raise NotImplementedError

    def get_published_messages(self, topic: Optional[str] = None) -> List[Dict[str, Any]]:
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Stub implementation (no ROS 2 dependency)
# ---------------------------------------------------------------------------

class StubROS2Interface(BaseROS2Interface):
    """
    Deterministic fallback when rclpy is not installed.
    Records all published messages in memory and returns canned sensor data.
    """

    def __init__(self, rate_limit_hz: float = 10.0):
        self._rate_limit_hz = max(0.1, float(rate_limit_hz))
        self._min_interval = 1.0 / self._rate_limit_hz
        self._publishers: Dict[str, Dict[str, Any]] = {}
        self._subscribers: Dict[str, Callable] = {}
        self._published_log: List[Dict[str, Any]] = []
        self._last_publish_time: Dict[str, float] = {}
        self._lock = threading.Lock()
        self._canned_joint_state = JointState(
            name=["joint_1", "joint_2", "joint_3", "joint_4", "joint_5", "joint_6"],
            position=[0.0] * 6, velocity=[0.0] * 6, effort=[0.0] * 6,
        )
        self._canned_laser = LaserScan(
            ranges=[5.0] * 360, angle_min=0.0, angle_max=2 * math.pi,
            range_min=0.1, range_max=10.0,
        )

    def is_available(self) -> bool:
        return False

    def create_publisher(self, topic: str, msg_type: str, qos: int = 10) -> None:
        with self._lock:
            self._publishers[topic] = {"msg_type": msg_type, "qos": qos}
        logger.debug(f"[stub] Created publisher on '{topic}' ({msg_type})")

    def create_subscriber(self, topic: str, msg_type: str,
                          callback: Callable, qos: int = 10) -> None:
        with self._lock:
            self._subscribers[topic] = callback
        logger.debug(f"[stub] Created subscriber on '{topic}' ({msg_type})")

    def publish(self, topic: str, message: Any) -> None:
        now = time.monotonic()
        with self._lock:
            last = self._last_publish_time.get(topic)
            if last is not None and (now - last) < self._min_interval:
                logger.debug(f"[stub] Rate-limited publish on '{topic}'")
                return
            self._last_publish_time[topic] = now
            if isinstance(message, Twist):
                message = self._validate_twist(message)
            elif isinstance(message, JointTrajectory):
                message = self._validate_trajectory(message)
            self._published_log.append({"topic": topic, "timestamp": time.time(), "message": message})
        logger.debug(f"[stub] Published on '{topic}': {message}")

    def _validate_twist(self, twist: Twist) -> Twist:
        def _clean(v: float) -> float:
            if math.isnan(v) or math.isinf(v):
                return 0.0
            return max(-100.0, min(100.0, v))
        twist.linear = Vector3(_clean(twist.linear.x), _clean(twist.linear.y), _clean(twist.linear.z))
        twist.angular = Vector3(_clean(twist.angular.x), _clean(twist.angular.y), _clean(twist.angular.z))
        return twist

    def _validate_trajectory(self, traj: JointTrajectory) -> JointTrajectory:
        for point in traj.points:
            point.positions = [0.0 if math.isnan(v) or math.isinf(v) else v for v in point.positions]
            point.velocities = [0.0 if math.isnan(v) or math.isinf(v) else v for v in point.velocities]
            point.accelerations = [0.0 if math.isnan(v) or math.isinf(v) else v for v in point.accelerations]
        return traj

    def get_subscription_count(self, topic: str) -> int:
        with self._lock:
            return 1 if topic in self._subscribers else 0

    def spin_once(self, timeout: float = 0.1) -> None:
        pass

    def shutdown(self) -> None:
        with self._lock:
            self._publishers.clear()
            self._subscribers.clear()
        logger.info("[stub] ROS 2 interface shut down")

    def get_published_messages(self, topic: Optional[str] = None) -> List[Dict[str, Any]]:
        with self._lock:
            if topic is None:
                return list(self._published_log)
            return [e for e in self._published_log if e["topic"] == topic]

    def get_canned_joint_state(self) -> JointState:
        return self._canned_joint_state

    def get_canned_laser_scan(self) -> LaserScan:
        return self._canned_laser


# ---------------------------------------------------------------------------
# Real ROS 2 implementation
# ---------------------------------------------------------------------------

class ROS2Interface(BaseROS2Interface):
    """Real ROS 2 interface using rclpy. Only instantiated when rclpy is available."""

    def __init__(self, node_name: str = "shugocore_agent", rate_limit_hz: float = 10.0):
        try:
            import rclpy
            from rclpy.node import Node
        except ImportError:
            raise RuntimeError("rclpy is not installed")
        self._rclpy = rclpy
        rclpy.init()
        self._node = rclpy.create_node(node_name)
        self._rate_limit_hz = max(0.1, float(rate_limit_hz))
        self._min_interval = 1.0 / self._rate_limit_hz
        self._publishers: Dict[str, Any] = {}
        self._subscribers: Dict[str, Any] = {}
        self._last_publish_time: Dict[str, float] = {}
        self._lock = threading.Lock()
        logger.info(f"ROS 2 node '{node_name}' initialized")

    def is_available(self) -> bool:
        return True

    def create_publisher(self, topic: str, msg_type: str, qos: int = 10) -> None:
        from geometry_msgs.msg import Twist as RosTwist
        from trajectory_msgs.msg import JointTrajectory as RosJointTrajectory
        from sensor_msgs.msg import JointState as RosJointState
        from sensor_msgs.msg import LaserScan as RosLaserScan
        type_map = {"Twist": RosTwist, "JointTrajectory": RosJointTrajectory,
                    "JointState": RosJointState, "LaserScan": RosLaserScan}
        ros_type = type_map.get(msg_type)
        if ros_type is None:
            raise ValueError(f"Unknown message type: {msg_type}")
        self._publishers[topic] = self._node.create_publisher(ros_type, topic, qos)
        logger.debug(f"Created publisher on '{topic}' ({msg_type})")

    def create_subscriber(self, topic: str, msg_type: str,
                          callback: Callable, qos: int = 10) -> None:
        from geometry_msgs.msg import Twist as RosTwist
        from trajectory_msgs.msg import JointTrajectory as RosJointTrajectory
        from sensor_msgs.msg import JointState as RosJointState
        from sensor_msgs.msg import LaserScan as RosLaserScan
        type_map = {"Twist": RosTwist, "JointTrajectory": RosJointTrajectory,
                    "JointState": RosJointState, "LaserScan": RosLaserScan}
        ros_type = type_map.get(msg_type)
        if ros_type is None:
            raise ValueError(f"Unknown message type: {msg_type}")
        self._subscribers[topic] = self._node.create_subscription(ros_type, topic, callback, qos)
        logger.debug(f"Created subscriber on '{topic}' ({msg_type})")

    def publish(self, topic: str, message: Any) -> None:
        now = time.monotonic()
        with self._lock:
            last = self._last_publish_time.get(topic)
            if last is not None and (now - last) < self._min_interval:
                logger.debug(f"Rate-limited publish on '{topic}'")
                return
            self._last_publish_time[topic] = now
            publisher = self._publishers.get(topic)
            if publisher is None:
                raise RuntimeError(f"No publisher registered for topic '{topic}'")
            publisher.publish(message)
        logger.debug(f"Published on '{topic}': {message}")

    def get_subscription_count(self, topic: str) -> int:
        publisher = self._publishers.get(topic)
        if publisher is None:
            return 0
        return self._rclpy.publisher_get_subscription_count(publisher)

    def spin_once(self, timeout: float = 0.1) -> None:
        self._rclpy.spin_once(self._node, timeout_sec=timeout)

    def shutdown(self) -> None:
        self._node.destroy_node()
        self._rclpy.shutdown()
        logger.info("ROS 2 node shut down")

    def get_published_messages(self, topic: Optional[str] = None) -> List[Dict[str, Any]]:
        return []


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def create_ros2_interface(node_name: str = "shugocore_agent",
                          rate_limit_hz: float = 10.0) -> BaseROS2Interface:
    """
    Create the best available ROS 2 interface.
    Returns ROS2Interface if rclpy is available, otherwise StubROS2Interface.
    """
    try:
        import rclpy  # noqa: F401
        return ROS2Interface(node_name=node_name, rate_limit_hz=rate_limit_hz)
    except (ImportError, RuntimeError):
        logger.info("rclpy not available; using stub ROS 2 interface")
        return StubROS2Interface(rate_limit_hz=rate_limit_hz)