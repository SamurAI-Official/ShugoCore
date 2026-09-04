"""
Standardized simulation test scenarios for ShugoCore.

Each scenario produces public benchmark data in JSONL format.
Scenarios are deterministic with seeded RNG for reproducibility.
"""

import json
import logging
import time
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np

from simulation.base import BaseSimulation, SimulationResult
from simulation.robots.base import RobotModel

logger = logging.getLogger(__name__)


class SimulationScenario(ABC):
    """Base class for simulation test scenarios."""

    def __init__(self, robot: RobotModel, simulation: BaseSimulation,
                 max_steps: int = 1000, seed: int = 42):
        self.robot = robot
        self.simulation = simulation
        self.max_steps = max_steps
        self.seed = seed
        self._rng = np.random.RandomState(seed)

    @abstractmethod
    def get_action(self, step: int) -> np.ndarray:
        """Get joint commands for current step."""
        ...

    @abstractmethod
    def check_success(self) -> bool:
        """Check if scenario objective is achieved."""
        ...

    @abstractmethod
    def get_metrics(self) -> Dict[str, Any]:
        """Get scenario-specific metrics."""
        ...

    def run(self) -> SimulationResult:
        """Execute the scenario and return results."""
        logger.info(f"Running scenario: {self.__class__.__name__} on {self.robot.name}")

        self.simulation.reset()
        start_time = time.time()
        joint_trajectories = []
        sensor_data = []

        success = False
        for step in range(self.max_steps):
            action = self.get_action(step)
            self.simulation.set_joint_commands(action)
            self.simulation.step()

            joint_trajectories.append({
                "positions": self.simulation.get_joint_positions().tolist(),
                "velocities": self.simulation.get_joint_velocities().tolist(),
            })
            sensor_data.append({
                "imu": {k: v.tolist() for k, v in self.simulation.get_imu_data().items()},
                "base_position": self.simulation.get_base_position().tolist(),
            })

            if self.check_success():
                success = True
                break

        duration = time.time() - start_time

        return SimulationResult(
            scenario=self.__class__.__name__,
            robot=self.robot.name,
            seed=self.seed,
            steps=self.simulation.step_count,
            duration_seconds=duration,
            success=success,
            metrics=self.get_metrics(),
            joint_trajectories=joint_trajectories,
            sensor_data=sensor_data,
            metadata={
                "max_steps": self.max_steps,
                "num_joints": self.robot.num_joints,
                "robot_config": self.robot.to_dict(),
            },
        )


class WalkToTarget(SimulationScenario):
    """Navigation scenario: walk to a target position."""

    def __init__(self, robot: RobotModel, simulation: BaseSimulation,
                 target: np.ndarray = None, max_steps: int = 1000, seed: int = 42):
        super().__init__(robot, simulation, max_steps, seed)
        self.target = target if target is not None else np.array([1.0, 0.0])
        self._start_position = None

    def get_action(self, step: int) -> np.ndarray:
        """Generate walking gait toward target."""
        freq = 2.0
        phase = step * 0.01 * freq
        actions = np.zeros(self.robot.num_joints)

        if self.robot.num_joints >= 12:
            actions[1] = 0.3 * np.sin(phase)
            actions[3] = -0.2 * abs(np.sin(phase))
            actions[7] = 0.3 * np.sin(phase + np.pi)
            actions[9] = -0.2 * abs(np.sin(phase + np.pi))
        return actions

    def check_success(self) -> bool:
        """Check if reached target."""
        pos = self.simulation.get_base_position()[:2]
        if self._start_position is None:
            self._start_position = pos.copy()
        return np.linalg.norm(pos - self.target) < 0.1

    def get_metrics(self) -> Dict[str, Any]:
        """Calculate navigation metrics."""
        pos = self.simulation.get_base_position()[:2]
        distance_to_target = float(np.linalg.norm(pos - self.target))
        path_length = float(np.linalg.norm(pos - self._start_position)) if self._start_position is not None else 0.0

        return {
            "target": self.target.tolist(),
            "final_position": pos.tolist(),
            "distance_to_target": distance_to_target,
            "path_length": path_length,
            "path_efficiency": distance_to_target / max(path_length, 1e-6),
        }


class BalanceTest(SimulationScenario):
    """Balance scenario: maintain balance under perturbations."""

    def __init__(self, robot: RobotModel, simulation: BaseSimulation,
                 max_steps: int = 1000, seed: int = 42,
                 perturbation_interval: int = 200):
        super().__init__(robot, simulation, max_steps, seed)
        self.perturbation_interval = perturbation_interval
        self._perturbations_applied = 0

    def get_action(self, step: int) -> np.ndarray:
        """Apply balance control with periodic perturbations."""
        actions = np.zeros(self.robot.num_joints)

        if step > 0 and step % self.perturbation_interval == 0:
            perturbation = self._rng.randn(self.robot.num_joints) * 0.5
            actions += perturbation
            self._perturbations_applied += 1
        return actions

    def check_success(self) -> bool:
        """Success if still upright."""
        pos = self.simulation.get_base_position()
        return pos[2] > 0.3

    def get_metrics(self) -> Dict[str, Any]:
        """Calculate balance metrics."""
        pos = self.simulation.get_base_position()
        imu = self.simulation.get_imu_data()

        return {
            "final_height": float(pos[2]),
            "perturbations_applied": self._perturbations_applied,
            "tilt_angle": float(np.arctan2(
                np.linalg.norm(imu["accel"][:2]),
                abs(imu["accel"][2])
            )),
        }


class EmergencyStop(SimulationScenario):
    """Safety scenario: measure emergency stop response."""

    def __init__(self, robot: RobotModel, simulation: BaseSimulation,
                 target_speed: float = 1.0, max_steps: int = 500, seed: int = 42):
        super().__init__(robot, simulation, max_steps, seed)
        self.target_speed = target_speed
        self._stop_step = max_steps // 2

    def get_action(self, step: int) -> np.ndarray:
        """Accelerate then emergency stop."""
        actions = np.zeros(self.robot.num_joints)

        if step < self._stop_step and self.robot.num_joints >= 12:
            freq = 5.0
            phase = step * 0.01 * freq
            actions[1] = self.target_speed * np.sin(phase)
            actions[7] = self.target_speed * np.sin(phase + np.pi)
        return actions

    def check_success(self) -> bool:
        """Success if stopped after stop command."""
        if self.simulation.step_count < self._stop_step:
            return False
        vel = np.abs(self.simulation.get_joint_velocities())
        return np.max(vel) < 0.01

    def get_metrics(self) -> Dict[str, Any]:
        """Calculate stopping metrics."""
        pos = self.simulation.get_base_position()
        vel = self.simulation.get_joint_velocities()

        return {
            "target_speed": self.target_speed,
            "stop_step": self._stop_step,
            "stop_position": pos.tolist(),
            "max_joint_velocity": float(np.max(np.abs(vel))),
            "stopping_distance": float(np.linalg.norm(pos[:2])),
        }


def run_benchmark(robot_name: str, output_dir: str = None,
                  scenarios: List[str] = None) -> List[SimulationResult]:
    """
    Run a full benchmark suite on a robot.

    Args:
        robot_name: Name of robot to test
        output_dir: Directory to save JSONL results
        scenarios: List of scenario names to run (default: all)

    Returns:
        List of SimulationResult objects
    """
    from simulation import create_simulation
    from simulation.robots import get_robot_model

    robot = get_robot_model(robot_name)
    sim = create_simulation(robot_name=robot_name)

    if scenarios is None:
        scenarios = ["WalkToTarget", "BalanceTest", "EmergencyStop"]

    scenario_classes = {
        "WalkToTarget": WalkToTarget,
        "BalanceTest": BalanceTest,
        "EmergencyStop": EmergencyStop,
    }

    results = []
    for scenario_name in scenarios:
        if scenario_name not in scenario_classes:
            logger.warning(f"Unknown scenario: {scenario_name}")
            continue

        scenario = scenario_classes[scenario_name](robot, sim)
        result = scenario.run()
        results.append(result)

    if output_dir:
        output_path = Path(output_dir)
        output_path.mkdir(parents=True, exist_ok=True)
        output_file = output_path / f"{robot_name}_benchmark.jsonl"

        with open(output_file, "w") as f:
            for result in results:
                f.write(json.dumps(result.to_dict()) + "\n")

        logger.info(f"Benchmark results saved to: {output_file}")

    return results