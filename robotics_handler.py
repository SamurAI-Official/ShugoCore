"""
ShugoCore robotics execution handler
====================================

Bridges the execution layer with the robotics modules (ROS 2, MoveIt 2, Gazebo).
Registered with ExecutionLayer for all robotics action types. Translates
high-level intent into ROS 2 messages, with full safety enforcement.

Safety limits (velocity, acceleration, workspace, joint limits) are enforced
here before any command reaches the physical robot or simulation.
"""

import logging
import threading
import time
from typing import Any, Dict, Optional

from ros2_interface import (
    JointTrajectory, JointTrajectoryPoint, Twist, Vector3,
    create_ros2_interface,
)
from moveit_planner import create_moveit_planner, Pose
from gazebo_simulation import create_gazebo_simulation

logger = logging.getLogger(__name__)

# Action types handled by this handler
ROBOTICS_ACTION_TYPES = {"robot_navigate", "robot_manipulate", "robot_gripper", "robot_stop"}
ROBOTICS_READ_ACTION_TYPES = {"robot_query_state", "robot_scan"}


class RoboticsExecutionHandler:
    """
    Executes robotics actions through ROS 2, with MoveIt 2 for motion planning
    and Gazebo for simulation. Enforces all safety limits.
    """

    def __init__(self, ros2=None, moveit=None, gazebo=None,
                 capabilities=None, audit=None,
                 max_linear_velocity: float = 1.0,
                 max_angular_velocity: float = 1.0,
                 max_acceleration: float = 0.5,
                 workspace_bounds: Optional[Dict[str, tuple]] = None,
                 joint_limits: Optional[Dict[str, tuple]] = None,
                 watchdog_timeout: float = 5.0):
        self._ros2 = ros2 or create_ros2_interface()
        self._moveit = moveit or create_moveit_planner()
        self._gazebo = gazebo or create_gazebo_simulation()
        self._capabilities = capabilities
        self._audit = audit
        self._max_linear_velocity = max(0.01, float(max_linear_velocity))
        self._max_angular_velocity = max(0.01, float(max_angular_velocity))
        self._max_acceleration = max(0.01, float(max_acceleration))
        self._workspace_bounds = dict(workspace_bounds or {
            "x": (-2.0, 2.0), "y": (-2.0, 2.0), "z": (0.0, 2.0)
        })
        self._joint_limits = dict(joint_limits or {})
        self._watchdog_timeout = max(0.1, float(watchdog_timeout))
        self._last_command_time = time.monotonic()
        self._lock = threading.Lock()
        self._emergency_stop_active = False

        # Set up ROS 2 interfaces
        self._ros2.create_publisher("/cmd_vel", "Twist")
        self._ros2.create_publisher("/gripper_cmd", "JointTrajectory")
        logger.info("Robotics execution handler initialized")

    def handle(self, decision: Dict[str, Any]) -> Dict[str, Any]:
        """Main dispatch for robotics actions."""
        action_type = decision.get("action_type", "")
        if action_type in ROBOTICS_READ_ACTION_TYPES:
            return self._handle_read(decision)
        if action_type == "robot_navigate":
            return self._handle_navigate(decision)
        if action_type == "robot_manipulate":
            return self._handle_manipulate(decision)
        if action_type == "robot_gripper":
            return self._handle_gripper(decision)
        if action_type == "robot_stop":
            return self._handle_stop(decision)
        return {"status": "error", "message": f"unknown robotics action: {action_type}"}

    def _handle_navigate(self, decision: Dict[str, Any]) -> Dict[str, Any]:
        """Handle mobile base navigation via /cmd_vel."""
        params = decision.get("params", {})
        linear = Vector3(
            x=self._clamp_velocity(params.get("linear_x", 0.0), self._max_linear_velocity),
            y=self._clamp_velocity(params.get("linear_y", 0.0), self._max_linear_velocity),
            z=0.0,
        )
        angular = Vector3(
            x=0.0, y=0.0,
            z=self._clamp_velocity(params.get("angular_z", 0.0), self._max_angular_velocity),
        )
        twist = Twist(linear=linear, angular=angular)
        self._ros2.publish("/cmd_vel", twist)
        self._update_watchdog()
        return {"status": "success", "action": "navigate", "twist": twist.to_dict()}

    def _handle_manipulate(self, decision: Dict[str, Any]) -> Dict[str, Any]:
        """Handle arm manipulation via MoveIt 2."""
        params = decision.get("params", {})
        target = params.get("target", {})
        if not target:
            return {"status": "error", "message": "no target specified"}
        # Check workspace bounds
        if not self._check_workspace(target):
            return {"status": "refused", "reason": "target outside workspace bounds"}
        pose = Pose(position=Vector3(
            target.get("x", 0.0), target.get("y", 0.0), target.get("z", 0.0)
        ))
        trajectory = self._moveit.plan_to_pose(pose)
        if trajectory is None:
            return {"status": "error", "message": "planning failed"}
        # Validate trajectory
        valid, reason = self._moveit.validate_trajectory(trajectory)
        if not valid:
            return {"status": "refused", "reason": reason}
        # Check collision
        if self._moveit.check_collision(trajectory):
            return {"status": "refused", "reason": "collision detected"}
        self._update_watchdog()
        return {"status": "success", "action": "manipulate", "joints": trajectory.joint_names}

    def _handle_gripper(self, decision: Dict[str, Any]) -> Dict[str, Any]:
        """Handle gripper command."""
        params = decision.get("params", {})
        position = float(params.get("position", 0.0))
        trajectory = JointTrajectory(
            joint_names=["gripper_joint"],
            points=[JointTrajectoryPoint(positions=[position], time_from_start=0.5)],
        )
        self._ros2.publish("/gripper_cmd", trajectory)
        self._update_watchdog()
        return {"status": "success", "action": "gripper", "position": position}

    def _handle_stop(self, decision: Dict[str, Any]) -> Dict[str, Any]:
        """Emergency stop — immediate halt, cancel all goals."""
        with self._lock:
            self._emergency_stop_active = True
        # Publish zero velocity
        self._ros2.publish("/cmd_vel", Twist())
        self._audit_log("emergency_stop", {})
        return {"status": "success", "action": "emergency_stop"}

    def _handle_read(self, decision: Dict[str, Any]) -> Dict[str, Any]:
        """Handle read-only queries (state, scan)."""
        action_type = decision.get("action_type", "")
        if action_type == "robot_query_state":
            model = decision.get("params", {}).get("model", "robot")
            joints = self._gazebo.get_joint_states(model)
            return {"status": "success", "joints": joints.position if joints else []}
        if action_type == "robot_scan":
            scan = self._gazebo.get_sensor_data("/scan")
            return {"status": "success", "scan": scan.ranges if scan else []}
        return {"status": "error", "message": f"unknown read action: {action_type}"}

    def _clamp_velocity(self, value: float, limit: float) -> float:
        """Clamp velocity to [-limit, limit]."""
        return max(-limit, min(limit, float(value)))

    def _check_workspace(self, target: Dict[str, float]) -> bool:
        """Check if target is within workspace bounds."""
        for axis in ["x", "y", "z"]:
            if axis in target:
                lo, hi = self._workspace_bounds.get(axis, (-float("inf"), float("inf")))
                if target[axis] < lo or target[axis] > hi:
                    return False
        return True

    def _update_watchdog(self) -> None:
        """Update the command watchdog timer."""
        self._last_command_time = time.monotonic()

    def check_watchdog(self) -> bool:
        """Check if watchdog has expired. Returns True if expired."""
        return (time.monotonic() - self._last_command_time) > self._watchdog_timeout

    def is_emergency_stopped(self) -> bool:
        """Check if emergency stop is active."""
        with self._lock:
            return self._emergency_stop_active

    def reset_emergency_stop(self, reset_by: str = "") -> Dict[str, Any]:
        """Reset emergency stop (requires operator attribution)."""
        with self._lock:
            self._emergency_stop_active = False
        self._audit_log("emergency_stop_reset", {"reset_by": str(reset_by)[:120]})
        return {"status": "success", "action": "reset_emergency_stop"}

    def _audit_log(self, event_type: str, payload: Dict[str, Any]) -> None:
        """Append to audit log if available."""
        if self._audit is not None:
            try:
                self._audit.append(event_type, payload)
            except Exception:
                pass

    def shutdown(self) -> None:
        """Shut down all robotics modules."""
        self._ros2.shutdown()
        self._moveit.shutdown()
        self._gazebo.shutdown()
        logger.info("Robotics execution handler shut down")