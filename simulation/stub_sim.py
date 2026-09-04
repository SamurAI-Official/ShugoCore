"""
Stub simulation backend for ShugoCore.

Deterministic fallback when MuJoCo is not installed.
Tracks robot state in memory with simple kinematic integration.
"""

import logging
import threading
from typing import Dict, Optional

import numpy as np

from simulation.base import BaseSimulation

logger = logging.getLogger(__name__)


class StubSimulation(BaseSimulation):
    """
    Deterministic in-memory simulation.

    Integrates joint commands into positions with simple kinematics.
    Useful for testing the full command->effect chain without physics.
    """

    def __init__(self, model_path: Optional[str] = None, seed: int = 42,
                 num_joints: int = 24):
        super().__init__(model_path, seed)
        self._num_joints = num_joints
        self._rng = np.random.RandomState(seed)
        self._joint_positions = np.zeros(num_joints)
        self._joint_velocities = np.zeros(num_joints)
        self._base_position = np.zeros(3)
        self._base_orientation = np.array([1.0, 0.0, 0.0, 0.0])  # w,x,y,z
        self._last_commands = np.zeros(num_joints)
        self._lock = threading.Lock()
        logger.info(f"[stub] Simulation initialized ({num_joints} joints)")

    def is_available(self) -> bool:
        return False

    def step(self, dt: float = 0.001) -> None:
        """Integrate commands into positions."""
        with self._lock:
            # Simple integration: position += command * dt
            self._joint_positions += self._last_commands * dt
            self._joint_velocities = self._last_commands.copy()
            self._time += dt
            self._step_count += 1

            # Integrate base position from first 3 commands (x, y, yaw)
            if len(self._last_commands) >= 3:
                self._base_position[0] += self._last_commands[0] * dt
                self._base_position[1] += self._last_commands[1] * dt
                # Simple yaw integration
                yaw = self._last_commands[2] * dt
                # Update quaternion (simplified rotation about z)
                cos_yaw = np.cos(yaw / 2)
                sin_yaw = np.sin(yaw / 2)
                self._base_orientation = np.array([
                    cos_yaw,
                    self._base_orientation[1],
                    self._base_orientation[2],
                    sin_yaw
                ])

    def reset(self) -> None:
        """Reset to initial state."""
        with self._lock:
            self._joint_positions = np.zeros(self._num_joints)
            self._joint_velocities = np.zeros(self._num_joints)
            self._base_position = np.zeros(3)
            self._base_orientation = np.array([1.0, 0.0, 0.0, 0.0])
            self._last_commands = np.zeros(self._num_joints)
            self._time = 0.0
            self._step_count = 0

    def get_joint_positions(self) -> np.ndarray:
        with self._lock:
            return self._joint_positions.copy()

    def get_joint_velocities(self) -> np.ndarray:
        with self._lock:
            return self._joint_velocities.copy()

    def set_joint_commands(self, commands: np.ndarray) -> None:
        with self._lock:
            self._last_commands = np.asarray(commands, dtype=np.float64)

    def get_imu_data(self) -> Dict[str, np.ndarray]:
        """Return deterministic IMU data."""
        with self._lock:
            # Simulated accelerometer (gravity + motion)
            accel = np.array([0.0, 0.0, -9.81]) + self._rng.randn(3) * 0.01
            # Simulated gyroscope
            gyro = self._joint_velocities[:3] * 0.1 + self._rng.randn(3) * 0.001
            return {"accel": accel, "gyro": gyro}

    def get_base_position(self) -> np.ndarray:
        with self._lock:
            return self._base_position.copy()

    def get_base_orientation(self) -> np.ndarray:
        with self._lock:
            return self._base_orientation.copy()
