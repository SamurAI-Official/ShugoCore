"""
ShugoCore MoveIt 2 motion planning layer.

The "how" of physical motion. Handles inverse kinematics, trajectory planning,
and obstacle avoidance. The agent says "move here"; MoveIt computes the joint
angles without breaking joints.

All MoveIt 2 dependencies (moveit_msgs, moveit_commander) are optional.
Without them, falls back to StubMoveItPlanner.
"""

import logging
import math
import threading
from typing import Any, Dict, List, Optional, Tuple

from ros2_interface import JointState, JointTrajectory, JointTrajectoryPoint, Pose, Vector3

logger = logging.getLogger(__name__)


class BaseMoveItPlanner:
    """Interface for all MoveIt 2 abstractions (real and stub)."""
    def is_available(self) -> bool:
        raise NotImplementedError
    def compute_ik(self, pose: Pose, frame_id: str = "base_link",
                   joint_state: Optional[JointState] = None) -> Optional[JointState]:
        raise NotImplementedError
    def plan_to_pose(self, pose: Pose, frame_id: str = "base_link") -> Optional[JointTrajectory]:
        raise NotImplementedError
    def plan_to_joint_target(self, joint_targets: Dict[str, float]) -> Optional[JointTrajectory]:
        raise NotImplementedError
    def plan_cartesian_path(self, waypoints: List[Pose], frame_id: str = "base_link") -> Optional[JointTrajectory]:
        raise NotImplementedError
    def check_collision(self, trajectory: JointTrajectory) -> bool:
        raise NotImplementedError
    def get_joint_limits(self) -> Dict[str, Tuple[float, float]]:
        raise NotImplementedError
    def validate_trajectory(self, trajectory: JointTrajectory) -> Tuple[bool, str]:
        raise NotImplementedError
    def cancel_all_goals(self) -> None:
        raise NotImplementedError

    def get_execution_status(self) -> Dict[str, Any]:
        """Return trajectory execution progress and current joint positions."""
        raise NotImplementedError

    def shutdown(self) -> None:
        raise NotImplementedError


class StubMoveItPlanner(BaseMoveItPlanner):
    """Deterministic fallback when MoveIt 2 is not installed."""

    DEFAULT_JOINT_NAMES = ["joint_1", "joint_2", "joint_3", "joint_4", "joint_5", "joint_6"]
    DEFAULT_JOINT_LIMITS = {f"joint_{i}": (-math.pi, math.pi) for i in range(1, 7)}

    def __init__(self, joint_names=None, joint_limits=None,
                 velocity_limit=1.0, acceleration_limit=0.5,
                 collision_objects=None):
        self._joint_names = list(joint_names or self.DEFAULT_JOINT_NAMES)
        self._joint_limits = dict(joint_limits or self.DEFAULT_JOINT_LIMITS)
        self._velocity_limit = max(0.01, float(velocity_limit))
        self._acceleration_limit = max(0.01, float(acceleration_limit))
        self._collision_objects: List[Dict[str, Any]] = list(collision_objects or [])
        self._lock = threading.Lock()
        logger.info(f"[stub] MoveIt planner initialized ({len(self._joint_names)} joints)")

    def is_available(self) -> bool:
        return False

    def compute_ik(self, pose: Pose, frame_id: str = "base_link",
                   joint_state: Optional[JointState] = None) -> Optional[JointState]:
        if pose is None:
            return None
        positions = []
        for i, name in enumerate(self._joint_names):
            if i == 0:
                pos = math.sin(pose.position.x) * 0.5
            elif i == 1:
                pos = math.sin(pose.position.y) * 0.5
            elif i == 2:
                pos = math.sin(pose.position.z) * 0.3
            else:
                pos = 0.0
            lo, hi = self._joint_limits.get(name, (-math.pi, math.pi))
            positions.append(max(lo, min(hi, pos)))
        return JointState(name=list(self._joint_names), position=positions)

    def plan_to_pose(self, pose: Pose, frame_id: str = "base_link") -> Optional[JointTrajectory]:
        target = self.compute_ik(pose, frame_id)
        if target is None:
            return None
        return self._create_trajectory(target.position)

    def plan_to_joint_target(self, joint_targets: Dict[str, float]) -> Optional[JointTrajectory]:
        if not joint_targets:
            return None
        positions = []
        for name in self._joint_names:
            if name in joint_targets:
                pos = float(joint_targets[name])
                lo, hi = self._joint_limits.get(name, (-math.pi, math.pi))
                positions.append(max(lo, min(hi, pos)))
            else:
                positions.append(0.0)
        return self._create_trajectory(positions)

    def plan_cartesian_path(self, waypoints: List[Pose], frame_id: str = "base_link") -> Optional[JointTrajectory]:
        if not waypoints:
            return None
        trajectory = JointTrajectory(joint_names=list(self._joint_names))
        for i, wp in enumerate(waypoints):
            ik = self.compute_ik(wp, frame_id)
            if ik is None:
                continue
            point = JointTrajectoryPoint(positions=list(ik.position), time_from_start=float(i + 1))
            trajectory.points.append(point)
        if not trajectory.points:
            return None
        return trajectory

    def check_collision(self, trajectory: JointTrajectory) -> bool:
        with self._lock:
            if not self._collision_objects:
                return False
            for point in trajectory.points:
                for obj in self._collision_objects:
                    pos = obj.get("position", {})
                    size = obj.get("size", {})
                    if (abs(point.positions[0] - pos.get("x", 0)) < size.get("x", 0.1) and
                        abs(point.positions[1] - pos.get("y", 0)) < size.get("y", 0.1)):
                        return True
        return False

    def get_joint_limits(self) -> Dict[str, Tuple[float, float]]:
        with self._lock:
            return dict(self._joint_limits)

    def validate_trajectory(self, trajectory: JointTrajectory) -> Tuple[bool, str]:
        if not trajectory.points:
            return False, "empty trajectory"
        for point in trajectory.points:
            for i, name in enumerate(trajectory.joint_names):
                if i >= len(point.positions):
                    continue
                lo, hi = self._joint_limits.get(name, (-math.pi, math.pi))
                if point.positions[i] < lo or point.positions[i] > hi:
                    return False, f"joint '{name}' outside limits"
        for i in range(1, len(trajectory.points)):
            dt = trajectory.points[i].time_from_start - trajectory.points[i-1].time_from_start
            if dt <= 0:
                continue
            for j in range(len(trajectory.joint_names)):
                if j >= len(trajectory.points[i].positions) or j >= len(trajectory.points[i-1].positions):
                    continue
                vel = abs(trajectory.points[i].positions[j] - trajectory.points[i-1].positions[j]) / dt
                if vel > self._velocity_limit:
                    return False, f"joint '{trajectory.joint_names[j]}' velocity exceeds limit"
        return True, ""

    def add_collision_object(self, name: str, position: Dict[str, float], size: Dict[str, float]) -> None:
        with self._lock:
            self._collision_objects.append({"name": name, "position": position, "size": size})

    def remove_collision_object(self, name: str) -> bool:
        with self._lock:
            for i, obj in enumerate(self._collision_objects):
                if obj.get("name") == name:
                    self._collision_objects.pop(i)
                    return True
        return False

    def _create_trajectory(self, positions: List[float]) -> JointTrajectory:
        point = JointTrajectoryPoint(positions=list(positions), time_from_start=1.0)
        return JointTrajectory(joint_names=list(self._joint_names), points=[point])

    def cancel_all_goals(self) -> None:
        logger.info("[stub] MoveIt: cancelled all goals")

    def get_execution_status(self) -> Dict[str, Any]:
        """Stub: return a simulated execution status."""
        return {
            "state": "idle",
            "progress": 1.0,
            "current_positions": [0.0] * len(self._joint_names),
            "active_goal": None,
        }

    def shutdown(self) -> None:
        logger.info("[stub] MoveIt planner shut down")


# ---------------------------------------------------------------------------
# Real MoveIt 2 implementation
# ---------------------------------------------------------------------------

class MoveItPlanner(BaseMoveItPlanner):
    """Real MoveIt 2 planner. Only instantiated when moveit is available."""

    def __init__(self, joint_names=None, joint_limits=None,
                 velocity_limit=1.0, acceleration_limit=0.5,
                 robot_description="robot_description",
                 planning_group="arm"):
        try:
            from moveit_commander import MoveGroupCommander, RobotCommander, PlanningSceneInterface
        except ImportError:
            raise RuntimeError("moveit_commander is not installed")
        self._robot = RobotCommander(robot_description=robot_description)
        self._group = MoveGroupCommander(planning_group)
        self._scene = PlanningSceneInterface()
        self._joint_names = list(joint_names or self._group.get_active_joints())
        self._joint_limits = dict(joint_limits or {})
        self._velocity_limit = max(0.01, float(velocity_limit))
        self._acceleration_limit = max(0.01, float(acceleration_limit))
        self._lock = threading.Lock()
        logger.info(f"MoveIt 2 planner initialized (group: {planning_group})")

    def is_available(self) -> bool:
        return True

    def compute_ik(self, pose: Pose, frame_id: str = "base_link",
                   joint_state: Optional[JointState] = None) -> Optional[JointState]:
        from geometry_msgs.msg import PoseStamped
        pose_stamped = PoseStamped()
        pose_stamped.header.frame_id = frame_id
        pose_stamped.pose.position.x = pose.position.x
        pose_stamped.pose.position.y = pose.position.y
        pose_stamped.pose.position.z = pose.position.z
        pose_stamped.pose.orientation.w = 1.0
        self._group.set_pose_target(pose_stamped)
        result = self._group.get_current_joint_values()
        if result is None:
            return None
        return JointState(name=list(self._joint_names), position=list(result))

    def plan_to_pose(self, pose: Pose, frame_id: str = "base_link") -> Optional[JointTrajectory]:
        from geometry_msgs.msg import PoseStamped
        pose_stamped = PoseStamped()
        pose_stamped.header.frame_id = frame_id
        pose_stamped.pose.position.x = pose.position.x
        pose_stamped.pose.position.y = pose.position.y
        pose_stamped.pose.position.z = pose.position.z
        pose_stamped.pose.orientation.w = 1.0
        self._group.set_pose_target(pose_stamped)
        plan = self._group.plan()
        if not plan or not plan[1]:
            return None
        return self._convert_plan(plan)

    def plan_to_joint_target(self, joint_targets: Dict[str, float]) -> Optional[JointTrajectory]:
        target = [float(joint_targets.get(name, 0.0)) for name in self._joint_names]
        self._group.set_joint_value_target(target)
        plan = self._group.plan()
        if not plan or not plan[1]:
            return None
        return self._convert_plan(plan)

    def plan_cartesian_path(self, waypoints: List[Pose], frame_id: str = "base_link") -> Optional[JointTrajectory]:
        from geometry_msgs.msg import Pose as RosPose
        ros_waypoints = []
        for wp in waypoints:
            ros_pose = RosPose()
            ros_pose.position.x = wp.position.x
            ros_pose.position.y = wp.position.y
            ros_pose.position.z = wp.position.z
            ros_pose.orientation.w = 1.0
            ros_waypoints.append(ros_pose)
        self._group.set_pose_reference_frame(frame_id)
        plan, fraction = self._group.compute_cartesian_path(ros_waypoints, 0.01, 0.0)
        if fraction < 1.0:
            logger.warning(f"Cartesian path only {fraction:.1%} achievable")
        return self._convert_plan(plan) if plan else None

    def check_collision(self, trajectory: JointTrajectory) -> bool:
        return False

    def get_joint_limits(self) -> Dict[str, Tuple[float, float]]:
        return dict(self._joint_limits)

    def validate_trajectory(self, trajectory: JointTrajectory) -> Tuple[bool, str]:
        if not trajectory.points:
            return False, "empty trajectory"
        return True, ""

    def cancel_all_goals(self) -> None:
        self._group.stop()
        logger.info("MoveIt 2: cancelled all goals")

    def get_execution_status(self) -> Dict[str, Any]:
        """Return trajectory execution progress and current joint positions."""
        try:
            current = self._group.get_current_joint_values()
            return {
                "state": "active" if self._group.get_active_joints() else "idle",
                "progress": 1.0,  # MoveIt doesn't expose progress directly
                "current_positions": list(current) if current else [],
                "active_goal": None,
            }
        except Exception:
            return {"state": "error", "progress": 0.0, "current_positions": [], "active_goal": None}

    def shutdown(self) -> None:
        logger.info("MoveIt 2 planner shut down")

    def _convert_plan(self, plan) -> JointTrajectory:
        """Convert a MoveIt plan to our abstract JointTrajectory."""
        trajectory = JointTrajectory(joint_names=list(self._joint_names))
        for point in plan.joint_trajectory.points:
            trajectory.points.append(JointTrajectoryPoint(
                positions=list(point.positions),
                velocities=list(point.velocities) if point.velocities else [],
                accelerations=list(point.accelerations) if point.accelerations else [],
                time_from_start=point.time_from_start.to_sec(),
            ))
        return trajectory


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def create_moveit_planner(joint_names=None, joint_limits=None,
                          velocity_limit=1.0, acceleration_limit=0.5,
                          **kwargs) -> BaseMoveItPlanner:
    """Create the best available MoveIt 2 planner."""
    try:
        import moveit_commander  # noqa: F401
        import moveit_msgs  # noqa: F401
        return MoveItPlanner(joint_names=joint_names, joint_limits=joint_limits,
                             velocity_limit=velocity_limit, acceleration_limit=acceleration_limit,
                             **kwargs)
    except (ImportError, RuntimeError, ModuleNotFoundError):
        logger.info("moveit not available; using stub MoveIt planner")
        return StubMoveItPlanner(joint_names=joint_names, joint_limits=joint_limits,
                                 velocity_limit=velocity_limit, acceleration_limit=acceleration_limit)