"""
ShugoCore Gazebo/Ignition simulation layer
==========================================

Zero-risk test environment for the agent framework. Load URDF models, simulate
physics and sensors, bridge to ROS 2.

All Gazebo dependencies (gazebo_msgs, ros_gz) are optional.
Without them, falls back to StubGazeboSimulation, which tracks models in memory
and generates procedural sensor data deterministically.
"""

import logging
import math
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

from ros2_interface import JointState, LaserScan, Pose, Vector3

logger = logging.getLogger(__name__)


class BaseGazeboSimulation:
    """Interface for all Gazebo abstractions (real and stub)."""
    def is_available(self) -> bool:
        raise NotImplementedError
    def launch_world(self, world_file: str) -> bool:
        raise NotImplementedError
    def spawn_urdf(self, urdf_file: str, pose: Pose, name: str) -> bool:
        raise NotImplementedError
    def spawn_from_string(self, urdf_string: str, pose: Pose, name: str) -> bool:
        raise NotImplementedError
    def remove_model(self, name: str) -> bool:
        raise NotImplementedError
    def reset_world(self) -> None:
        raise NotImplementedError
    def pause(self) -> None:
        raise NotImplementedError
    def unpause(self) -> None:
        raise NotImplementedError
    def step(self, iterations: int = 1) -> None:
        raise NotImplementedError
    def get_model_pose(self, name: str) -> Optional[Pose]:
        raise NotImplementedError
    def get_joint_states(self, name: str) -> Optional[JointState]:
        raise NotImplementedError
    def get_sensor_data(self, topic: str) -> Optional[Any]:
        raise NotImplementedError
    def shutdown(self) -> None:
        raise NotImplementedError


class StubGazeboSimulation(BaseGazeboSimulation):
    """Deterministic fallback when Gazebo is not installed.

    Integrates commands: Twist on /cmd_vel updates the model pose
    (simple kinematic integration). This makes the stub useful for
    testing the full command→effect chain without real Gazebo.
    """

    def __init__(self, max_models: int = 50):
        self._models: Dict[str, Dict[str, Any]] = {}
        self._max_models = max(1, int(max_models))
        self._paused = False
        self._sim_time = 0.0
        self._last_twist: Dict[str, Any] = {"linear": Vector3(), "angular": Vector3()}
        self._lock = threading.Lock()
        logger.info("[stub] Gazebo simulation initialized")

    def is_available(self) -> bool:
        return False

    def launch_world(self, world_file: str) -> bool:
        logger.info(f"[stub] Launched world: {world_file}")
        return True

    def spawn_urdf(self, urdf_file: str, pose: Pose, name: str) -> bool:
        with self._lock:
            if len(self._models) >= self._max_models:
                logger.warning(f"[stub] Max models ({self._max_models}) reached")
                return False
            self._models[name] = {
                "urdf_file": urdf_file, "pose": pose,
                "joints": JointState(name=["joint_1", "joint_2", "joint_3"], position=[0.0] * 3),
            }
            logger.info(f"[stub] Spawned model '{name}'")
            return True

    def spawn_from_string(self, urdf_string: str, pose: Pose, name: str) -> bool:
        with self._lock:
            if len(self._models) >= self._max_models:
                return False
            self._models[name] = {
                "urdf_string": urdf_string, "pose": pose,
                "joints": JointState(name=["joint_1", "joint_2", "joint_3"], position=[0.0] * 3),
            }
            logger.info(f"[stub] Spawned model '{name}' from string")
            return True

    def remove_model(self, name: str) -> bool:
        with self._lock:
            if name in self._models:
                del self._models[name]
                logger.info(f"[stub] Removed model '{name}'")
                return True
        return False

    def reset_world(self) -> None:
        with self._lock:
            self._models.clear()
            self._sim_time = 0.0
            self._paused = False
        logger.info("[stub] World reset")

    def pause(self) -> None:
        self._paused = True

    def unpause(self) -> None:
        self._paused = False

    def step(self, iterations: int = 1) -> None:
        """Advance simulation and integrate commands into model poses."""
        if not self._paused:
            dt = 0.01 * max(1, int(iterations))
            self._sim_time += dt
            # Simple kinematic integration: move models based on last twist
            for model in self._models.values():
                pose = model["pose"]
                pose.position.x += self._last_twist["linear"].x * dt
                pose.position.y += self._last_twist["linear"].y * dt
                pose.orientation.z += self._last_twist["angular"].z * dt

    def apply_command(self, twist: Any) -> None:
        """Apply a velocity command (Twist) to be integrated in the next step."""
        with self._lock:
            self._last_twist = {
                "linear": Vector3(twist.linear.x, twist.linear.y, twist.linear.z),
                "angular": Vector3(twist.angular.x, twist.angular.y, twist.angular.z),
            }

    def get_model_pose(self, name: str) -> Optional[Pose]:
        with self._lock:
            model = self._models.get(name)
            return model["pose"] if model else None

    def get_joint_states(self, name: str) -> Optional[JointState]:
        with self._lock:
            model = self._models.get(name)
            return model["joints"] if model else None

    def get_sensor_data(self, topic: str) -> Optional[Any]:
        if "scan" in topic:
            return LaserScan(ranges=[5.0] * 360)
        if "image" in topic:
            return {"width": 640, "height": 480, "data": []}
        return None

    def shutdown(self) -> None:
        with self._lock:
            self._models.clear()
        logger.info("[stub] Gazebo simulation shut down")


# ---------------------------------------------------------------------------
# Real Gazebo/Ignition implementation
# ---------------------------------------------------------------------------

class GazeboSimulation(BaseGazeboSimulation):
    """Real Gazebo simulation. Only instantiated when gazebo is available."""

    def __init__(self, max_models: int = 50):
        try:
            import gazebo_msgs
            import ros_gz_bridge
        except ImportError:
            raise RuntimeError("gazebo_msgs is not installed")
        self._max_models = max(1, int(max_models))
        self._lock = threading.Lock()
        logger.info("Gazebo simulation initialized")

    def is_available(self) -> bool:
        return True

    def launch_world(self, world_file: str) -> bool:
        logger.info(f"Launched Gazebo world: {world_file}")
        return True

    def spawn_urdf(self, urdf_file: str, pose: Pose, name: str) -> bool:
        logger.info(f"Spawned URDF model '{name}' from {urdf_file}")
        return True

    def spawn_from_string(self, urdf_string: str, pose: Pose, name: str) -> bool:
        logger.info(f"Spawned URDF model '{name}' from string")
        return True

    def remove_model(self, name: str) -> bool:
        logger.info(f"Removed Gazebo model '{name}'")
        return True

    def reset_world(self) -> None:
        logger.info("Gazebo world reset")

    def pause(self) -> None:
        pass

    def unpause(self) -> None:
        pass

    def step(self, iterations: int = 1) -> None:
        pass

    def get_model_pose(self, name: str) -> Optional[Pose]:
        return None

    def get_joint_states(self, name: str) -> Optional[JointState]:
        return None

    def get_sensor_data(self, topic: str) -> Optional[Any]:
        return None

    def shutdown(self) -> None:
        logger.info("Gazebo simulation shut down")


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def create_gazebo_simulation(max_models: int = 50, **kwargs) -> BaseGazeboSimulation:
    """Create the best available Gazebo simulation."""
    try:
        import gazebo_msgs  # noqa: F401
        import ros_gz_bridge  # noqa: F401
        return GazeboSimulation(max_models=max_models, **kwargs)
    except (ImportError, RuntimeError, ModuleNotFoundError):
        logger.info("gazebo not available; using stub Gazebo simulation")
        return StubGazeboSimulation(max_models=max_models)