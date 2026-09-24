#!/usr/bin/env python3
"""Track A regression: mesh peer health must cross the BT wire.

Source-level checks (same pattern as test_mesh_role_ui) so the Kotlin
wiring is guarded without an Android SDK or a device. The invariants:

  1. DeviceMeshManager parses `mesh/health` and clamps the advertised
     fields (a peer cannot assert an out-of-range thermal/priority).
  2. Both peer serializers emit the four health keys ONLY when the peer
     has actually advertised health -- absence is never fabricated.
  3. SensorPublisherService broadcasts health on a fixed cadence, carries
     a monotone seq, and cancels the task on destroy.
  4. The Python consumer forwards peer `seq` into the election.
"""
import os
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JAVA = os.path.join(ROOT, "platforms", "android", "app", "src", "main",
                    "java", "com", "samurai", "shugocore")


def read(relpath: str) -> str:
    with open(os.path.join(JAVA, relpath)) as fh:
        return fh.read()


class PeerHealthDataModel(unittest.TestCase):
    def test_peer_health_holds_four_election_fields(self):
        mesh = read(os.path.join("runtime", "DeviceMeshManager.kt"))
        self.assertIn("data class PeerHealth(", mesh)
        for field in ("thermalStatus", "memAvailableBytes", "priority", "seq"):
            self.assertIn(field, mesh)

    def test_mesh_peer_carries_optional_health(self):
        """Absence is explicit: null until a peer advertises."""
        mesh = read(os.path.join("runtime", "DeviceMeshManager.kt"))
        self.assertIn("var health: PeerHealth? = null", mesh)


class MeshHealthParsing(unittest.TestCase):
    def setUp(self):
        self.mesh = read(os.path.join("runtime", "DeviceMeshManager.kt"))

    def test_handles_mesh_health_message(self):
        self.assertIn('"mesh/health" -> {', self.mesh)
        pub = read(os.path.join("runtime", "SensorPublisherService.kt"))
        self.assertIn('put("type", "mesh/health")', pub)

    def test_clamps_advertised_fields(self):
        """Thermal 0..4 and priority 1..9999 are clamped on receipt."""
        self.assertIn("kotlin.math.min(4, thermal)", self.mesh)
        self.assertIn("kotlin.math.max(0,", self.mesh)
        self.assertIn("kotlin.math.min(9999, priority)", self.mesh)

    def test_requires_present_fields_before_accepting(self):
        """A message without the fields must not create a health record."""
        self.assertIn("if (thermal >= 0 && mem >= 0) {", self.mesh)


class PeerSerializerNoFabrication(unittest.TestCase):
    """Both serializers (push path + tick path) must gate health keys on
    peer.health being non-null."""

    def test_push_sensor_agents_gates_on_health(self):
        mesh = read(os.path.join("runtime", "DeviceMeshManager.kt"))
        self.assertIn("peer.health?.let { h ->", mesh)
        for key in ('put("thermal_status", h.thermalStatus)',
                    'put("mem_available_bytes", h.memAvailableBytes)',
                    'put("priority", h.priority)',
                    'put("seq", h.seq)'):
            self.assertIn(key, mesh)

    def test_tick_telemetry_gates_on_health(self):
        svc = read("ShugoCoreService.kt")
        self.assertIn("peer.health?.let { h ->", svc)
        self.assertIn('put("thermal_status", h.thermalStatus)', svc)
        self.assertIn('put("mem_available_bytes", h.memAvailableBytes)', svc)


class PublisherHealthBroadcast(unittest.TestCase):
    def setUp(self):
        self.pub = read(os.path.join("runtime", "SensorPublisherService.kt"))

    def test_broadcasts_on_fixed_cadence(self):
        self.assertIn("HEALTH_INTERVAL_MS = 5_000L", self.pub)
        self.assertIn("startHealthBroadcast()", self.pub)
        self.assertIn("healthTask = executor.scheduleAtFixedRate", self.pub)

    def test_carries_monotone_seq(self):
        self.assertIn("private var meshSeq: Long = 0L", self.pub)
        self.assertIn('put("seq", ++meshSeq)', self.pub)

    def test_reads_real_thermal_and_memory(self):
        self.assertIn("thermalMonitor?.getThermalInfo()", self.pub)
        self.assertIn("info.state.ordinal", self.pub)
        self.assertIn("mem.availMem", self.pub)

    def test_skips_when_no_thermal_monitor(self):
        """No monitor -> no advertisement (peer stays ineligible)."""
        self.assertIn("health broadcast skipped: no thermal monitor", self.pub)

    def test_cancelled_on_destroy(self):
        self.assertIn("healthTask?.cancel(true)", self.pub)

    def test_priority_override_is_clamped(self):
        self.assertIn("EXTRA_ELECTION_PRIORITY", self.pub)
        self.assertIn("if (it in 1..9999) electionPriority = it", self.pub)


class PythonForwardsPeerSeq(unittest.TestCase):
    def test_agent_forwards_seq_into_election(self):
        with open(os.path.join(ROOT, "shugocore_agent.py")) as fh:
            agent = fh.read()
        self.assertIn('"seq": peer.get("seq", 0)', agent)


if __name__ == "__main__":
    unittest.main()
