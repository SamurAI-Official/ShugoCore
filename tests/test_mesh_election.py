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
from mesh_election import MeshElection, available_memory_bytes  # noqa: E402
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


class IncumbencyTestCase(unittest.TestCase):
    """Who leads must not depend on who was heard first.

    Observed live: the desktop and the Mac both advertised priority 10; the phones
    elected the Mac, the Mac elected itself, and the desktop elected itself -- so every
    delegated action the desktop sent was refused with "sender is not the primary" by
    the very peers it was asking, while each node's own status looked healthy. The
    incumbency rule compared priority alone, which makes two equal-priority nodes
    mutually non-deposable: whichever won first kept the lease for ever and the camps
    never merged.
    """

    def _observer(self, node_id, priority=10, audit=None):
        e = MeshElection(node_id=node_id, priority=priority, audit=audit)
        e.local_heartbeat(thermal_status=0, mem_available_bytes=DESKTOP_MEM)
        return e

    def _audit_log(self):
        events = []

        class _Audit:
            @staticmethod
            def append(event_type, payload):
                events.append((event_type, payload))

        return events, _Audit()

    def test_a_better_ranked_challenger_deposes_whatever_the_arrival_order(self):
        # The Mac was heard first; the desktop (same priority, earlier id) must still win.
        mac_first = self._observer("shugo-tab", priority=500)
        mac_first.observe_heartbeat({"node_id": "shugo-mac", "priority": 10,
                                     "mem_available_bytes": DESKTOP_MEM})
        self.assertEqual(mac_first.tick()["primary"], "shugo-mac")
        mac_first.observe_heartbeat({"node_id": "shugo-desktop", "priority": 10,
                                     "mem_available_bytes": DESKTOP_MEM})
        self.assertEqual(mac_first.tick()["primary"], "shugo-desktop")
        # ... and the other way round gives the same leader: the rank decides.
        desktop_first = self._observer("shugo-a16", priority=500)
        desktop_first.observe_heartbeat({"node_id": "shugo-desktop", "priority": 10,
                                         "mem_available_bytes": DESKTOP_MEM})
        self.assertEqual(desktop_first.tick()["primary"], "shugo-desktop")
        desktop_first.observe_heartbeat({"node_id": "shugo-mac", "priority": 10,
                                         "mem_available_bytes": DESKTOP_MEM})
        self.assertEqual(desktop_first.tick()["primary"], "shugo-desktop")

    def test_a_worse_ranked_challenger_still_does_not_depose_a_healthy_holder(self):
        """Incumbency still holds: no re-homing for a node that ranks behind."""
        e = self._observer("shugo-desktop", priority=10)
        e.observe_heartbeat({"node_id": "shugo-mac", "priority": 10,
                             "mem_available_bytes": DESKTOP_MEM})
        self.assertEqual(e.tick()["primary"], "shugo-desktop")
        e.observe_heartbeat({"node_id": "shugo-aaa", "priority": 20,
                             "mem_available_bytes": DESKTOP_MEM})
        self.assertEqual(e.tick()["primary"], "shugo-desktop")

    def test_the_heartbeat_carries_what_this_node_thinks_leads(self):
        e = self._observer("shugo-tab", priority=500)
        e.observe_heartbeat({"node_id": "shugo-mac", "priority": 10,
                             "mem_available_bytes": DESKTOP_MEM})
        e.tick()
        self.assertEqual(e.local_heartbeat()["primary"], "shugo-mac")
        # A peer that says nothing about the lease is unknown, not disagreeing.
        self.assertEqual(e.live_peers()[0].get("primary"), "")

    def test_a_peer_claiming_another_primary_is_reported_once(self):
        events, audit = self._audit_log()
        e = self._observer("shugo-desktop", priority=10, audit=audit)
        claim = {"node_id": "shugo-tab", "priority": 500, "primary": "shugo-mac",
                 "mem_available_bytes": DESKTOP_MEM}
        e.observe_heartbeat(claim)
        e.tick()
        e.observe_heartbeat(claim)
        e.tick()
        disagreements = [p for kind, p in events
                         if kind == "mesh_lease_disagreement"]
        self.assertEqual(len(disagreements), 1)
        self.assertEqual(disagreements[0]["peer"], "shugo-tab")
        self.assertEqual(disagreements[0]["peer_primary"], "shugo-mac")
        self.assertEqual(disagreements[0]["our_primary"], "shugo-desktop")

    def test_a_peer_that_is_one_heartbeat_behind_is_not_a_disagreement(self):
        """It claims us, which means it agrees -- just less recently."""
        events, audit = self._audit_log()
        e = self._observer("shugo-desktop", priority=10, audit=audit)
        e.observe_heartbeat({"node_id": "shugo-tab", "priority": 500,
                             "primary": "shugo-desktop",
                             "mem_available_bytes": DESKTOP_MEM})
        e.tick()
        self.assertEqual([k for k, _ in events if "disagreement" in k], [])


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


class TestAvailableMemoryBytes(unittest.TestCase):
    """A host that cannot measure memory must not report zero.

    The election reads ``<= 0`` as "no-headroom" and refuses the node. macOS
    has no ``SC_AVPHYS_PAGES`` — ``os.sysconf`` raises ``ValueError`` for that
    name — so before the vm_stat fallback every Mac advertised zero headroom
    and disqualified itself from an election it was the best candidate for.
    Live symptom: ``mesh election: no eligible peer yet:
    shugo-MacBook=no-headroom`` against real, healthy peers.
    """

    def test_reports_a_positive_figure(self):
        self.assertGreater(available_memory_bytes(), 0)

    def test_never_negative(self):
        self.assertGreaterEqual(available_memory_bytes(), 0)

    def test_below_physical_memory(self):
        try:
            total = (int(os.sysconf("SC_PHYS_PAGES"))
                     * int(os.sysconf("SC_PAGE_SIZE")))
        except (AttributeError, ValueError, OSError):
            self.skipTest("no SC_PHYS_PAGES on this platform")
        self.assertLessEqual(available_memory_bytes(), total)

    def test_a_node_reporting_it_is_eligible(self):
        """The regression end to end: a positive figure elects the node."""
        election = MeshElection("shugo-macbook", priority=10)
        election.observe_heartbeat({
            "node_id": "shugo-macbook", "priority": 10, "thermal_status": 0,
            "mem_available_bytes": available_memory_bytes()})
        self.assertIsNone(MeshElection._eligible(
            {"paired": True, "thermal_status": 0,
             "mem_available_bytes": available_memory_bytes()}))
        self.assertEqual(election.tick()["primary"], "shugo-macbook")


if __name__ == "__main__":
    unittest.main()
