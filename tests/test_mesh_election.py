#!/usr/bin/env python3
"""Track 1 mesh primary election tests.

Covers the deterministic election rules (lowest priority wins, tie-break
on smallest node_id, thermal / headroom / pairing ineligibility, stale
heartbeat partition + re-merge) and the AndroidAgent primary-only
guardrail wiring (_mesh_may_act, side-effect refusals, status surface).
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mesh_election import MeshElection  # noqa: E402
from shugocore_agent import create_agent  # noqa: E402

DESKTOP_MEM = 8_000_000_000  # healthy desktop headroom


class TestElectionRules(unittest.TestCase):
    def test_lowest_priority_wins(self):
        """Desktop (priority 10) beats the Android custodian (500)."""
        e = MeshElection(node_id="android-A51", priority=500)
        e.observe_heartbeat({"node_id": "macbook", "priority": 10,
                             "mem_available_bytes": DESKTOP_MEM})
        e.local_heartbeat(thermal_status=0, mem_available_bytes=2_000_000)
        result = e.tick()
        self.assertEqual(result["primary"], "macbook")
        self.assertFalse(result["is_primary"])

    def test_tie_break_on_smallest_node_id(self):
        e = MeshElection(node_id="node-b", priority=100)
        e.observe_heartbeat({"node_id": "node-a", "priority": 100,
                             "mem_available_bytes": DESKTOP_MEM})
        e.local_heartbeat(thermal_status=0, mem_available_bytes=DESKTOP_MEM)
        self.assertEqual(e.tick()["primary"], "node-a")
        self.assertFalse(e.is_primary())

    def test_thermal_refused_node_cannot_win(self):
        """thermal_status >= THERMAL_REFUSE_STATUS (3) is ineligible."""
        e = MeshElection(node_id="android-A51", priority=500)
        e.observe_heartbeat({"node_id": "macbook", "priority": 10,
                             "mem_available_bytes": DESKTOP_MEM,
                             "thermal_status": 3})
        e.local_heartbeat(thermal_status=0, mem_available_bytes=2_000_000)
        self.assertEqual(e.tick()["primary"], "android-A51")
        self.assertTrue(e.is_primary())

    def test_zero_headroom_refused(self):
        e = MeshElection(node_id="android-A51", priority=500)
        e.observe_heartbeat({"node_id": "macbook", "priority": 10,
                             "mem_available_bytes": 0})
        e.local_heartbeat(thermal_status=0, mem_available_bytes=2_000_000)
        self.assertEqual(e.tick()["primary"], "android-A51")

    def test_unpaired_excluded(self):
        e = MeshElection(node_id="android-A51", priority=500)
        e.observe_heartbeat({"node_id": "macbook", "priority": 10,
                             "mem_available_bytes": DESKTOP_MEM,
                             "paired": False})
        e.local_heartbeat(thermal_status=0, mem_available_bytes=2_000_000)
        self.assertEqual(e.tick()["primary"], "android-A51")

    def test_stale_heartbeat_partition_and_remerge(self):
        """Past heartbeat_timeout_s the peer is dropped: no primary
        (standalone). Fresh heartbeats re-elect deterministically."""
        e = MeshElection(node_id="android-A51", priority=500)
        e.observe_heartbeat({"node_id": "macbook", "priority": 10,
                             "mem_available_bytes": DESKTOP_MEM}, now=0.0)
        e.local_heartbeat(thermal_status=0, mem_available_bytes=2_000_000)
        self.assertEqual(e.tick(now=1.0)["primary"], "macbook")
        # Partition: 40s of silence exceeds the 30s timeout — the stale
        # peer is dropped and the lone node keeps its own single-node
        # lease ("fail closed to standalone", no quorum required).
        result = e.tick(now=41.0)
        self.assertEqual(result["primary"], "android-A51")
        self.assertEqual(result["candidates"], ["android-A51"])
        # Re-merge: fresh advertisement (increasing seq — monotone
        # heartbeats) -> desktop re-elected deterministically.
        e.observe_heartbeat({"node_id": "macbook", "priority": 10,
                             "mem_available_bytes": DESKTOP_MEM,
                             "seq": 2}, now=42.0)
        self.assertEqual(e.tick(now=42.5)["primary"], "macbook")

    def test_status_reports_ineligible_reasons(self):
        e = MeshElection(node_id="android-A51", priority=500)
        e.observe_heartbeat({"node_id": "macbook", "priority": 10,
                             "mem_available_bytes": 0})
        status = e.status()
        self.assertEqual(status["ineligible"].get("macbook"), "no-headroom")
        self.assertEqual(status["role"], "unknown")

    def test_a_replayed_advertisement_cannot_rewrite_the_record(self):
        """Equal seq = a duplicate frame: it renews the lease, nothing else.

        A *lower* seq is a restarted peer and does replace the record; that rule
        lives in test_mesh_heartbeat.RestartedPeerTestCase.
        """
        e = MeshElection(node_id="android-A51", priority=500)
        self.assertTrue(e.observe_heartbeat({"node_id": "macbook", "seq": 5,
                                            "priority": 10,
                                            "thermal_status": 3}))
        # A replayed copy of an older frame, carrying different fields, must not
        # be able to overwrite what we already know about the peer.
        self.assertTrue(e.observe_heartbeat({"node_id": "macbook", "seq": 5,
                                            "priority": 999,
                                            "thermal_status": 0}))
        self.assertEqual(e.status()["nodes"]["macbook"]["seq"], 5)
        self.assertEqual(e.status()["nodes"]["macbook"]["priority"], 10)


class TestAgentGuardrail(unittest.TestCase):
    """AndroidAgent wiring: create_agent pass-through, follower refusals,
    standalone allowance, and the status surface."""

    @classmethod
    def setUpClass(cls):
        from shugocore_agent import create_agent
        cls._tmp = tempfile.mkdtemp(prefix="mesh_election_test_")
        cls.agent = create_agent(device_caps="A51",
                                 api_url="http://127.0.0.1:11434",
                                 data_dir=cls._tmp)

    @classmethod
    def tearDownClass(cls):
        try:
            cls.agent.cleanup()
        except Exception:
            pass

    def setUp(self):
        """Fresh election per test (pytest runs methods alphabetically;
        leftover heartbeats must not leak between them)."""
        from mesh_election import MeshElection
        self.agent.mesh_election = MeshElection(
            node_id="android-A51", priority=500)
        self.agent.telemetry = {}

    def test_create_agent_builds_election(self):
        probe = create_agent(device_caps="A51",
                             api_url="http://127.0.0.1:11434",
                             data_dir=tempfile.mkdtemp(
                                 prefix="mesh_election_probe_"))
        try:
            self.assertIsNotNone(probe.mesh_election)
            self.assertEqual(probe.mesh_election.node_id, "android-A51")
            self.assertEqual(probe.mesh_election.priority, 500)
        finally:
            try:
                probe.cleanup()
            except Exception:
                pass

    def test_standalone_node_may_act(self):
        """No live peers -> no primary -> standalone (may act)."""
        self.assertEqual(self.agent._mesh_role_label(), "standalone")
        self.assertTrue(self.agent._mesh_may_act("speak"))

    def test_follower_refuses_side_effects(self):
        """A healthy desktop peer (lower priority) holds the lease: the
        Android node is a follower and must refuse side effects."""
        e = self.agent.mesh_election
        e.drop_node("macbook")  # clear any state from earlier tests
        e.observe_heartbeat({"node_id": "macbook", "priority": 10,
                             "mem_available_bytes": DESKTOP_MEM})
        e.local_heartbeat(thermal_status=0, mem_available_bytes=0)
        self.assertEqual(e.tick()["primary"], "macbook")

        self.assertEqual(self.agent._mesh_role_label(), "follower")
        self.assertFalse(self.agent._mesh_may_act("speak"))

        result = self.agent._execute_speak({"params": {"text": "hello"}})
        self.assertEqual(result["status"], "refused")
        self.assertEqual(result["reason"], "mesh_follower")
        self.assertEqual(result["primary"], "macbook")

        result = self.agent._execute_ask_user(
            {"params": {"question": "hello?"}})
        self.assertEqual(result["status"], "refused")
        self.assertEqual(result["reason"], "mesh_follower")

        result = self.agent.speak_test("hello")
        self.assertEqual(result["status"], "refused")
        self.assertEqual(result["reason"], "mesh_follower")

        self.assertFalse(self.agent._speak_direct("hello"))

        status = self.agent.get_status()
        self.assertEqual(status["mesh_role"], "follower")
        self.assertEqual(status["mesh_primary"], "macbook")

    def test_primary_may_act(self):
        """Lowest-priority node with live peers holds the lease and may
        act (lone nodes are 'standalone', not 'primary')."""
        e = self.agent.mesh_election
        e.drop_node("macbook")
        e.observe_heartbeat({"node_id": "android-A52", "priority": 500,
                             "mem_available_bytes": 2_000_000})
        e.local_heartbeat(thermal_status=0, mem_available_bytes=2_000_000)
        self.assertEqual(e.tick()["primary"], "android-A51")
        self.assertTrue(self.agent._mesh_may_act("speak"))
        self.assertEqual(self.agent._mesh_role_label(), "primary")

    def test_thermal_critical_self_cannot_hold_lease(self):
        """thermal_state >= 3 (Kotlin ThermalMonitor) makes this node
        ineligible; with a healthy peer present it yields (follower),
        alone it stays standalone."""
        e = self.agent.mesh_election
        e.drop_node("macbook")
        self.agent.telemetry = {"thermal_state": 3,
                                "mem_available_bytes": 2_000_000}
        self.agent._mesh_heartbeat_tick()
        self.assertEqual(self.agent._mesh_role_label(), "standalone")
        e.observe_heartbeat({"node_id": "macbook", "priority": 10,
                             "mem_available_bytes": DESKTOP_MEM})
        self.assertEqual(self.agent._mesh_role_label(), "follower")
        self.assertFalse(self.agent._mesh_may_act("speak"))
        self.agent.telemetry = {}

    def test_update_mesh_peers_feeds_election(self):
        """Kotlin peer snapshots (device_id keyed) become election
        heartbeats via the telemetry path."""
        import json
        e = self.agent.mesh_election
        e.drop_node("macbook")
        self.agent.telemetry = {}
        snap = json.dumps({"mesh_peer_count": 1, "mesh_peers": [
            {"device_id": "macbook", "name": "MacBook", "role": "primary",
             "priority": 10, "mem_available_bytes": DESKTOP_MEM,
             "thermal_status": 0}]})
        self.agent.update_mesh_peers(snap)
        self.agent._mesh_heartbeat_tick()
        self.assertEqual(e.tick()["primary"], "macbook")
        self.assertEqual(self.agent._mesh_role_label(), "follower")
        self.agent.telemetry = {}


if __name__ == "__main__":
    unittest.main()
