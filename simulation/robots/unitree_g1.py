"""
Unitree G1 robot model.

Compact humanoid robot by Unitree Robotics.
Available in LeRobot for simulation and real-world use.
"""

import logging
from pathlib import Path
from typing import Dict

from simulation.robots.base import RobotModel, JointLimits

logger = logging.getLogger(__name__)


class UnitreeG1(RobotModel):
    """
    Unitree G1 robot configuration.

    Compact humanoid with 23-35 DOF depending on configuration.
    Popular for research and available with open-source models.
    """

    def __init__(self):
        super().__init__(
            name="unitree_g1",
            num_joints=23,
            joint_names=[
                # Left leg (6 DOF)
                "left_hip_pitch", "left_hip_roll", "left_hip_yaw",
                "left_knee", "left_ankle_pitch", "left_ankle_roll",
                # Right leg (6 DOF)
                "right_hip_pitch", "right_hip_roll", "right_hip_yaw",
                "right_knee", "right_ankle_pitch", "right_ankle_roll",
                # Waist (3 DOF)
                "waist_yaw", "waist_roll", "waist_pitch",
                # Left arm (4 DOF simplified)
                "left_shoulder_pitch", "left_shoulder_roll",
                "left_elbow", "left_wrist_roll",
                # Right arm (4 DOF simplified)
                "right_shoulder_pitch", "right_shoulder_roll",
                "right_elbow", "right_wrist_roll",
            ],
            joint_limits={
                # Leg joints
                "left_hip_pitch": JointLimits(-2.0, 2.0, max_velocity=7.0),
                "left_hip_roll": JointLimits(-0.5, 0.5, max_velocity=7.0),
                "left_hip_yaw": JointLimits(-1.0, 1.0, max_velocity=7.0),
                "left_knee": JointLimits(-2.5, 0.0, max_velocity=7.0),
                "left_ankle_pitch": JointLimits(-0.8, 0.8, max_velocity=7.0),
                "left_ankle_roll": JointLimits(-0.3, 0.3, max_velocity=7.0),
                "right_hip_pitch": JointLimits(-2.0, 2.0, max_velocity=7.0),
                "right_hip_roll": JointLimits(-0.5, 0.5, max_velocity=7.0),
                "right_hip_yaw": JointLimits(-1.0, 1.0, max_velocity=7.0),
                "right_knee": JointLimits(-2.5, 0.0, max_velocity=7.0),
                "right_ankle_pitch": JointLimits(-0.8, 0.8, max_velocity=7.0),
                "right_ankle_roll": JointLimits(-0.3, 0.3, max_velocity=7.0),
                # Waist
                "waist_yaw": JointLimits(-1.5, 1.5, max_velocity=5.0),
                "waist_roll": JointLimits(-0.5, 0.5, max_velocity=5.0),
                "waist_pitch": JointLimits(-0.5, 0.5, max_velocity=5.0),
                # Arms
                "left_shoulder_pitch": JointLimits(-3.14, 3.14, max_velocity=5.0),
                "left_shoulder_roll": JointLimits(-1.5, 1.5, max_velocity=5.0),
                "left_elbow": JointLimits(-2.5, 0.0, max_velocity=5.0),
                "left_wrist_roll": JointLimits(-1.0, 1.0, max_velocity=5.0),
                "right_shoulder_pitch": JointLimits(-3.14, 3.14, max_velocity=5.0),
                "right_shoulder_roll": JointLimits(-1.5, 1.5, max_velocity=5.0),
                "right_elbow": JointLimits(-2.5, 0.0, max_velocity=5.0),
                "right_wrist_roll": JointLimits(-1.0, 1.0, max_velocity=5.0),
            },
            model_url="https://github.com/unitrobotics/unitree_mujoco/",
            model_filename="g1.xml",
            model_license="BSD-3-Clause",
            has_imu=True,
            has_camera=False,
            has_force_sensors=True,
        )

    def get_model_path(self) -> str:
        """Get path to Unitree G1 MJCF model."""
        cache_dir = Path.home() / ".shugocore" / "models" / "unitree_g1"
        cache_dir.mkdir(parents=True, exist_ok=True)
        model_path = cache_dir / "g1.xml"

        if model_path.exists():
            return str(model_path)

        logger.info(f"Unitree G1 model not cached. Expected at: {model_path}")
        return str(model_path)

    def get_initial_joint_positions(self) -> Dict[str, float]:
        """Get default standing pose."""
        return {
            "left_hip_pitch": -0.2, "left_hip_roll": 0.0,
            "left_hip_yaw": 0.0, "left_knee": -0.4,
            "left_ankle_pitch": 0.2, "left_ankle_roll": 0.0,
            "right_hip_pitch": -0.2, "right_hip_roll": 0.0,
            "right_hip_yaw": 0.0, "right_knee": -0.4,
            "right_ankle_pitch": 0.2, "right_ankle_roll": 0.0,
            "waist_yaw": 0.0, "waist_roll": 0.0, "waist_pitch": 0.0,
            "left_shoulder_pitch": 0.0, "left_shoulder_roll": 0.2,
            "left_elbow": -0.5, "left_wrist_roll": 0.0,
            "right_shoulder_pitch": 0.0, "right_shoulder_roll": -0.2,
            "right_elbow": -0.5, "right_wrist_roll": 0.0,
        }
