"""
Robot model definitions for ShugoCore simulation.

Each robot provides:
- Joint names and limits
- Default standing pose
- Sensor configuration
- Model file path (downloaded at runtime)
"""

from simulation.robots.base import RobotModel, JointLimits
from simulation.robots.berkeley_humanoid_lite import BerkeleyHumanoidLite
from simulation.robots.reachy2 import Reachy2
from simulation.robots.unitree_g1 import UnitreeG1

__all__ = [
    "RobotModel",
    "JointLimits",
    "BerkeleyHumanoidLite",
    "Reachy2",
    "UnitreeG1",
    "get_robot_model",
    "list_robots",
]

# Registry of available robots
_ROBOTS = {
    "berkeley_humanoid_lite": BerkeleyHumanoidLite,
    "reachy2": Reachy2,
    "unitree_g1": UnitreeG1,
}


def get_robot_model(name: str) -> RobotModel:
    """
    Get a robot model by name.

    Args:
        name: Robot identifier (e.g., "berkeley_humanoid_lite")

    Returns:
        RobotModel instance
    """
    if name not in _ROBOTS:
        raise ValueError(
            f"Unknown robot: {name}. Available: {list(_ROBOTS.keys())}"
        )
    return _ROBOTS[name]()


def list_robots() -> list:
    """List all available robot names."""
    return list(_ROBOTS.keys())
