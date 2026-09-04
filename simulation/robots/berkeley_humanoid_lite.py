"""
Berkeley Humanoid Lite robot model.

Open-source humanoid robot from UC Berkeley Hybrid Robotics Lab.
License: MIT (code), CC BY-SA 4.0 (assets)
Reference: https://github.com/HybridRobotics/berkeley-humanoid-lite
"""

import logging
import math
from pathlib import Path
from typing import Dict, List

from simulation.robots.base import RobotModel, JointLimits

logger = logging.getLogger(__name__)


class BerkeleyHumanoidLite(RobotModel):
    """
    Berkeley Humanoid Lite robot configuration.

    24-DOF humanoid with modular 3D-printed gearboxes.
    Designed for sub-$5,000 total cost.
    """

    def __init__(self):
        super().__init__(
            name="berkeley_humanoid_lite",
            num_joints=24,
            joint_names=[
                # Left leg (6 DOF)
                "left_hip_roll", "left_hip_pitch", "left_hip_yaw",
                "left_knee", "left_ankle_pitch", "left_ankle_roll",
                # Right leg (6 DOF)
                "right_hip_roll", "right_hip_pitch", "right_hip_yaw",
                "right_knee", "right_ankle_pitch", "right_ankle_roll",
                # Left arm (6 DOF)
                "left_shoulder_pitch", "left_shoulder_roll", "left_shoulder_yaw",
                "left_elbow", "left_wrist_pitch", "left_wrist_roll",
                # Right arm (6 DOF)
                "right_shoulder_pitch", "right_shoulder_roll", "right_shoulder_yaw",
                "right_elbow", "right_wrist_pitch", "right_wrist_roll",
            ],
            joint_limits={
                # Leg joints
                "left_hip_roll": JointLimits(-0.5, 0.5, max_velocity=5.0),
                "left_hip_pitch": JointLimits(-1.5, 1.5, max_velocity=5.0),
                "left_hip_yaw": JointLimits(-0.5, 0.5, max_velocity=5.0),
                "left_knee": JointLimits(-2.5, 0.0, max_velocity=5.0),
                "left_ankle_pitch": JointLimits(-0.8, 0.8, max_velocity=5.0),
                "left_ankle_roll": JointLimits(-0.5, 0.5, max_velocity=5.0),
                "right_hip_roll": JointLimits(-0.5, 0.5, max_velocity=5.0),
                "right_hip_pitch": JointLimits(-1.5, 1.5, max_velocity=5.0),
                "right_hip_yaw": JointLimits(-0.5, 0.5, max_velocity=5.0),
                "right_knee": JointLimits(-2.5, 0.0, max_velocity=5.0),
                "right_ankle_pitch": JointLimits(-0.8, 0.8, max_velocity=5.0),
                "right_ankle_roll": JointLimits(-0.5, 0.5, max_velocity=5.0),
                # Arm joints
                "left_shoulder_pitch": JointLimits(-3.14, 3.14, max_velocity=3.0),
                "left_shoulder_roll": JointLimits(-1.5, 1.5, max_velocity=3.0),
                "left_shoulder_yaw": JointLimits(-1.5, 1.5, max_velocity=3.0),
                "left_elbow": JointLimits(-2.5, 0.0, max_velocity=3.0),
                "left_wrist_pitch": JointLimits(-1.0, 1.0, max_velocity=3.0),
                "left_wrist_roll": JointLimits(-1.0, 1.0, max_velocity=3.0),
                "right_shoulder_pitch": JointLimits(-3.14, 3.14, max_velocity=3.0),
                "right_shoulder_roll": JointLimits(-1.5, 1.5, max_velocity=3.0),
                "right_shoulder_yaw": JointLimits(-1.5, 1.5, max_velocity=3.0),
                "right_elbow": JointLimits(-2.5, 0.0, max_velocity=3.0),
                "right_wrist_pitch": JointLimits(-1.0, 1.0, max_velocity=3.0),
                "right_wrist_roll": JointLimits(-1.0, 1.0, max_velocity=3.0),
            },
            model_url="https://github.com/HybridRobotics/berkeley-humanoid-lite/raw/main/source/berkeley_humanoid_lite_assets/",
            model_filename="berkeley_humanoid_lite.xml",
            model_license="MIT",
            has_imu=True,
            has_camera=False,
            has_force_sensors=False,
        )

    def get_model_path(self) -> str:
        """Get path to Berkeley Humanoid Lite MJCF model."""
        # Check cache first
        cache_dir = Path.home() / ".shugocore" / "models" / "berkeley_humanoid_lite"
        cache_dir.mkdir(parents=True, exist_ok=True)
        model_path = cache_dir / "berkeley_humanoid_lite.xml"

        if model_path.exists():
            return str(model_path)

        # Model not cached - return expected path
        # Actual download will be handled by model manager
        logger.info(
            f"Berkeley Humanoid Lite model not cached. "
            f"Expected at: {model_path}"
        )
        logger.info(
            "Download from: https://github.com/HybridRobotics/berkeley-humanoid-lite"
        )
        return str(model_path)

    def get_initial_joint_positions(self) -> Dict[str, float]:
        """Get standing pose joint positions."""
        return {
            # Standing pose - slight bend in knees
            "left_hip_roll": 0.0, "left_hip_pitch": -0.2,
            "left_hip_yaw": 0.0, "left_knee": -0.4,
            "left_ankle_pitch": 0.2, "left_ankle_roll": 0.0,
            "right_hip_roll": 0.0, "right_hip_pitch": -0.2,
            "right_hip_yaw": 0.0, "right_knee": -0.4,
            "right_ankle_pitch": 0.2, "right_ankle_roll": 0.0,
            # Arms at sides
            "left_shoulder_pitch": 0.0, "left_shoulder_roll": 0.2,
            "left_shoulder_yaw": 0.0, "left_elbow": -0.5,
            "left_wrist_pitch": 0.0, "left_wrist_roll": 0.0,
            "right_shoulder_pitch": 0.0, "right_shoulder_roll": -0.2,
            "right_shoulder_yaw": 0.0, "right_elbow": -0.5,
            "right_wrist_pitch": 0.0, "right_wrist_roll": 0.0,
        }
