"""
Tests for the ShugoCore simulation framework.

Tests run in stub mode (no MuJoCo required).
"""

import unittest

import numpy as np

from simulation.base import SimulationResult
from simulation.stub_sim import StubSimulation
from simulation.scenarios import WalkToTarget, BalanceTest, EmergencyStop, run_benchmark
from simulation.robots import BerkeleyHumanoidLite, Reachy2, UnitreeG1, get_robot_model, list_robots


class TestStubSimulation(unittest.TestCase):
    """Tests for the stub simulation backend."""

    def test_initialization(self):
        sim = StubSimulation(num_joints=24)
        self.assertFalse(sim.is_available())

    def test_reset(self):
        sim = StubSimulation(num_joints=6)
        sim.set_joint_commands(np.array([0.1, 0.1, 0.1, 0.1, 0.1, 0.1]))
        sim.step()
        sim.reset()
        self.assertEqual(sim.step_count, 0)
        self.assertEqual(sim.time, 0.0)

    def test_joint_integration(self):
        sim = StubSimulation(num_joints=6)
        commands = np.array([0.1, 0.0, 0.0, 0.0, 0.0, 0.0])
        sim.set_joint_commands(commands)
        sim.step(dt=0.01)
        positions = sim.get_joint_positions()
        self.assertAlmostEqual(positions[0], 0.001, places=5)

    def test_base_position_integration(self):
        sim = StubSimulation(num_joints=6)
        commands = np.array([0.5, 0.0, 0.0, 0.0, 0.0, 0.0])
        sim.set_joint_commands(commands)
        for _ in range(100):
            sim.step(dt=0.01)
        pos = sim.get_base_position()
        self.assertGreater(pos[0], 0.0)

    def test_imu_data(self):
        sim = StubSimulation(num_joints=6)
        imu = sim.get_imu_data()
        self.assertIn("accel", imu)
        self.assertIn("gyro", imu)
        self.assertEqual(len(imu["accel"]), 3)
        self.assertEqual(len(imu["gyro"]), 3)

class TestBerkeleyHumanoidLite(unittest.TestCase):
    """Tests for Berkeley Humanoid Lite robot model."""

    def test_creation(self):
        robot = BerkeleyHumanoidLite()
        self.assertEqual(robot.name, "berkeley_humanoid_lite")
        self.assertEqual(robot.num_joints, 24)

    def test_joint_names(self):
        robot = BerkeleyHumanoidLite()
        names = robot.get_joint_names()
        self.assertEqual(len(names), 24)
        self.assertIn("left_hip_pitch", names)
        self.assertIn("right_knee", names)

    def test_joint_limits(self):
        robot = BerkeleyHumanoidLite()
        lower, upper = robot.get_joint_limits_array()
        self.assertEqual(len(lower), 24)
        self.assertEqual(len(upper), 24)

    def test_initial_pose(self):
        robot = BerkeleyHumanoidLite()
        pose = robot.get_initial_joint_positions()
        self.assertIsInstance(pose, dict)
        self.assertGreater(len(pose), 0)

    def test_serialization(self):
        robot = BerkeleyHumanoidLite()
        config = robot.to_dict()
        self.assertEqual(config["name"], "berkeley_humanoid_lite")
        self.assertTrue(config["has_imu"])


class TestReachy2(unittest.TestCase):
    """Tests for Reachy 2 robot model."""

    def test_creation(self):
        robot = Reachy2()
        self.assertEqual(robot.name, "reachy2")
        self.assertEqual(robot.num_joints, 20)

    def test_joint_names(self):
        robot = Reachy2()
        names = robot.get_joint_names()
        self.assertIn("neck_yaw", names)
        self.assertIn("l_shoulder_pitch", names)


class TestUnitreeG1(unittest.TestCase):
    """Tests for Unitree G1 robot model."""

    def test_creation(self):
        robot = UnitreeG1()
        self.assertEqual(robot.name, "unitree_g1")
        self.assertEqual(robot.num_joints, 23)

    def test_joint_names(self):
        robot = UnitreeG1()
        names = robot.get_joint_names()
        self.assertIn("left_hip_pitch", names)
        self.assertIn("waist_yaw", names)


class TestRobotRegistry(unittest.TestCase):
    """Tests for robot model registry."""

    def test_list_robots(self):
        robots = list_robots()
        self.assertIn("berkeley_humanoid_lite", robots)
        self.assertIn("reachy2", robots)
        self.assertIn("unitree_g1", robots)

    def test_get_robot(self):
        robot = get_robot_model("berkeley_humanoid_lite")
        self.assertIsInstance(robot, BerkeleyHumanoidLite)

    def test_get_unknown_robot(self):
        with self.assertRaises(ValueError):
            get_robot_model("unknown_robot")


class TestSimulationResult(unittest.TestCase):
    """Tests for simulation result serialization."""

    def test_to_dict(self):
        result = SimulationResult(
            scenario="TestScenario",
            robot="test_robot",
            seed=42,
            steps=100,
            duration_seconds=1.0,
            success=True,
            metrics={"distance": 0.5},
        )
        d = result.to_dict()
        self.assertEqual(d["scenario"], "TestScenario")
        self.assertEqual(d["robot"], "test_robot")
        self.assertTrue(d["success"])


class TestScenarios(unittest.TestCase):
    """Tests for simulation scenarios."""

    def test_walk_to_target(self):
        robot = BerkeleyHumanoidLite()
        sim = StubSimulation(num_joints=24)
        scenario = WalkToTarget(robot, sim, max_steps=100)
        result = scenario.run()
        self.assertIsInstance(result, SimulationResult)
        self.assertEqual(result.scenario, "WalkToTarget")

    def test_balance_test(self):
        robot = BerkeleyHumanoidLite()
        sim = StubSimulation(num_joints=24)
        scenario = BalanceTest(robot, sim, max_steps=100)
        result = scenario.run()
        self.assertIsInstance(result, SimulationResult)
        self.assertEqual(result.scenario, "BalanceTest")

    def test_emergency_stop(self):
        robot = BerkeleyHumanoidLite()
        sim = StubSimulation(num_joints=24)
        scenario = EmergencyStop(robot, sim, max_steps=100)
        result = scenario.run()
        self.assertIsInstance(result, SimulationResult)
        self.assertEqual(result.scenario, "EmergencyStop")


if __name__ == "__main__":
    unittest.main()