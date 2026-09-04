"""
Base simulation interface for ShugoCore.

All simulation backends implement this interface, ensuring consistent
behavior across MuJoCo, PyBullet, and stub implementations.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import numpy as np


@dataclass
class SimulationResult:
    """
    Standardized result from a simulation scenario run.

    This format is designed to be serialized to JSONL for public test data.
    """
    scenario: str
    robot: str
    seed: int
    steps: int
    duration_seconds: float
    success: bool
    metrics: Dict[str, Any] = field(default_factory=dict)
    joint_trajectories: List[Dict[str, List[float]]] = field(default_factory=list)
    sensor_data: List[Dict[str, Any]] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for JSONL serialization."""
        return {
            "scenario": self.scenario,
            "robot": self.robot,
            "seed": self.seed,
            "steps": self.steps,
            "duration_seconds": self.duration_seconds,
            "success": self.success,
            "metrics": self.metrics,
            "joint_trajectories": self.joint_trajectories[-10:],  # Last 10 steps
            "sensor_data": self.sensor_data[-10:],
            "metadata": self.metadata,
        }


class BaseSimulation(ABC):
    """
    Abstract base for all simulation backends.

    Implementations must provide deterministic, seeded simulation
    with consistent state access and command interfaces.
    """

    def __init__(self, model_path: Optional[str] = None, seed: int = 42):
        self._model_path = model_path
        self._seed = seed
        self._step_count = 0
        self._time = 0.0

    @abstractmethod
    def is_available(self) -> bool:
        """Check if this backend has all required dependencies."""
        ...

    @abstractmethod
    def step(self, dt: float = 0.001) -> None:
        """Advance simulation by one timestep."""
        ...

    @abstractmethod
    def reset(self) -> None:
        """Reset simulation to initial state."""
        ...

    @abstractmethod
    def get_joint_positions(self) -> np.ndarray:
        """Get current joint positions (radians)."""
        ...

    @abstractmethod
    def get_joint_velocities(self) -> np.ndarray:
        """Get current joint velocities (rad/s)."""
        ...

    @abstractmethod
    def set_joint_commands(self, commands: np.ndarray) -> None:
        """Set joint position commands."""
        ...

    @abstractmethod
    def get_imu_data(self) -> Dict[str, np.ndarray]:
        """Get IMU sensor data (accel, gyro)."""
        ...

    @abstractmethod
    def get_base_position(self) -> np.ndarray:
        """Get base link position [x, y, z]."""
        ...

    @abstractmethod
    def get_base_orientation(self) -> np.ndarray:
        """Get base link orientation quaternion [w, x, y, z]."""
        ...

    @property
    def step_count(self) -> int:
        return self._step_count

    @property
    def time(self) -> float:
        return self._time
