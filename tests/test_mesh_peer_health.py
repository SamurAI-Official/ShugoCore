#!/usr/bin/env python3
"""Track A — mesh peer health tests.

Verifies the Python election logic that consumes the Kotlin `mesh/health`
heartbeats (thermal_status, mem_available_bytes, priority, seq) and the
fail-closed rule for peers that never advertise health.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from mesh_election import MeshElection  # noqa: E402

DESKTOP_MEM = 8_000_000_000  # healthy desktop headroom


class TestHealthHeartbeat(unittest.TestCase):
    """The election consumes `mesh/health` heartbeats as first-class
    election inputs (thermal + mem + priority + seq staleness)."""

    def test_healthy_peripheral_wins_over_hot_desktop(self):
        """A healthy peripheral (priority 500, thermal 0) beats a
        desktop that advertises thermal_status >= 3."""
        e = MeshElection(node_id="android-A51", priority=500)
        # Desktop becomes thermal-critical -> ineligible.
        e.observe_heartbeat({
            "node_id": "macbook",
            "priority": 10,
            "mem_available_bytes": DESKTOP_MEM,
            "thermal_status": 3,
            "seq": 1,
        })
        e.local_heartbeat(thermal_status=0, mem_available_bytes=2_000_000)
        result = e.tick()
        self.assertEqual(result["primary"], "android-A51")
        self.assertTrue(result["is_primary"])

    def test_hot_peripheral_loses_to_desktop(self):
        """thermal_status >= 3 makes the peripheral ineligible."""
        e = MeshElection(node_id="android-A51", priority=500)
        e.observe_heartbeat({
            "node_id": "macbook",
            "priority": 10,
            "mem_available_bytes": DESKTOP_MEM,
            "thermal_status": 0,
            "seq": 1,
        })
        e.local_heartbeat(thermal_status=3, mem_available_bytes=2_000_000)
        result = e.tick()
        self.assertEqual(result["primary"], "macbook")
        self.assertFalse(result["is_primary"])

    def test_zero_headroom_peripheral_excluded(self):
        """mem_available_bytes == 0 excludes the peripheral."""
        e = MeshElection(node_id="android-A51", priority=500)
        e.observe_heartbeat({
            "node_id": "macbook",
            "priority": 10,
            "mem_available_bytes": DESKTOP_MEM,
            "thermal_status": 0,
            "seq": 1,
        })
        e.local_heartbeat(thermal_status=0, mem_available_bytes=0)
        result = e.tick()
        self.assertEqual(result["primary"], "macbook")

    def test_a_restarted_peer_replaces_its_record(self):
        """A lower seq is a rebooted peer, not a stale frame: accept it.

        Dropping it (the old rule) left a node that remembered a high sequence
        blind to that peer for good -- it advertised every 10 s and stayed
        invisible, which is what made the hive flap after every redeploy. A
        duplicate (equal seq) still cannot rewrite the record; see
        test_mesh_election.TestElectionRules.
        """
        e = MeshElection(node_id="android-A51", priority=500)
        e.observe_heartbeat({
            "node_id": "macbook",
            "priority": 10,
            "mem_available_bytes": DESKTOP_MEM,
            "thermal_status": 0,
            "seq": 5,
        })
        result = e.observe_heartbeat({
            "node_id": "macbook",
            "priority": 10,
            "mem_available_bytes": DESKTOP_MEM,
            "thermal_status": 0,
            "seq": 3,  # the peer rebooted and its counter began again
        })
        self.assertTrue(result)
        tick_result = e.tick()
        self.assertEqual(tick_result["primary"], "macbook")
        entry = e._nodes.get("macbook")
        self.assertIsNotNone(entry)
        self.assertEqual(int(entry.get("seq", 0)), 3)

    def test_fresh_seq_updates_entry(self):
        """A heartbeat with seq > last-known seq updates the entry."""
        e = MeshElection(node_id="android-A51", priority=500)
        e.observe_heartbeat({
            "node_id": "macbook",
            "priority": 10,
            "mem_available_bytes": DESKTOP_MEM,
            "thermal_status": 0,
            "seq": 1,
        })
        e.observe_heartbeat({
            "node_id": "macbook",
            "priority": 10,
            "mem_available_bytes": DESKTOP_MEM,
            "thermal_status": 0,
            "seq": 2,
        })
        entry = e._nodes.get("macbook")
        self.assertIsNotNone(entry)
        self.assertEqual(int(entry.get("seq", 0)), 2)


class TestPeerHealthOmission(unittest.TestCase):
    """A peer that never advertised mesh/health stays ineligible —
    absence is not fabrication (fail-closed)."""

    def test_peer_without_health_defaults_to_safe(self):
        """A peer with no thermal_status / mem fields defaults to
        thermal=0 / mem=0. The election treats mem=0 as no-headroom
        (ineligible), per the documented rule."""
        e = MeshElection(node_id="android-A51", priority=500)
        # Peer advertises no health fields at all.
        e.observe_heartbeat({
            "node_id": "macbook",
            "priority": 10,
        })
        e.local_heartbeat(thermal_status=0, mem_available_bytes=2_000_000)
        result = e.tick()
        # Peer has no mem_available_bytes -> defaults to 0 -> excluded.
        self.assertEqual(result["primary"], "android-A51")
        self.assertTrue(result["is_primary"])

    def test_peer_with_health_beats_local(self):
        """A peer that DOES advertise health can win."""
        e = MeshElection(node_id="android-A51", priority=500)
        e.observe_heartbeat({
            "node_id": "macbook",
            "priority": 10,
            "mem_available_bytes": DESKTOP_MEM,
            "thermal_status": 0,
            "seq": 1,
        })
        e.local_heartbeat(thermal_status=0, mem_available_bytes=2_000_000)
        result = e.tick()
        self.assertEqual(result["primary"], "macbook")
        self.assertFalse(result["is_primary"])


class TestIneligibleReason(unittest.TestCase):
    """The election reports WHY a peer is excluded (health-based)."""

    def test_thermal_critical_reason(self):
        e = MeshElection(node_id="android-A51", priority=500)
        e.observe_heartbeat({
            "node_id": "macbook",
            "priority": 10,
            "mem_available_bytes": DESKTOP_MEM,
            "thermal_status": 3,
            "seq": 1,
        })
        reasons = e._eligible_reasons()
        self.assertIn("macbook", reasons)
        self.assertEqual(reasons["macbook"], "thermal-critical")

    def test_no_headroom_reason(self):
        e = MeshElection(node_id="android-A51", priority=500)
        e.observe_heartbeat({
            "node_id": "macbook",
            "priority": 10,
            "mem_available_bytes": 0,
            "thermal_status": 0,
            "seq": 1,
        })
        reasons = e._eligible_reasons()
        self.assertEqual(reasons["macbook"], "no-headroom")


if __name__ == "__main__":
    unittest.main()
