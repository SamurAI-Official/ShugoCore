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
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import mesh_election as me  # noqa: E402
from mesh_election import MeshElection  # noqa: E402
from shugocore_agent import create_agent  # noqa: E402
import node_identity  # noqa: E402

DESKTOP_MEM = 8_000_000_000  # healthy desktop headroom


class MacosMemoryTestCase(unittest.TestCase):
    """macOS exposes no SC_AVPHYS_PAGES through os.sysconf.

    That is why a Mac node advertised `mem=0`: it looked alive to every peer,
    was ineligible in the election, and would be skipped by the layer planner.
    """

    VM_STAT = ("Mach Virtual Memory Statistics: (page size of 16384 bytes)\n"
               "Pages free:                          123456.\n"
               "Pages active:                       1000000.\n"
               "Pages inactive:                      654321.\n"
               "Pages speculative:                     5000.\n"
               "Pages wired down:                    400000.\n")

    def test_vm_stat_counts_free_inactive_and_speculative(self):
        self.assertEqual(me.parse_vm_stat(self.VM_STAT, 16384),
                         (123456 + 654321 + 5000) * 16384)

    def test_unparsable_output_is_zero_not_an_error(self):
        self.assertEqual(me.parse_vm_stat("", 16384), 0)
        self.assertEqual(me.parse_vm_stat("garbage", 0), 0)
        self.assertEqual(me.parse_vm_stat(None, "not-a-size"), 0)

    def test_the_darwin_path_uses_vm_stat_and_the_reported_page_size(self):
        calls = []

        def _run(cmd, **_kwargs):
            calls.append(cmd[0])
            if cmd[0] == "vm_stat":
                return types.SimpleNamespace(stdout=self.VM_STAT)
            return types.SimpleNamespace(stdout="16384\n")

        self.assertEqual(me.macos_available_memory(run=_run),
                         (123456 + 654321 + 5000) * 16384)
        self.assertEqual(calls, ["vm_stat", "sysctl"])

    def test_a_missing_vm_stat_reports_zero(self):
        def _run(_cmd, **_kwargs):
            raise OSError("vm_stat not found")

        self.assertEqual(me.macos_available_memory(run=_run), 0)


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
        data_dir = tempfile.mkdtemp(prefix="mesh_election_probe_")
        probe = create_agent(device_caps="A51",
                             api_url="http://127.0.0.1:11434",
                             data_dir=data_dir)
        try:
            self.assertIsNotNone(probe.mesh_election)
            # The election id is this node's identity: a valid, unique name that
            # is persisted, not a capability string it shares with other devices.
            self.assertTrue(node_identity.is_valid(probe.mesh_election.node_id),
                            probe.mesh_election.node_id)
            self.assertEqual(probe.mesh_election.node_id, probe.node_id)
            self.assertEqual(probe.mesh_election.priority, 500)
            # Same data dir, same name: an upgrade or restart keeps its identity.
            second = create_agent(device_caps="A51",
                                  api_url="http://127.0.0.1:11434",
                                  data_dir=data_dir)
            try:
                self.assertEqual(second.node_id, probe.node_id,
                                 "the node changed identity across a restart")
            finally:
                try:
                    second.cleanup()
                except Exception:
                    pass
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
