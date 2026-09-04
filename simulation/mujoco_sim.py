"""
MuJoCo simulation backend for ShugoCore.

Provides high-fidelity physics simulation using the open-source MuJoCo engine.
Automatically downloaded models at runtime.
"""

import logging
import threading
from pathlib import Path
from typing import Dict, Optional

import numpy as np

from simulation.base import BaseSimulation

logger = logging.getLogger(__name__)


class MuJoCoSimulation(BaseSimulation):
    """
    MuJoCo physics simulation backend.

    Requires: pip install shugocore[simulation]
    Loads MJCF/URDF models and provides realistic physics simulation.
    """

    def __init__(self, model_path: Optional[str] = None, seed: int = 42):
        super().__init__(model_path, seed)

        try:
            import mujoco
            self._mujoco = mujoco
        except ImportError:
            raise ImportError(
                "MuJoCo not installed. Install with: pip install 'shugocore[simulation]'"
            )

        self._model = None
        self._data = None
        self._lock = threading.Lock()

        if model_path:
            self._load_model(model_path)
        else:
            # Create empty world
            self._create_empty_world()

        self._rng = np.random.RandomState(seed)
        logger.info("[mujoco] Simulation initialized")

    def _load_model(self, model_path: str) -> None:
        """Load model from file path."""
        path = Path(model_path)
        if not path.exists():
            raise FileNotFoundError(f"Model file not found: {model_path}")

        try:
            self._model = self._mujoco.MjModel.from_xml_path(str(path))
            self._data = self._mujoco.MjData(self._model)
            logger.info(f"[mujoco] Loaded model: {model_path}")
        except Exception as e:
            raise RuntimeError(f"Failed to load model: {e}")

    def _create_empty_world(self) -> None:
        """Create a minimal empty world."""
        xml = """
        <mujoco>
            <worldbody>
                <geom name="floor" type="plane" size="10 10 0.1" rgba="0.8 0.8 0.8 1"/>
                <light diffuse="0.8 0.8 0.8" pos="0 0 5" dir="0 0 -1"/>
            </worldbody>
        </mujoco>
        """
        self._model = self._mujoco.MjModel.from_xml_string(xml)
        self._data = self._mujoco.MjData(self._model)

    def is_available(self) -> bool:
        return True

    def step(self, dt: float = 0.001) -> None:
        """Advance simulation by one timestep."""
        with self._lock:
            self._mujoco.mj_step(self._model, self._data)
            self._time += self._model.opt.timestep
            self._step_count += 1

    def reset(self) -> None:
        """Reset simulation to initial state."""
        with self._lock:
            self._mujoco.mj_resetData(self._model, self._data)
            self._time = 0.0
            self._step_count = 0

    def get_joint_positions(self) -> np.ndarray:
        """Get joint positions (excluding free joint if present)."""
        with self._lock:
            # Return actuated joint positions
            return self._data.qpos[-self._model.nu:].copy()

    def get_joint_velocities(self) -> np.ndarray:
        """Get joint velocities."""
        with self._lock:
            return self._data.qvel[-self._model.nu:].copy()

    def set_joint_commands(self, commands: np.ndarray) -> None:
        """Set joint position commands via position actuators."""
        with self._lock:
            commands = np.asarray(commands, dtype=np.float64)
            # Set actuator commands (position control)
            for i, cmd in enumerate(commands[:self._model.nu]):
                self._data.ctrl[i] = cmd

    def get_imu_data(self) -> Dict[str, np.ndarray]:
        """Get IMU data if sensors exist in model."""
        with self._lock:
            result = {"accel": np.zeros(3), "gyro": np.zeros(3)}

            # Try to find IMU sensors
            for i in range(self._model.nsensor):
                sensor_type = self._model.sensor_type[i]
                # Accelerometer = 0, Gyro = 1 in MuJoCo
                if sensor_type == 0:  # accelerometer
                    adr = self._model.sensor_adr[i]
                    dim = self._model.sensor_dim[i]
                    result["accel"] = self._data.sensordata[adr:adr+dim].copy()
                elif sensor_type == 1:  # gyro
                    adr = self._model.sensor_adr[i]
                    dim = self._model.sensor_dim[i]
                    result["gyro"] = self._data.sensordata[adr:adr+dim].copy()

            return result

    def get_base_position(self) -> np.ndarray:
        """Get base link position (first 3 of qpos if free joint)."""
        with self._lock:
            if self._model.nq > self._model.nu:
                # Has free joint
                return self._data.qpos[:3].copy()
            return np.zeros(3)

    def get_base_orientation(self) -> np.ndarray:
        """Get base link orientation quaternion."""
        with self._lock:
            if self._model.nq > self._model.nu:
                # Has free joint (quaternion at positions 3:7)
                return self._data.qpos[3:7].copy()
            return np.array([1.0, 0.0, 0.0, 0.0])

    @property
    def num_actuators(self) -> int:
        """Number of actuators in the model."""
        return self._model.nu if self._model else 0

    @property
    def model(self):
        """Access the MuJoCo model."""
        return self._model

    @property
    def data(self):
        """Access the MuJoCo data."""
        return self._data
