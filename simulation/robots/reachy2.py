"""
Reachy 2 robot model.

Open-source humanoid robot by Pollen Robotics.
Reference: https://huggingface.co/docs/lerobot/en/reachy2
"""

import logging
from pathlib import Path
from typing import Dict

from simulation.robots.base import RobotModel, JointLimits

logger = logging.getLogger(__name__)


class Reachy2(RobotModel):
    """
    Reachy 2 robot configuration.

    Humanoid with mobile base, 2 arms, neck, and antennas.
    Used with LeRobot for imitation learning research.
    """

    def __init__(self):
        super().__init__(
            name="reachy2",
            num_joints=20,
            joint_names=[
                # Mobile base (simplified to 2 wheel joints)
                "left_wheel", "right_wheel",
                # Head/neck (3 DOF)
                "neck_roll", "neck_pitch", "neck_yaw",
                # Left arm (7 DOF)
                "l_shoulder_pitch", "l_shoulder_roll",
                "l_arm_yaw", "l_elbow_pitch",
                "l_forearm_yaw", "l_wrist_pitch", "l_wrist_roll",
                # Right arm (7 DOF)
                "r_shoulder_pitch", "r_shoulder_roll",
                "r_arm_yaw", "r_elbow_pitch",
                "r_forearm_yaw", "r_wrist_pitch", "r_wrist_roll",
                # Antennas (2 DOF)
                "l_antenna", "r_antenna",
            ],
            joint_limits={
                # Head
                "neck_roll": JointLimits(-0.5, 0.5, max_velocity=2.0),
                "neck_pitch": JointLimits(-0.5, 0.5, max_velocity=2.0),
                "neck_yaw": JointLimits(-1.5, 1.5, max_velocity=2.0),
                # Left arm
                "l_shoulder_pitch": JointLimits(-3.14, 3.14, max_velocity=2.0),
                "l_shoulder_roll": JointLimits(-1.5, 1.5, max_velocity=2.0),
                "l_arm_yaw": JointLimits(-1.5, 1.5, max_velocity=2.0),
                "l_elbow_pitch": JointLimits(-2.5, 0.0, max_velocity=2.0),
                "l_forearm_yaw": JointLimits(-1.5, 1.5, max_velocity=2.0),
                "l_wrist_pitch": JointLimits(-1.0, 1.0, max_velocity=2.0),
                "l_wrist_roll": JointLimits(-1.0, 1.0, max_velocity=2.0),
                # Right arm
                "r_shoulder_pitch": JointLimits(-3.14, 3.14, max_velocity=2.0),
                "r_shoulder_roll": JointLimits(-1.5, 1.5, max_velocity=2.0),
                "r_arm_yaw": JointLimits(-1.5, 1.5, max_velocity=2.0),
                "r_elbow_pitch": JointLimits(-2.5, 0.0, max_velocity=2.0),
                "r_forearm_yaw": JointLimits(-1.5, 1.5, max_velocity=2.0),
                "r_wrist_pitch": JointLimits(-1.0, 1.0, max_velocity=2.0),
                "r_wrist_roll": JointLimits(-1.0, 1.0, max_velocity=2.0),
                # Antennas
                "l_antenna": JointLimits(-1.0, 1.0, max_velocity=5.0),
                "r_antenna": JointLimits(-1.0, 1.0, max_velocity=5.0),
            },
            model_url="https://github.com/pollen-robotics/reachy2_mujoco/",
            model_filename="reachy2.xml",
            model_license="Apache-2.0",
            has_imu=True,
            has_camera=True,
            has_force_sensors=True,
        )

    def get_model_path(self) -> str:
        """Get path to Reachy 2 MJCF model."""
        cache_dir = Path.home() / ".shugocore" / "models" / "reachy2"
        cache_dir.mkdir(parents=True, exist_ok=True)
        model_path = cache_dir / "reachy2.xml"

        if model_path.exists():
            return str(model_path)

        logger.info(f"Reachy 2 model not cached. Expected at: {model_path}")
        return str(model_path)

    def get_initial_joint_positions(self) -> Dict[str, float]:
        """Get default standing pose."""
        return {
            "neck_roll": 0.0, "neck_pitch": 0.0, "neck_yaw": 0.0,
            "l_shoulder_pitch": 0.0, "l_shoulder_roll": 0.3,
            "l_arm_yaw": 0.0, "l_elbow_pitch": -0.5,
            "l_forearm_yaw": 0.0, "l_wrist_pitch": 0.0, "l_wrist_roll": 0.0,
            "r_shoulder_pitch": 0.0, "r_shoulder_roll": -0.3,
            "r_arm_yaw": 0.0, "r_elbow_pitch": -0.5,
            "r_forearm_yaw": 0.0, "r_wrist_pitch": 0.0, "r_wrist_roll": 0.0,
            "l_antenna": 0.0, "r_antenna": 0.0,
        }
