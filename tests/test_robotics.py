"""
Tests for the ShugoCore robotics integration.

Covers ROS 2 interface, MoveIt 2 planner, Gazebo simulation, and the
robotics execution handler — all in stub mode (no real ROS 2/Gazebo required).
"""

import math
import unittest

from ros2_interface import (
    JointState, JointTrajectory, JointTrajectoryPoint, LaserScan,
    Pose, Twist, Vector3, StubROS2Interface, create_ros2_interface,
    sanitize_twist, validate_joint_trajectory,
)
from moveit_planner import StubMoveItPlanner, _parse_plan_result, create_moveit_planner
from gazebo_simulation import StubGazeboSimulation, create_gazebo_simulation
from robotics_handler import RoboticsExecutionHandler


class TestVector3(unittest.TestCase):
    def test_creation(self):
        v = Vector3(1.0, 2.0, 3.0)
        self.assertEqual(v.x, 1.0)
        self.assertEqual(v.y, 2.0)
        self.assertEqual(v.z, 3.0)

    def test_to_dict(self):
        v = Vector3(1.0, 2.0, 3.0)
        self.assertEqual(v.to_dict(), {"x": 1.0, "y": 2.0, "z": 3.0})

    def test_default(self):
        v = Vector3()
        self.assertEqual(v.x, 0.0)


class TestTwist(unittest.TestCase):
    def test_creation(self):
        t = Twist(linear=Vector3(1, 0, 0), angular=Vector3(0, 0, 0.5))
        self.assertEqual(t.linear.x, 1.0)
        self.assertEqual(t.angular.z, 0.5)

    def test_to_dict(self):
        t = Twist(linear=Vector3(1, 0, 0))
        d = t.to_dict()
        self.assertEqual(d["linear"]["x"], 1.0)


class TestStubROS2Interface(unittest.TestCase):
    def test_not_available(self):
        ros = StubROS2Interface()
        self.assertFalse(ros.is_available())

    def test_publish_and_retrieve(self):
        ros = StubROS2Interface()
        ros.create_publisher("/cmd_vel", "Twist")
        ros.publish("/cmd_vel", Twist(linear=Vector3(1, 0, 0)))
        msgs = ros.get_published_messages("/cmd_vel")
        self.assertEqual(len(msgs), 1)
        self.assertEqual(msgs[0]["message"].linear.x, 1.0)

    def test_nan_rejection(self):
        ros = StubROS2Interface()
        ros.create_publisher("/cmd_vel", "Twist")
        ros.publish("/cmd_vel", Twist(linear=Vector3(float("nan"), 0, 0)))
        msgs = ros.get_published_messages("/cmd_vel")
        self.assertEqual(msgs[0]["message"].linear.x, 0.0)

    def test_inf_rejection(self):
        ros = StubROS2Interface()
        ros.create_publisher("/cmd_vel", "Twist")
        ros.publish("/cmd_vel", Twist(linear=Vector3(float("inf"), 0, 0)))
        msgs = ros.get_published_messages("/cmd_vel")
        self.assertEqual(msgs[0]["message"].linear.x, 0.0)

    def test_rate_limiting(self):
        ros = StubROS2Interface(rate_limit_hz=10.0)
        ros.create_publisher("/cmd_vel", "Twist")
        ros.publish("/cmd_vel", Twist(linear=Vector3(1, 0, 0)))
        ros.publish("/cmd_vel", Twist(linear=Vector3(2, 0, 0)))
        msgs = ros.get_published_messages("/cmd_vel")
        self.assertEqual(len(msgs), 1)

    def test_canned_joint_state(self):
        ros = StubROS2Interface()
        js = ros.get_canned_joint_state()
        self.assertEqual(len(js.name), 6)

    def test_canned_laser_scan(self):
        ros = StubROS2Interface()
        scan = ros.get_canned_laser_scan()
        self.assertEqual(len(scan.ranges), 360)

    def test_shutdown(self):
        ros = StubROS2Interface()
        ros.create_publisher("/cmd_vel", "Twist")
        ros.shutdown()
        self.assertEqual(len(ros._publishers), 0)

    def test_factory_returns_stub(self):
        ros = create_ros2_interface()
        self.assertIsInstance(ros, StubROS2Interface)


class TestStubMoveItPlanner(unittest.TestCase):
    def test_not_available(self):
        planner = StubMoveItPlanner()
        self.assertFalse(planner.is_available())

    def test_compute_ik(self):
        planner = StubMoveItPlanner()
        pose = Pose(position=Vector3(0.5, 0.3, 0.2))
        result = planner.compute_ik(pose)
        self.assertIsNotNone(result)
        self.assertEqual(len(result.position), 6)

    def test_compute_ik_none_pose(self):
        planner = StubMoveItPlanner()
        self.assertIsNone(planner.compute_ik(None))

    def test_plan_to_pose(self):
        planner = StubMoveItPlanner()
        pose = Pose(position=Vector3(0.5, 0.3, 0.2))
        traj = planner.plan_to_pose(pose)
        self.assertIsNotNone(traj)
        self.assertEqual(len(traj.joint_names), 6)

    def test_plan_to_joint_target(self):
        planner = StubMoveItPlanner()
        traj = planner.plan_to_joint_target({"joint_1": 0.5, "joint_2": -0.3})
        self.assertIsNotNone(traj)
        self.assertEqual(traj.points[0].positions[0], 0.5)

    def test_plan_cartesian_path(self):
        planner = StubMoveItPlanner()
        waypoints = [Pose(position=Vector3(0.1, 0, 0)), Pose(position=Vector3(0.2, 0, 0))]
        traj = planner.plan_cartesian_path(waypoints)
        self.assertIsNotNone(traj)
        self.assertEqual(len(traj.points), 2)

    def test_joint_limit_clamping(self):
        planner = StubMoveItPlanner()
        traj = planner.plan_to_joint_target({"joint_1": 999.0})
        self.assertLessEqual(traj.points[0].positions[0], math.pi)

    def test_validate_trajectory_valid(self):
        planner = StubMoveItPlanner()
        traj = planner.plan_to_joint_target({"joint_1": 0.5})
        valid, reason = planner.validate_trajectory(traj)
        self.assertTrue(valid)

    def test_validate_trajectory_empty(self):
        planner = StubMoveItPlanner()
        valid, reason = planner.validate_trajectory(JointTrajectory())
        self.assertFalse(valid)

    def test_collision_detection(self):
        planner = StubMoveItPlanner()
        planner.add_collision_object("box", {"x": 0.5, "y": 0.5}, {"x": 0.2, "y": 0.2})
        traj = JointTrajectory(
            joint_names=["joint_1", "joint_2"],
            points=[JointTrajectoryPoint(positions=[0.5, 0.5])],
        )
        self.assertTrue(planner.check_collision(traj))

    def test_no_collision(self):
        planner = StubMoveItPlanner()
        traj = JointTrajectory(
            joint_names=["joint_1", "joint_2"],
            points=[JointTrajectoryPoint(positions=[0.5, 0.5])],
        )
        self.assertFalse(planner.check_collision(traj))

    def test_factory_returns_stub(self):
        planner = create_moveit_planner()
        self.assertIsInstance(planner, StubMoveItPlanner)


class TestStubGazeboSimulation(unittest.TestCase):
    def test_not_available(self):
        gz = StubGazeboSimulation()
        self.assertFalse(gz.is_available())

    def test_spawn_and_get_pose(self):
        gz = StubGazeboSimulation()
        pose = Pose(position=Vector3(1, 2, 3))
        self.assertTrue(gz.spawn_urdf("robot.urdf", pose, "robot1"))
        result = gz.get_model_pose("robot1")
        self.assertEqual(result.position.x, 1.0)

    def test_spawn_from_string(self):
        gz = StubGazeboSimulation()
        pose = Pose()
        self.assertTrue(gz.spawn_from_string("<urdf/>", pose, "robot2"))

    def test_remove_model(self):
        gz = StubGazeboSimulation()
        gz.spawn_urdf("robot.urdf", Pose(), "robot1")
        self.assertTrue(gz.remove_model("robot1"))
        self.assertIsNone(gz.get_model_pose("robot1"))

    def test_reset_world(self):
        gz = StubGazeboSimulation()
        gz.spawn_urdf("robot.urdf", Pose(), "robot1")
        gz.reset_world()
        self.assertEqual(len(gz._models), 0)

    def test_max_models_limit(self):
        gz = StubGazeboSimulation(max_models=2)
        gz.spawn_urdf("r1.urdf", Pose(), "r1")
        gz.spawn_urdf("r2.urdf", Pose(), "r2")
        self.assertFalse(gz.spawn_urdf("r3.urdf", Pose(), "r3"))

    def test_get_sensor_data(self):
        gz = StubGazeboSimulation()
        scan = gz.get_sensor_data("/scan")
        self.assertEqual(len(scan.ranges), 360)

    def test_step(self):
        gz = StubGazeboSimulation()
        gz.step(10)
        self.assertGreater(gz._sim_time, 0)

    def test_factory_returns_stub(self):
        gz = create_gazebo_simulation()
        self.assertIsInstance(gz, StubGazeboSimulation)


class TestRoboticsExecutionHandler(unittest.TestCase):
    def test_navigate(self):
        h = RoboticsExecutionHandler()
        result = h.handle({"action_type": "robot_navigate", "params": {"linear_x": 0.5}})
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["action"], "navigate")

    def test_navigate_velocity_clamping(self):
        h = RoboticsExecutionHandler(max_linear_velocity=0.5)
        result = h.handle({"action_type": "robot_navigate", "params": {"linear_x": 999.0}})
        self.assertEqual(result["status"], "success")
        self.assertLessEqual(result["twist"]["linear"]["x"], 0.5)

    def test_manipulate(self):
        h = RoboticsExecutionHandler()
        result = h.handle({"action_type": "robot_manipulate", "params": {"target": {"x": 0.5, "y": 0.3, "z": 0.2}}})
        self.assertEqual(result["status"], "success")

    def test_manipulate_out_of_bounds(self):
        h = RoboticsExecutionHandler()
        result = h.handle({"action_type": "robot_manipulate", "params": {"target": {"x": 99, "y": 0, "z": 0}}})
        self.assertEqual(result["status"], "refused")

    def test_manipulate_no_target(self):
        h = RoboticsExecutionHandler()
        result = h.handle({"action_type": "robot_manipulate", "params": {}})
        self.assertEqual(result["status"], "error")

    def test_gripper(self):
        h = RoboticsExecutionHandler()
        result = h.handle({"action_type": "robot_gripper", "params": {"position": 0.5}})
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["position"], 0.5)

    def test_emergency_stop(self):
        h = RoboticsExecutionHandler()
        result = h.handle({"action_type": "robot_stop", "params": {}})
        self.assertEqual(result["status"], "success")
        self.assertTrue(h.is_emergency_stopped())

    def test_reset_emergency_stop(self):
        h = RoboticsExecutionHandler()
        h.handle({"action_type": "robot_stop", "params": {}})
        result = h.reset_emergency_stop(reset_by="operator")
        self.assertEqual(result["status"], "success")
        self.assertFalse(h.is_emergency_stopped())

    def test_query_state(self):
        h = RoboticsExecutionHandler()
        result = h.handle({"action_type": "robot_query_state", "params": {"model": "robot"}})
        self.assertEqual(result["status"], "success")

    def test_scan(self):
        h = RoboticsExecutionHandler()
        result = h.handle({"action_type": "robot_scan", "params": {}})
        self.assertEqual(result["status"], "success")
        self.assertEqual(len(result["scan"]), 360)

    def test_unknown_action(self):
        h = RoboticsExecutionHandler()
        result = h.handle({"action_type": "robot_fly", "params": {}})
        self.assertEqual(result["status"], "error")

    def test_watchdog(self):
        h = RoboticsExecutionHandler(watchdog_timeout=0.1)
        h.handle({"action_type": "robot_navigate", "params": {"linear_x": 0.1}})
        self.assertFalse(h.check_watchdog())
        import time
        time.sleep(0.15)
        self.assertTrue(h.check_watchdog())


class TestWatchdogAutoStop(unittest.TestCase):
    """Test that the watchdog auto-triggers emergency stop on expiry."""

    def test_watchdog_auto_stop(self):
        h = RoboticsExecutionHandler(watchdog_timeout=0.2)
        h.start_watchdog()
        h.handle({"action_type": "robot_navigate", "params": {"linear_x": 0.1}})
        self.assertFalse(h.is_emergency_stopped())
        import time
        time.sleep(0.6)  # wait for watchdog to expire and fire (0.5s check interval)
        self.assertTrue(h.is_emergency_stopped())
        h.shutdown()

    def test_watchdog_stopped_by_command(self):
        h = RoboticsExecutionHandler(watchdog_timeout=0.3)
        h.start_watchdog()
        h.handle({"action_type": "robot_navigate", "params": {"linear_x": 0.1}})
        import time
        time.sleep(0.1)
        h.handle({"action_type": "robot_navigate", "params": {"linear_x": 0.2}})  # resets watchdog
        time.sleep(0.1)
        self.assertFalse(h.is_emergency_stopped())  # not expired yet
        h.shutdown()

    def test_stop_watchdog(self):
        h = RoboticsExecutionHandler(watchdog_timeout=0.1)
        h.start_watchdog()
        self.assertTrue(h._watchdog_thread.is_alive())
        h.stop_watchdog()
        self.assertIsNone(h._watchdog_thread)


class TestEmergencyStopDuringOperation(unittest.TestCase):
    """Test emergency stop during active operation."""

    def test_emergency_stop_during_navigation(self):
        h = RoboticsExecutionHandler()
        h.handle({"action_type": "robot_navigate", "params": {"linear_x": 1.0}})
        result = h.handle({"action_type": "robot_stop", "params": {}})
        self.assertEqual(result["status"], "success")
        self.assertTrue(h.is_emergency_stopped())
        # Verify zero velocity was published
        msgs = h._ros2.get_published_messages("/cmd_vel")
        self.assertEqual(msgs[-1]["message"].linear.x, 0.0)

    def test_emergency_stop_cancels_moveit_goals(self):
        h = RoboticsExecutionHandler()
        h.handle({"action_type": "robot_manipulate", "params": {"target": {"x": 0.5, "y": 0.3, "z": 0.2}}})
        with self.assertLogs("moveit_planner", level="INFO") as cm:
            h.handle({"action_type": "robot_stop", "params": {}})
        self.assertTrue(any("cancelled all goals" in msg for msg in cm.output))

    def test_safety_action_bypasses_gates(self):
        """robot_stop should be in SAFETY_ACTION_TYPES, not ACTION_TYPES."""
        from robotics_handler import ROBOTICS_SAFETY_ACTION_TYPES, ROBOTICS_ACTION_TYPES
        self.assertIn("robot_stop", ROBOTICS_SAFETY_ACTION_TYPES)
        self.assertNotIn("robot_stop", ROBOTICS_ACTION_TYPES)


class TestTrajectoryExecution(unittest.TestCase):
    """Test that trajectories are actually executed after planning."""

    def test_manipulate_publishes_trajectory(self):
        h = RoboticsExecutionHandler()
        h.handle({"action_type": "robot_manipulate", "params": {"target": {"x": 0.5, "y": 0.3, "z": 0.2}}})
        msgs = h._ros2.get_published_messages("/arm_controller/follow_joint_trajectory")
        self.assertGreaterEqual(len(msgs), 1)
        self.assertEqual(msgs[0]["message"].joint_names, ["joint_1", "joint_2", "joint_3", "joint_4", "joint_5", "joint_6"])


class TestRoboticsSecurityIntegration(unittest.TestCase):
    """Test that robotics invariants and fallback triggers are registered."""

    def test_robotics_invariants_exist(self):
        from memory_system import CoreIdentity
        invariants = CoreIdentity().invariants()
        self.assertIn("physical_safety", invariants)
        self.assertIn("velocity_limit", invariants)
        self.assertIn("workspace_boundary", invariants)
        self.assertIn("emergency_stop", invariants)
        self.assertIn("human_safety", invariants)

    def test_robotics_action_types_registered(self):
        from policy import ROBOTICS_ACTION_TYPES, ROBOTICS_SAFETY_ACTION_TYPES, ROBOTICS_READ_ACTION_TYPES
        self.assertIn("robot_navigate", ROBOTICS_ACTION_TYPES)
        self.assertIn("robot_manipulate", ROBOTICS_ACTION_TYPES)
        self.assertIn("robot_stop", ROBOTICS_SAFETY_ACTION_TYPES)
        self.assertIn("robot_query_state", ROBOTICS_READ_ACTION_TYPES)

    def test_robotics_capabilities_exist(self):
        from policy import CapabilityRegistry
        reg = CapabilityRegistry()
        self.assertEqual(reg.max_linear_velocity, 1.0)
        self.assertEqual(reg.workspace_bounds["x"], (-2.0, 2.0))
        self.assertEqual(reg.robot_hosts, ["localhost", "127.0.0.1"])

    def test_robotics_fallback_triggers_exist(self):
        from fallbacks import DEFAULT_SEVERITIES
        self.assertEqual(DEFAULT_SEVERITIES["collision_detected"], "halt")
        self.assertEqual(DEFAULT_SEVERITIES["emergency_stop_activated"], "halt")
        self.assertEqual(DEFAULT_SEVERITIES["velocity_exceeded"], "safe_state")
        self.assertEqual(DEFAULT_SEVERITIES["ros2_connection_lost"], "safe_state")

    def test_robotics_states_exist(self):
        from state_machine import AgentState
        self.assertEqual(AgentState.PLANNING.value, "planning")
        self.assertEqual(AgentState.PHYSICAL_EXECUTING.value, "physical_executing")
        self.assertEqual(AgentState.EMERGENCY_STOP.value, "emergency_stop")

    def test_robotics_transitions(self):
        from state_machine import AgentState, _ALLOWED
        self.assertIn(AgentState.PLANNING, _ALLOWED[AgentState.DECIDING])
        self.assertIn(AgentState.PHYSICAL_EXECUTING, _ALLOWED[AgentState.PLANNING])
        self.assertIn(AgentState.EMERGENCY_STOP, _ALLOWED[AgentState.PHYSICAL_EXECUTING])
        self.assertIn(AgentState.IDLE, _ALLOWED[AgentState.EMERGENCY_STOP])


class TestContinuousNavigation(unittest.TestCase):
    """Test continuous command mode for mobile base."""

    def test_start_and_stop_continuous(self):
        h = RoboticsExecutionHandler()
        h.start_continuous_navigate(linear_x=0.5, angular_z=0.1, rate_hz=10.0)
        self.assertTrue(h._continuous_thread.is_alive())
        import time
        time.sleep(0.25)  # let it publish a few times
        h.stop_continuous_navigate()
        msgs = h._ros2.get_published_messages("/cmd_vel")
        self.assertGreaterEqual(len(msgs), 2)  # should have published multiple times
        # Last message should be zero velocity (stop)
        self.assertEqual(msgs[-1]["message"].linear.x, 0.0)

    def test_update_continuous(self):
        h = RoboticsExecutionHandler()
        h.start_continuous_navigate(linear_x=0.3, rate_hz=10.0)
        h.update_continuous_navigate(linear_x=0.8, angular_z=0.2)
        self.assertEqual(h._continuous_twist.linear.x, 0.8)
        self.assertEqual(h._continuous_twist.angular.z, 0.2)
        h.stop_continuous_navigate()

    def test_velocity_clamping_in_continuous(self):
        h = RoboticsExecutionHandler(max_linear_velocity=0.5)
        h.start_continuous_navigate(linear_x=999.0, rate_hz=10.0)
        self.assertLessEqual(h._continuous_twist.linear.x, 0.5)
        h.stop_continuous_navigate()


class TestSensorFeedback(unittest.TestCase):
    """Test sensor feedback loop for obstacle detection."""

    def test_sensor_feedback_detects_obstacle(self):
        h = RoboticsExecutionHandler()
        # Spawn a model close to simulate obstacle
        from ros2_interface import Pose, Vector3
        h._gazebo.spawn_from_string("<urdf/>", Pose(position=Vector3(0.2, 0, 0)), "obstacle")
        h.start_sensor_feedback(obstacle_distance=0.5, check_interval=0.05)
        import time
        time.sleep(0.15)  # let the sensor loop check
        # Note: stub returns fixed ranges, so this tests the loop runs
        h.stop_sensor_feedback()
        h.shutdown()

    def test_start_and_stop_sensor_feedback(self):
        h = RoboticsExecutionHandler()
        h.start_sensor_feedback(obstacle_distance=0.5)
        self.assertTrue(h._sensor_thread.is_alive())
        h.stop_sensor_feedback()
        self.assertIsNone(h._sensor_thread)


class TestTrajectoryExecutionMonitoring(unittest.TestCase):
    """Test trajectory execution status monitoring."""

    def test_get_execution_status(self):
        from moveit_planner import create_moveit_planner
        planner = create_moveit_planner()
        status = planner.get_execution_status()
        self.assertIn("state", status)
        self.assertIn("progress", status)
        self.assertIn("current_positions", status)

    def test_execution_status_after_plan(self):
        h = RoboticsExecutionHandler()
        h.handle({"action_type": "robot_manipulate", "params": {"target": {"x": 0.5, "y": 0.3, "z": 0.2}}})
        status = h._moveit.get_execution_status()
        self.assertEqual(status["state"], "idle")


class TestDecisionEngineIntegration(unittest.TestCase):
    """Test that the robotics handler integrates with DecisionEngine."""

    def test_decision_engine_accepts_robotics_handler(self):
        from decision_engine import DecisionEngine
        from robotics_handler import RoboticsExecutionHandler
        handler = RoboticsExecutionHandler()
        models = [{"id": "test", "type": "text", "weight": 1.0, "backend": {"type": "stub"}}]
        engine = DecisionEngine(models, {"type": "chroma"}, robotics_handler=handler)
        self.assertIsNotNone(engine.robotics_handler)
        handler.shutdown()

    def test_decision_engine_without_robotics(self):
        from decision_engine import DecisionEngine
        models = [{"id": "test", "type": "text", "weight": 1.0, "backend": {"type": "stub"}}]
        engine = DecisionEngine(models, {"type": "chroma"})
        self.assertIsNone(engine.robotics_handler)




class TestPhase3Quality(unittest.TestCase):
    """Phase 3 hardening: shared validators, plan parsing, stub IK, sim integration."""

    # -- shared message validators (3.3) -------------------------------------

    def test_sanitize_twist_replaces_nan_inf(self):
        twist = Twist(linear=Vector3(float("nan"), float("inf"), 0.5),
                      angular=Vector3(0.0, float("-inf"), 0.1))
        clean = sanitize_twist(twist)
        self.assertEqual(clean.linear.x, 0.0)
        self.assertEqual(clean.linear.y, 0.0)
        self.assertEqual(clean.linear.z, 0.5)
        self.assertEqual(clean.angular.y, 0.0)
        self.assertEqual(clean.angular.z, 0.1)

    def test_sanitize_twist_clamps_extremes(self):
        twist = Twist(linear=Vector3(1e9, -1e9, 0.0), angular=Vector3(0, 0, 0))
        clean = sanitize_twist(twist)
        self.assertEqual(clean.linear.x, 100.0)
        self.assertEqual(clean.linear.y, -100.0)

    def test_validate_joint_trajectory_rejects_empty(self):
        traj = JointTrajectory(joint_names=[], points=[])
        with self.assertRaises(ValueError):
            validate_joint_trajectory(traj)

    def test_validate_joint_trajectory_rejects_non_monotonic_time(self):
        traj = JointTrajectory(
            joint_names=["j1"],
            points=[JointTrajectoryPoint(positions=[0.0], time_from_start=1.0),
                    JointTrajectoryPoint(positions=[0.5], time_from_start=0.5)],
        )
        with self.assertRaises(ValueError):
            validate_joint_trajectory(traj)

    def test_validate_joint_trajectory_zeroes_nan_positions(self):
        traj = JointTrajectory(
            joint_names=["j1", "j2"],
            points=[JointTrajectoryPoint(positions=[float("nan"), 0.3],
                                         time_from_start=0.0),
                    JointTrajectoryPoint(positions=[float("inf"), 0.6],
                                         time_from_start=1.0)],
        )
        result = validate_joint_trajectory(traj)
        self.assertEqual(result.points[0].positions, [0.0, 0.3])
        self.assertEqual(result.points[1].positions, [0.0, 0.6])

    def test_validate_joint_trajectory_accepts_valid(self):
        traj = JointTrajectory(
            joint_names=["j1"],
            points=[JointTrajectoryPoint(positions=[0.0], time_from_start=0.0),
                    JointTrajectoryPoint(positions=[1.0], time_from_start=2.0)],
        )
        self.assertIs(validate_joint_trajectory(traj), traj)

    # -- MoveIt plan-result parsing (3.4) ------------------------------------

    def test_parse_plan_result_tuple_success(self):
        traj = JointTrajectory(joint_names=["j1"], points=[])
        self.assertIs(_parse_plan_result((True, traj, 0.5)), traj)

    def test_parse_plan_result_tuple_failure(self):
        traj = JointTrajectory(joint_names=["j1"], points=[])
        self.assertIsNone(_parse_plan_result((False, traj, 0.5)))

    def test_parse_plan_result_bare_trajectory(self):
        class FakeRobotTrajectory:
            joint_trajectory = JointTrajectory(joint_names=["j1"], points=[])
        fake = FakeRobotTrajectory()
        # A bare RobotTrajectory (carries .joint_trajectory) is returned as-is;
        # downstream code extracts .joint_trajectory when converting to our type.
        self.assertIs(_parse_plan_result(fake), fake)

    def test_parse_plan_result_bare_trajectory_empty_inner(self):
        class FakeEmptyTrajectory:
            joint_trajectory = None
        self.assertIsNone(_parse_plan_result(FakeEmptyTrajectory()))

    def test_parse_plan_result_none_and_garbage(self):
        self.assertIsNone(_parse_plan_result(None))
        self.assertIsNone(_parse_plan_result(()))
        self.assertIsNone(_parse_plan_result((True, None)))


    # -- stub IK quality (3.1) -----------------------------------------------

    def test_stub_ik_maps_pose_axes_to_distinct_joints(self):
        planner = StubMoveItPlanner()
        js = planner.compute_ik(Pose(position=Vector3(1.0, 0.0, 0.0)))
        js_y = planner.compute_ik(Pose(position=Vector3(0.0, 1.0, 0.0)))
        js_z = planner.compute_ik(Pose(position=Vector3(0.0, 0.0, 1.0)))
        # Each pose axis drives a different joint: verify cross-independence
        self.assertNotAlmostEqual(js.position[0], js_y.position[0])
        self.assertNotAlmostEqual(js_y.position[1], js_z.position[1])
        self.assertAlmostEqual(js_z.position[2], 0.3)  # 1.0 * scale_z

    def test_stub_ik_respects_joint_limits(self):
        planner = StubMoveItPlanner()
        huge = Pose(position=Vector3(1e6, 1e6, 1e6))
        js = planner.compute_ik(huge)
        for name, pos in zip(js.name, js.position):
            lo, hi = planner._joint_limits[name]
            self.assertGreaterEqual(pos, lo)
            self.assertLessEqual(pos, hi)

    def test_stub_ik_none_pose_returns_none(self):
        self.assertIsNone(StubMoveItPlanner().compute_ik(None))

    # -- Gazebo command integration (3.2) ------------------------------------

    def _spawn_robot(self, sim):
        urdf = "<robot name='test'><link name='base'/></robot>"
        pose = Pose(position=Vector3(0.0, 0.0, 0.0))
        self.assertTrue(sim.spawn_from_string(urdf, pose, "test_robot"))

    def test_gazebo_applies_twist_on_step(self):
        sim = StubGazeboSimulation()
        self._spawn_robot(sim)
        sim.apply_command(Twist(linear=Vector3(0.5, 0.0, 0.0),
                                angular=Vector3(0.0, 0.0, 0.0)))
        sim.step(iterations=100)  # dt = 0.01 * 100 = 1.0s
        pose = sim.get_model_pose("test_robot")
        self.assertAlmostEqual(pose.position.x, 0.5, places=6)
        self.assertAlmostEqual(pose.position.y, 0.0, places=6)
        sim.shutdown()

    def test_gazebo_displacement_proportional_to_iterations(self):
        sim = StubGazeboSimulation()
        self._spawn_robot(sim)
        sim.apply_command(Twist(linear=Vector3(1.0, 0.0, 0.0),
                                angular=Vector3(0, 0, 0)))
        sim.step(iterations=10)
        x_10 = sim.get_model_pose("test_robot").position.x
        sim.step(iterations=20)
        x_30 = sim.get_model_pose("test_robot").position.x
        delta_20 = x_30 - x_10
        self.assertAlmostEqual(delta_20 / max(x_10, 1e-9), 2.0, places=6)
        sim.shutdown()

    def test_gazebo_angular_z_rotates_orientation(self):
        sim = StubGazeboSimulation()
        self._spawn_robot(sim)
        sim.apply_command(Twist(linear=Vector3(0, 0, 0),
                                angular=Vector3(0.0, 0.0, 1.0)))
        sim.step(iterations=50)  # dt = 0.5s
        pose = sim.get_model_pose("test_robot")
        self.assertAlmostEqual(pose.orientation.z, 0.5, places=6)
        sim.shutdown()

    def test_gazebo_paused_does_not_integrate(self):
        sim = StubGazeboSimulation()
        self._spawn_robot(sim)
        sim.pause()
        sim.apply_command(Twist(linear=Vector3(1.0, 0.0, 0.0),
                                angular=Vector3(0, 0, 0)))
        sim.step(iterations=100)
        pose = sim.get_model_pose("test_robot")
        self.assertAlmostEqual(pose.position.x, 0.0, places=6)
        sim.unpause()
        sim.step(iterations=100)
        self.assertAlmostEqual(sim.get_model_pose("test_robot").position.x,
                               1.0, places=6)
        sim.shutdown()

if __name__ == "__main__":
    unittest.main()