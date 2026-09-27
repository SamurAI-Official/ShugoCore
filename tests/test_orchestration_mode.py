"""Capacity decides agency: a node hands orchestration up when the hive has a
better orchestrator, and stops deciding for itself when it has no headroom.

The mode is computed from measured capacity (thermal, free memory, battery) plus
the election's own verdict, and the behaviour that matters is checked directly:
a subordinate tick never reaches the engine, so no model is asked to propose
anything on a node that is not orchestrating.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import shugocore_agent as sa  # noqa: E402


class FakeElection:
    """Stand-in for MeshElection: one rival node outranks us, or nobody is live."""

    def __init__(self, node_id="android-test", priority=500, primary=None):
        self.node_id = node_id
        self.priority = priority
        self._primary = primary
        self._peers = []

    def tick(self):
        role = "primary" if self._primary in (None, self.node_id) else "follower"
        return {"primary": self._primary, "role": role}

    def live_peers(self):
        return list(self._peers)

    def add(self, node_id, priority, mem):
        self._peers.append({"node_id": node_id, "priority": priority,
                            "mem_available_bytes": mem, "thermal_status": 0})


class ModeTestCase(unittest.TestCase):
    """_orchestration_mode() with injected capacity: no engine, no mesh."""

    def _probe(self, *, election=None, thermal=0, mem=512 * 1024 * 1024,
               battery=80, charging=False, policy=None):
        probe = sa.AndroidAgent.__new__(sa.AndroidAgent)
        probe.device_caps = "test-node"
        probe.telemetry = {"thermal_state": thermal, "is_charging": charging}
        probe.policy = dict(policy or {})
        probe.mesh_election = election
        probe._mesh_mem_headroom = lambda: mem
        probe._get_battery = lambda: battery
        return probe

    def test_no_election_is_self_sufficient(self):
        mode, why = self._probe()._orchestration_mode()
        self.assertEqual(mode, "standalone")
        self.assertIn("self-sufficient", why)

    def test_lease_holder_orchestrates(self):
        election = FakeElection(primary="android-test")
        mode, why = self._probe(election=election)._orchestration_mode()
        self.assertEqual(mode, "primary")
        self.assertIn("lease", why)

    def test_a_better_peer_takes_orchestration(self):
        election = FakeElection(primary="shugo-desktop")
        election.add("shugo-desktop", 10, 12 * 1024 ** 3)
        mode, why = self._probe(election=election)._orchestration_mode()
        self.assertEqual(mode, "subordinate")
        self.assertIn("shugo-desktop", why)
        self.assertIn("prio 10 vs 500", why)

    def test_thermal_critical_steps_down_even_alone(self):
        mode, why = self._probe(thermal=3)._orchestration_mode()
        self.assertEqual(mode, "degraded")
        self.assertIn("thermal 3", why)

    def test_headroom_floor_steps_down(self):
        mode, why = self._probe(mem=30 * 1024 * 1024)._orchestration_mode()
        self.assertEqual(mode, "degraded")
        self.assertIn("headroom", why)

    def test_flat_battery_steps_down_unless_charging(self):
        mode, _ = self._probe(battery=5)._orchestration_mode()
        self.assertEqual(mode, "degraded")
        mode, _ = self._probe(battery=5, charging=True)._orchestration_mode()
        self.assertEqual(mode, "standalone")

    def test_operator_override_wins_both_ways(self):
        mode, why = self._probe(policy={"orchestration": "full"},
                                thermal=3)._orchestration_mode()
        self.assertEqual(mode, "primary")
        self.assertIn("override", why)
        mode, why = self._probe(policy={"orchestration": "sensor_only"},
                                )._orchestration_mode()
        self.assertEqual(mode, "subordinate")
        self.assertIn("override", why)


class SubordinateTickTestCase(unittest.TestCase):
    """A subordinate tick must not ask the engine to propose anything."""

    @classmethod
    def setUpClass(cls):
        import tempfile
        cls.tmp = tempfile.mkdtemp(prefix="shugo_orch_test_")
        cls.agent = sa.create_agent(device_caps="orch-test",
                                    api_url="http://127.0.0.1:9",
                                    data_dir=cls.tmp)
        cls.calls = []
        if getattr(cls.agent, "engine", None) is not None:
            original = cls.agent.engine.execute_task

            def _spy(task):
                cls.calls.append(task)
                return original(task)

            cls.agent.engine.execute_task = _spy

    def setUp(self):
        self.calls.clear()

    def tearDown(self):
        try:
            self.agent.mesh_election = None
        except Exception:
            pass

    def test_subordinate_tick_skips_the_engine(self):
        election = FakeElection(primary="shugo-desktop")
        election.add("shugo-desktop", 10, 12 * 1024 ** 3)
        self.agent.mesh_election = election
        self.agent.tick()
        self.assertEqual(self.agent._orchestration["mode"], "subordinate")
        self.assertEqual(self.calls, [], "a subordinate node called the engine")
        self.assertEqual(self.agent._decision_source, "none")


if __name__ == "__main__":
    unittest.main()
