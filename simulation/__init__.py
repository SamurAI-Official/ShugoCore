"""
ShugoCore Simulation Framework
==============================

Provides physics-based robot simulation for generating public test data.
Supports multiple robot platforms and simulation backends.

Architecture:
    - BaseSimulation: Interface for all simulation backends
    - MuJoCoSimulation: MuJoCo physics backend (pip install shugocore[simulation])
    - StubSimulation: Deterministic fallback (no dependencies)
    - RobotModel: Base class for robot-specific configurations
    - Scenarios: Standardized test scenarios producing public benchmark data

Usage:
    from simulation import create_simulation, BerkeleyHumanoidLite

    robot = BerkeleyHumanoidLite()
    sim = create_simulation(robot.model_path)
    result = WalkToTarget(robot, sim).run()
"""

from simulation.base import BaseSimulation, SimulationResult
from simulation.stub_sim import StubSimulation
from simulation.scenarios import SimulationScenario, WalkToTarget, BalanceTest, EmergencyStop

__all__ = [
    "BaseSimulation",
    "SimulationResult",
    "StubSimulation",
    "SimulationScenario",
    "WalkToTarget",
    "BalanceTest",
    "EmergencyStop",
    "create_simulation",
]

from simulation.mujoco_sim import MuJoCoSimulation


def create_simulation(model_path: str = None, robot_name: str = None) -> BaseSimulation:
    """
    Create the best available simulation backend.

    Attempts MuJoCo first, falls back to stub simulation.
    Models are downloaded at runtime if not already cached.

    Args:
        model_path: Path to MJCF/URDF model file
        robot_name: Name of robot (used to auto-resolve model path)

    Returns:
        BaseSimulation instance
    """
    # Resolve model path from robot name if provided
    if robot_name and not model_path:
        from simulation.robots import get_robot_model
        robot = get_robot_model(robot_name)
        model_path = robot.get_model_path()

    # Try MuJoCo backend
    try:
        return MuJoCoSimulation(model_path)
    except (ImportError, RuntimeError, OSError):
        pass

    # Fallback to stub
    return StubSimulation(model_path)
