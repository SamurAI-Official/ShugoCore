"""
Base robot model class for ShugoCore simulation.

Provides common interface for all robot platforms.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple


@dataclass
class JointLimits:
    """Joint limits for a single joint."""
    lower: float
    upper: float
    max_velocity: float = 1.0
    max_effort: float = 100.0


@dataclass
class RobotModel(ABC):
    """
    Base class for robot models.

    Each robot provides its configuration and model file.
    Models are downloaded at runtime if not cached.
    """

    # Override in subclasses
    name: str = "generic_robot"
    num_joints: int = 0
    joint_names: List[str] = field(default_factory=list)
    joint_limits: Dict[str, JointLimits] = field(default_factory=dict)

    # Model source
    model_url: str = ""
    model_filename: str = ""
    model_license: str = ""

    # Default standing pose (joint positions)
    default_pose: Dict[str, float] = field(default_factory=dict)

    # Sensor configuration
    has_imu: bool = False
    has_camera: bool = False
    has_force_sensors: bool = False

    @abstractmethod
    def get_model_path(self) -> str:
        """
        Get path to model file.

        Downloads model if not already cached.
        Returns path to MJCF/URDF file.
        """
        ...

    @abstractmethod
    def get_initial_joint_positions(self) -> Dict[str, float]:
        """Get default initial joint positions for standing."""
        ...

    def get_joint_names(self) -> List[str]:
        """Get list of joint names."""
        return self.joint_names

    def get_joint_limits_array(self) -> Tuple[List[float], List[float]]:
        """Get joint limits as (lower, upper) arrays."""
        lower = []
        upper = []
        for name in self.joint_names:
            if name in self.joint_limits:
                lower.append(self.joint_limits[name].lower)
                upper.append(self.joint_limits[name].upper)
            else:
                import math
                lower.append(-math.pi)
                upper.append(math.pi)
        return lower, upper

    def to_dict(self) -> Dict:
        """Serialize robot configuration to dict."""
        return {
            "name": self.name,
            "num_joints": self.num_joints,
            "joint_names": self.joint_names,
            "model_license": self.model_license,
            "has_imu": self.has_imu,
            "has_camera": self.has_camera,
            "has_force_sensors": self.has_force_sensors,
        }
